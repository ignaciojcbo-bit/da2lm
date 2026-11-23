"""da2lm — DA2 mod indexer / conflict checker (phase 1 of the DA2 launcher).

  da2lm where  [--set-game DIR] [--set-override DIR]   show/set the detected folders
  da2lm scan   [--root PATH ...] [--db FILE]     build the index (game files found automatically)
  da2lm report [--db FILE] [--json] [--all]      list conflicts
  da2lm inspect FILE.erf                         dump an ERF's header and entries
  da2lm order  [--db FILE] [KEY]                 show load order of resources (all, or one)

  da2lm lo list                                  mod folders in load order (last wins)
  da2lm lo export > order.txt                    write the current order as an editable list
  da2lm lo set order.txt [--yes]                 apply an order from a file (one mod per line)
  da2lm lo move MOD (--before X|--after X|--top|--bottom) [--yes]
  da2lm lo undo                                  reverse the last applied change
  (lo commands take --root, default = your DA2 override folder; without --yes they only preview)

  da2lm chargen [--yes] [--clear] [--exclude MOD]  merge every chargenmorphcfg.xml into one
  da2lm rows [--db FILE]                         row-ID overlaps in multi-part tables (needs --vanilla at scan)
  da2lm merge --vanilla DIR [--yes] [--clear]    merge mods that each repack the same vanilla .gda
  da2lm gda dump FILE [--entry NAME] [--rows N]  show a GDA table (FILE may be an .erf + --entry)
  da2lm gda check FILE [--entry NAME]            verify the tool reads/writes this table losslessly

  da2lm extract NAME --from DIR [-o OUTDIR]      copy a resource out of the game's archives
  da2lm pack OUT.erf FILE [FILE ...]             build a DA2 ERF V3.0 (uncompressed) from loose files
  da2lm find TEXT --from DIR                     list archive entries whose name contains TEXT

With no --root, scan uses the default DA2 override folder under your Documents.
Exit code of `report`: 0 = no real high-risk conflicts, 2 = at least one (handy as a pre-launch check).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .erf import read_erf
from .index import conflicts, connect, replacements, scan, summary
from . import loadorder as lo


def default_roots() -> list[Path]:
    from .paths import find_override
    return [find_override()]


def default_vanilla() -> list[Path]:
    from .paths import find_game, vanilla_dirs
    game = find_game()
    return vanilla_dirs(game) if game else []


def cmd_where(a) -> int:
    from .paths import config_path, find_game, find_override, is_game_dir, save_config, vanilla_dirs
    if a.set_game:
        if not is_game_dir(a.set_game):
            print(f"Not a DA2 install (no packages\\core\\data\\2da.rim): {a.set_game}", file=sys.stderr)
            return 1
        save_config(game=a.set_game)
    if a.set_override:
        if not Path(a.set_override).is_dir():
            print(f"Folder not found: {a.set_override}", file=sys.stderr)
            return 1
        save_config(override=a.set_override)
    game, ov = find_game(), find_override()
    print(f"Game install : {game or 'NOT FOUND - set it with: da2lm where --set-game \"<folder>\"'}")
    if game:
        for d in vanilla_dirs(game):
            print(f"  game data  : {d}")
    print(f"Override     : {ov}{'' if ov.is_dir() else '   (does not exist yet)'}")
    print(f"Saved paths  : {config_path()}")
    return 0 if game else 1


def cmd_scan(a) -> int:
    roots = [Path(r) for r in a.root] if a.root else default_roots()
    vanilla = [Path(v) for v in a.vanilla] if a.vanilla else ([] if a.no_vanilla else default_vanilla())
    if not a.vanilla and not a.no_vanilla and not vanilla:
        print("! DA2 install not found, scanning without the game's files."
              ' Set it with: da2lm where --set-game "<folder>"', file=sys.stderr)
    con = scan(a.db, roots, vanilla=vanilla, log=lambda m: print(m, file=sys.stderr))
    s = summary(con)
    print(f"Indexed {s['resources']} resources from {s['mods']} mods "
          f"({s['erfs']} ERFs, {s['erf_errors']} unreadable, {s['unreadable']} entries without readable content).")
    if s["hash_variants"]:
        print(f"V3.0 name-hash variant detected: {s['hash_variants']}")
    if s["vanilla"]:
        print(f"Vanilla reference: {s['vanilla']} game resource names.")
    else:
        print("No --vanilla folder given: only known base tables are checked for full replacement.")
    print(f"Index written to {a.db}")
    return 0


def _not_chargen(items):
    from .chargen import FILE_NAME
    return [x for x in items if x.key != FILE_NAME]


def cmd_report(a) -> int:
    con = connect(a.db)
    cs = _not_chargen(conflicts(con))
    if not a.all:
        shown = [c for c in cs if c.verdict != "identical"]
    else:
        shown = cs
    reps = _not_chargen(replacements(con))
    crit = [r for r in reps if r.risk == "critical"]
    if a.json:
        print(json.dumps({"conflicts": [asdict(c) | {"winner": c.winner.mod} for c in shown],
                          "replacements": [asdict(r) for r in reps]}, indent=2))
    else:
        rreps_early = _row_reports(con) or []
        if crit:
            print("WHOLE TABLES REPLACED (compared with the game's own copy, base + DLC + patch):")
            for r in crit:
                tag = "   [merged by da2lm]" if r.merged else ""
                d, src = _classify(con, r)
                r.verdict = d.verdict if d else "unverified"
                level = TABLE_LEVEL[r.verdict]
                print(f"  [{level:8}] {r.key:28} by {', '.join(r.mods)}{tag}")
                for line in _describe(d, src) + _redundancy(rreps_early, r, d):
                    print(f"             {line}")
            print()
        others = [r for r in reps if r.risk != "critical"]
        if others and a.all:
            print(f"{len(others)} other game files overridden by mods:")
            for r in others:
                print(f"  [{r.risk:6}] {r.key:40} {', '.join(r.mods)}")
            print()
        elif others:
            print(f"{len(others)} other game files overridden by mods (see --all).\n")
        rreps = rreps_early
        if rreps:
            from .rows import describe
            print("ROW OVERLAPS IN MULTI-PART TABLES (rows matched by ID across every part):")
            print("\n".join(describe(rreps)))
            print()
        wm = _within_mod(con)
        if wm:
            print("OVERRIDES INSIDE A SINGLE MOD FOLDER (the later file wins; listed so the active version is visible):")
            for (mod, chain), n in sorted(wm.items()):
                print(f"  {mod}: {n} files  {chain}")
            print()
        counts = {v: sum(c.verdict == v for c in cs) for v in ("real", "unverified", "identical")}
        print(f"{counts['real']} files shipped by more than one mod, {counts['unverified']} unverified, "
              f"{counts['identical']} identical duplicates (harmless{'' if a.all else ', hidden — use --all'}).\n")
        for c in shown:
            print(f"[{c.risk.upper():8}] {c.verdict:10} {c.key}   active: {c.winner.mod}")
            for cp in c.copies:
                mark = "  <- wins" if cp is c.winner else ""
                where = Path(cp.file).name + (f" :: {cp.inner_name}" if cp.inner_name else "")
                h = (cp.sha1 or cp.status)[:10]
                print(f"           {cp.mod:30} {where:50} {h}{mark}")
            print()
    # exit 2 = there are clashes between mods to review (not: a mod overriding the game)
    rr = _row_reports(con) or []
    bad = any(r.disagreements for r in rr) or \
        any(c.verdict == "real" and c.risk in ("high", "critical") for c in cs)
    return 2 if bad else 0


def _classify(con, rep):
    """Diff the winning mod copy of a whole-table replacement against the game's copy."""
    from .gda import diff_table
    from .index import vanilla_dirs, winning_copy
    from .merge import _read_resource, vanilla_table_source
    win = winning_copy(con, rep.hkey)
    dirs = [Path(d) for d in vanilla_dirs(con)]
    if not win or not dirs:
        return None, None
    try:
        mod = _read_resource(win[1], win[2])
    except Exception:
        return None, None
    van, src = vanilla_table_source(dirs, rep.key)
    if van is None:
        return None, src
    return diff_table(van, mod), src


def _row_reports(con):
    from .index import vanilla_dirs
    from .merge import _read_resource
    from .rows import analyze
    dirs = vanilla_dirs(con)
    if not dirs:
        return None
    return analyze(con, dirs, _read_resource)


def cmd_rows(a) -> int:
    from .rows import describe
    con = connect(a.db)
    reps = _row_reports(con)
    if reps is None:
        print("Run scan with --vanilla first: row checks need the game's tables.", file=sys.stderr)
        return 1
    lines = describe(reps)
    print("\n".join(lines) if lines else "No mod rows overlap with other copies of the same table.")
    return 2 if any(r.disagreements for r in reps) else 0


def _within_mod(con):
    """Copies of one resource inside the same mod folder, from different files: the mod's
    own subfolders overriding each other (e.g. optional variants kept next to the main files)."""
    rows = con.execute("""
        SELECT m.name, r.key, r.file, r.inner_name, r.sha1, r.tier, r.rank FROM resources r
        JOIN mods m ON m.id = r.mod_id
        WHERE (m.id, r.key) IN (SELECT mod_id, key FROM resources GROUP BY mod_id, key HAVING COUNT(*) > 1)
        ORDER BY m.name, r.key, r.tier, r.rank""").fetchall()
    groups = {}
    from .chargen import FILE_NAME
    for mod, key, f, inner, sha, tier, rank in rows:
        if key == FILE_NAME:
            continue                     # handled in the Character creation merge
        groups.setdefault((mod, key), []).append((f, inner, sha))
    summary = {}
    for (mod, key), copies in groups.items():
        if len({c[0] for c in copies}) < 2 or len({c[2] for c in copies}) < 2:
            continue                     # same file twice, or identical content: harmless
        chain = " -> ".join(dict.fromkeys(
            _short(mod, c[0]) if c[1] else _loose_folder(mod, c[0]) for c in copies))
        summary[(mod, chain)] = summary.get((mod, chain), 0) + 1
    return summary


def _loose_folder(mod, path):
    """Loose files are grouped by folder, so 64 per-weapon files collapse into one line."""
    short = _short(mod, path)
    folder = short.rsplit("\\", 1)[0] if "\\" in short else "(mod root)"
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return f"{folder}\\*.{ext} (loose)"


def _short(mod, path):
    p = path.replace("/", "\\")
    i = p.find("\\" + mod + "\\")
    return p[i + len(mod) + 2:] if i >= 0 else p.rsplit("\\", 1)[-1]


TABLE_LEVEL = {"changes": "OVERRIDE", "deletes": "WARNING", "adds": "note", "identical": "note",
               "unverified": "CHECK"}


def _redundancy(rreps, rep, d):
    if not d or not d.changed:
        return []
    from .rows import redundant_with
    same = redundant_with(rreps, rep.key, d.changed)
    return [f"redundant with {src}: same values in {n} of its {len(d.changed)} changed rows"
            + (" (no effect while both are installed)" if n == len(d.changed) else "")
            for src, n in sorted(same.items())]


def _ids(ids, n=8):
    ids = list(ids)
    return ", ".join(map(str, ids[:n])) + (f" (+{len(ids) - n} more)" if len(ids) > n else "")


def _describe(d, src):
    out = []
    if src and "addins" in src.replace("\\", "/").lower():
        out.append("DLC table: rows in a suffixed table LOSE to DLC rows, so replacing the file by name"
                   " is the only working method here.")
    if d is None:
        out.append("could not compare with the game's copy (not found, or encrypted DLC archive)")
        return out
    if d.error:
        out.append(f"could not compare: {d.error}")
        return out
    if d.changed:
        out.append(f"overrides values in {len(d.changed)} rows: {_ids(d.changed)}. Rows with the same IDs in other"
                   " mods' suffixed tables still win over this file.")
        cols = sorted({c for v in d.changed.values() for c in v})
        out.append(f"columns touched: {_ids(cols, 6)}")
    if d.deleted:
        out.append(f"deletes {len(d.deleted)} rows: {_ids(d.deleted)}. Needs a whole-table file (a suffixed"
                   " table can't delete rows).")
    if d.added:
        out.append(f"adds {len(d.added)} rows: {_ids(d.added)}")
    if d.duplicates:
        out.append(f"duplicate row IDs in the mod copy: {_ids(d.duplicates)}")
    if d.verdict == "identical":
        out.append("identical to the game's copy: no effect")
    return out


def cmd_inspect(a) -> int:
    erf = read_erf(a.file)
    print(f"{a.file}\n  version      {erf.version}\n  entries      {len(erf.entries)}")
    if erf.version != "2.0":
        print(f"  encryption   {erf.encryption}\n  compression  {erf.compression}\n"
              f"  module id    {erf.module_id}")
    if erf.version == "3.0":
        print(f"  hash variant {erf.hash_variant or 'undetected'} "
              f"({erf.hash_mismatches} named entries don't match)")
    print()
    for e in erf.entries[: a.limit or None]:
        nh = f"{e.name_hash:016x}" if e.name_hash is not None else ""
        print(f"  {e.offset:>10} {e.packed_size:>10} {e.unpacked_size:>10}  {nh:16}  {e.name or '(unnamed)'}")
    if a.limit and len(erf.entries) > a.limit:
        print(f"  ... {len(erf.entries) - a.limit} more")
    return 0


def cmd_order(a) -> int:
    con = connect(a.db)
    sql = ("SELECT r.rank, m.name, r.file, r.inner_name FROM resources r JOIN mods m ON m.id=r.mod_id "
           + ("WHERE r.key=? " if a.key else "") + "ORDER BY r.tier, r.rank")
    from .erf import hkey_of
    for rank, mod, f, inner in con.execute(sql, (hkey_of(a.key),) if a.key else ()):
        print(f"{rank:>7}  {mod:30} {f}{' :: ' + inner if inner else '   (loose)'}")
    if a.key:
        print("\nLast line wins. Loose files always beat copies inside .erf/.rim archives.")
    return 0


def _lo_root(a) -> Path:
    return Path(a.root) if a.root else default_roots()[0]


def _print_mods(root: Path) -> None:
    mods = lo.current(root)
    for i, m in enumerate(mods, 1):
        tag = "" if m.managed else "   (unmanaged)"
        print(f"{i:>4}. {m.name:40} [{m.folder}]{tag}")
    if mods:
        print(f"\n      last = highest priority ({mods[-1].name} wins ties)")
    lf = lo.loose_files(root)
    if lf:
        print(f"      + {len(lf)} loose file(s) directly in override (not movable)")


def _do_plan(a, root: Path, order: list[str]) -> int:
    renames, warnings = lo.plan(root, order)
    for w in warnings:
        print(f"! {w}")
    if not renames:
        print("Already in that order, nothing to do.")
        return 0
    for old, new in renames:
        print(f"  {old}  ->  {new}")
    if not a.yes:
        print(f"\nPreview only. Re-run with --yes to rename {len(renames)} folder(s).")
        return 0
    j = lo.apply(root, renames)
    print(f"\nDone. Undo with: da2lm lo undo   (journal {j.name})")
    return 0


def cmd_lo(a) -> int:
    root = _lo_root(a)
    if not root.is_dir():
        print(f"override folder not found: {root}", file=sys.stderr)
        return 1
    if a.lo_cmd == "list":
        _print_mods(root)
    elif a.lo_cmd == "export":
        print("# One mod per line. Top loads first, bottom wins. Lines starting with # are ignored.")
        for m in lo.current(root):
            print(m.name)
    elif a.lo_cmd == "set":
        lines = Path(a.file).read_text("utf-8").splitlines()
        order = [l.strip() for l in lines if l.strip() and not l.lstrip().startswith("#")]
        return _do_plan(a, root, order)
    elif a.lo_cmd == "move":
        names = [m.name for m in lo.current(root)]
        try:
            order = lo.move(names, a.mod, before=a.before, after=a.after, top=a.top, bottom=a.bottom)
        except KeyError as e:
            print(f"unknown mod: {e.args[0]}", file=sys.stderr)
            return 1
        return _do_plan(a, root, order)
    elif a.lo_cmd == "undo":
        j = lo.undo(root)
        print(f"Reverted {j.name}." if j else "Nothing to undo.")
    return 0


def cmd_merge(a) -> int:
    from . import merge as mg
    root = _lo_root(a)
    from . import rowmerge as rm
    if a.clear:
        t, r = mg.clear(root), rm.clear(root)
        print("Removed merged tables." if (t or r) else "Nothing to clear.")
        return 0
    vanilla = [Path(v) for v in a.vanilla] if a.vanilla else default_vanilla()
    if not vanilla:
        print('DA2 install not found. Set it with: da2lm where --set-game "<folder>"', file=sys.stderr)
        return 1
    results = mg.run(root, vanilla, Path(a.db), apply=a.yes,
                     log=lambda m: print(m, file=sys.stderr))
    rows = rm.run(root, vanilla, Path(a.db), apply=a.yes, log=lambda m: print(m, file=sys.stderr))
    if not results and not any(f.rows for f in rows):
        print("No game table is repacked by more than one mod, and no two mods edit the same table rows."
              " Nothing to merge.")
        return 0
    if results:
        print(mg.report_text(results))
    lines = rm.report_lines(rows)
    if lines:
        print("Rows edited by more than one mod (multi-part tables):")
        print("\n".join("  " + l for l in lines))
    ok = [r for r in results if r.data and r.mods_merged >= 2]
    n_rows = sum(1 for f in rows if f.data)
    if a.yes:
        from .outputs import stamp
        stamp(root, scan(a.db, [root], vanilla=vanilla, log=lambda m: None))
        print(f"Wrote {len(ok)} merged table(s) and {n_rows} combined row file(s) to {root / lo.MERGE_DIR}")
        print("Re-run after changing mods or load order. Undo: da2lm merge --clear")
    else:
        print(f"Preview only. Re-run with --yes to write {len(ok)} merged table(s) and {n_rows} combined row file(s).")
    return 0


def _gda_bytes(a) -> bytes:
    from .merge import _read_resource
    return _read_resource(a.file, a.entry) if a.entry else Path(a.file).read_bytes()


def cmd_gda(a) -> int:
    from .gda import GDA_TYPES, Gda, roundtrip_ok
    data = _gda_bytes(a)
    if a.gda_cmd == "check":
        ok = roundtrip_ok(data)
        print("OK: lossless read/write" if ok else "MISMATCH: do not merge this table, send me the file")
        return 0 if ok else 3
    t = Gda.from_bytes(data)
    print(f"{len(t.cols)} columns, {len(t.rows)} rows, GFF {t.g.version}, GDA {t.g.fversion.decode()}")
    print("columns: " + ", ".join(f"{t.col_name(i)}:{GDA_TYPES.get(t.types[i], '?')}" for i in range(len(t.cols))))
    for r in t.rows[: a.rows or None]:
        print("  " + " | ".join("-" if v is None else (v.rstrip("\x00") if isinstance(v, str) else str(v))
                                for v in t.cells(r)))
    return 0


def cmd_extract(a) -> int:
    from .erf import iter_archives
    from .erf import hkey_of
    name, found = a.name.lower(), []
    want = hkey_of(name)
    for src in a.src:
        for f in iter_archives(src):
            try:
                erf = read_erf(f)
            except Exception:
                continue
            for e in erf.entries:
                if e.hkey == want:
                    found.append((f, erf, e))
    if not found:
        print(f"{a.name} not found under {', '.join(a.src)}", file=sys.stderr)
        return 1
    for f, _, _ in found:
        print(f"found in {f}")
    f, erf, e = found[-1]
    data = erf.read(e)
    if data is None:
        print(f"{a.name} in {f.name} is encrypted or compressed in an unsupported way", file=sys.stderr)
        return 1
    out = Path(a.out) / (e.name.replace("\\", "/").rsplit("/", 1)[-1] if e.name else a.name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"wrote {out} ({len(data)} bytes, from {f.name})")
    return 0


def cmd_find(a) -> int:
    from .erf import iter_archives
    text, hits, unnamed = a.text.lower(), 0, 0
    for src in a.src:
        for f in iter_archives(src):
            try:
                erf = read_erf(f)
            except Exception as e:
                print(f"! {f}: {e}", file=sys.stderr)
                continue
            for e in erf.entries:
                if e.name is None:
                    unnamed += 1
                elif text in e.name.lower():
                    hits += 1
                    print(f"{e.name:50} {e.unpacked_size:>10}  {f}")
    print(f"\n{hits} match(es)." + (f" {unnamed} entries have no stored name and can't be searched by text "
                                     f"(extract still finds them by exact name)." if unnamed else ""))
    return 0


def cmd_pack(a) -> int:
    from .erf import write_erf_v3
    files = [(Path(x).name, Path(x).read_bytes()) for x in a.files]
    names = [n.lower() for n, _ in files]
    if len(set(names)) != len(names):
        print("two input files have the same name", file=sys.stderr)
        return 1
    write_erf_v3(a.out, files)
    print(f"wrote {a.out} with {len(files)} file(s)")
    return 0


def cmd_chargen(a) -> int:
    from . import chargen as cg
    from .index import vanilla_dirs
    from .paths import load_config, save_config
    root = _lo_root(a)
    if a.clear:
        print("Removed the merged character-creation file." if cg.clear(root) else "Nothing to clear.")
        return 0
    excluded = set(load_config().get("chargen_excluded", []))
    if a.exclude or a.include:
        excluded = (excluded | set(a.exclude or [])) - set(a.include or [])
        save_config(chargen_excluded=sorted(excluded))
    con = connect(a.db)
    copies = cg.mod_copies(con)
    if not copies:
        print("No mod ships a chargenmorphcfg.xml.")
        return 0
    out = cg.build(con, vanilla_dirs(con), excluded)
    print(f"Character creation: game file + {len(out.results)} mod copies"
          + (f" ({len(excluded)} excluded: {', '.join(sorted(excluded))})" if excluded else ""))
    for r in out.results:
        print(f"  {r.mod}: adds {r.n_added} entries"
              + (f" in {', '.join(f'{s} ({len(v)})' for s, v in r.added.items())}" if r.added else "")
              + f"; {r.already} already present"
              + (f"; lacks {r.missing_from_game} game entries (kept)" if r.missing_from_game else "")
              + (f"; new sections: {', '.join(r.new_sections)}" if r.new_sections else ""))
        for e in r.errors:
            print(f"     note: {e}")
    if a.yes:
        p = cg.write(root, out)
        from .outputs import stamp
        stamp(root, scan(a.db, [root], vanilla=vanilla_dirs(con), log=lambda m: None))
        print(f"Wrote {p}. Re-run after adding or removing chargen mods.")
    else:
        print("Preview only. Re-run with --yes to write the merged file.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="da2lm", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"da2lm {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="build the index")
    s.add_argument("--root", action="append", help="folder to scan; repeat in load order (later wins)")
    s.add_argument("--no-vanilla", action="store_true", help="don't compare with the game's files")
    s.add_argument("--vanilla", action="append",
                   help="game data folder to compare against, e.g. <DA2>\\packages\\core\\data (repeatable)")
    s.add_argument("--db", default="da2lm.db")
    s.set_defaults(fn=cmd_scan)

    r = sub.add_parser("report", help="list conflicts")
    r.add_argument("--db", default="da2lm.db")
    r.add_argument("--json", action="store_true")
    r.add_argument("--all", action="store_true", help="include identical duplicates")
    r.set_defaults(fn=cmd_report)

    i = sub.add_parser("inspect", help="dump an ERF")
    i.add_argument("file")
    i.add_argument("--limit", type=int, default=50, help="max entries to list (0 = all)")
    i.set_defaults(fn=cmd_inspect)

    o = sub.add_parser("order", help="show assumed load order")
    o.add_argument("key", nargs="?")
    o.add_argument("--db", default="da2lm.db")
    o.set_defaults(fn=cmd_order)

    l = sub.add_parser("lo", help="load order")
    lsub = l.add_subparsers(dest="lo_cmd", required=True)
    for name in ("list", "export", "set", "move", "undo"):
        x = lsub.add_parser(name)
        x.add_argument("--root", help="override folder")
        if name in ("set", "move"):
            x.add_argument("--yes", action="store_true", help="actually rename folders")
        if name == "set":
            x.add_argument("file")
        if name == "move":
            x.add_argument("mod")
            g = x.add_mutually_exclusive_group(required=True)
            g.add_argument("--before")
            g.add_argument("--after")
            g.add_argument("--top", action="store_true")
            g.add_argument("--bottom", action="store_true")
    l.set_defaults(fn=cmd_lo)

    wh = sub.add_parser("where", help="show (or set) the game and override folders")
    wh.add_argument("--set-game")
    wh.add_argument("--set-override")
    wh.set_defaults(fn=cmd_where)

    cg = sub.add_parser("chargen", help="merge every mod's character-creation config into one")
    cg.add_argument("--root", help="override folder")
    cg.add_argument("--db", default="da2lm.db")
    cg.add_argument("--yes", action="store_true", help="write the merged file")
    cg.add_argument("--clear", action="store_true", help="remove the merged file")
    cg.add_argument("--exclude", action="append", help="leave this mod out (remembered)")
    cg.add_argument("--include", action="append", help="put an excluded mod back")
    cg.set_defaults(fn=cmd_chargen)

    rw = sub.add_parser("rows", help="row-ID overlaps in multi-part tables")
    rw.add_argument("--db", default="da2lm.db")
    rw.set_defaults(fn=cmd_rows)

    mg = sub.add_parser("merge", help="merge full repacks of vanilla GDA tables")
    mg.add_argument("--root", help="override folder")
    mg.add_argument("--vanilla", action="append", help="game data folder(s), in order (later wins)")
    mg.add_argument("--db", default="da2lm.db")
    mg.add_argument("--yes", action="store_true", help="write the merged tables")
    mg.add_argument("--clear", action="store_true", help="remove previously merged tables")
    mg.set_defaults(fn=cmd_merge)

    gd = sub.add_parser("gda", help="inspect GDA tables")
    gsub = gd.add_subparsers(dest="gda_cmd", required=True)
    for name in ("dump", "check"):
        x = gsub.add_parser(name)
        x.add_argument("file")
        x.add_argument("--entry", help="name inside an .erf")
        if name == "dump":
            x.add_argument("--rows", type=int, default=20)
    gd.set_defaults(fn=cmd_gda)

    ex = sub.add_parser("extract", help="copy a resource out of game archives")
    ex.add_argument("name")
    ex.add_argument("--from", dest="src", action="append", required=True, help="folder to search (repeatable)")
    ex.add_argument("-o", "--out", default=".")
    ex.set_defaults(fn=cmd_extract)

    fd = sub.add_parser("find", help="search archive entry names")
    fd.add_argument("text")
    fd.add_argument("--from", dest="src", action="append", required=True)
    fd.set_defaults(fn=cmd_find)

    pk = sub.add_parser("pack", help="build an ERF V3.0 from loose files")
    pk.add_argument("out")
    pk.add_argument("files", nargs="+")
    pk.set_defaults(fn=cmd_pack)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
