"""Merge mods that each ship a full copy of the same vanilla GDA table.

Pipeline
1. Scan override (and the vanilla data folders) into the index.
2. Find GDA names that exist in vanilla and are shipped whole by 2+ mods.
3. For each, diff every mod's copy against vanilla and merge only what changed (gda.merge).
4. Write the merged tables as LOOSE files into override/~da2lm_merged/. Loose files
   beat every archived copy, and that folder sorts after every other folder, so its
   copy is the one the game loads. The mods' own copies stay untouched on disk.

Re-run after changing mods or load order: the folder is rebuilt from scratch each time.
Remove the folder (or `da2lm merge --clear`) to go back to the old behaviour.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .erf import ErfError, hkey_of, iter_archives, read_erf
from .gda import MergeResult, merge
from .index import scan
from .loadorder import MERGE_DIR


def _read_resource(file: str, inner: str | None) -> bytes:
    if inner is None:
        return Path(file).read_bytes()
    erf = read_erf(file)
    for e in erf.entries:
        if (e.name or e.key) == inner:
            data = erf.read(e)
            if data is None:
                raise ErfError(f"{inner} in {Path(file).name} is encrypted or compressed in an unknown way")
            return data
    raise ErfError(f"{inner} not found in {file}")


def vanilla_table_source(vanilla_dirs: list[Path], key: str) -> tuple[bytes | None, str | None]:
    """Like vanilla_table, plus the archive path the winning copy came from."""
    found, src, want = None, None, hkey_of(key)
    for vdir in vanilla_dirs:
        for f in iter_archives(vdir):
            try:
                erf = read_erf(f)
            except (ErfError, OSError, ValueError):
                continue
            for e in erf.entries:
                if e.hkey == want:
                    data = erf.read(e)
                    src = str(f)
                    if data is not None:
                        found = data
    return found, src


def vanilla_table(vanilla_dirs: list[Path], key: str) -> bytes | None:
    """Latest copy of `key` across the vanilla folders (later folders / ERFs win)."""
    found, want = None, hkey_of(key)
    for vdir in vanilla_dirs:
        for f in iter_archives(vdir):
            try:
                erf = read_erf(f)
            except (ErfError, OSError, ValueError):
                continue
            for e in erf.entries:
                if e.hkey == want:
                    data = erf.read(e)
                    if data is not None:
                        found = data
    return found


def candidates(con) -> dict[str, list[tuple[str, str, str | None]]]:
    rows = con.execute("""
        SELECT r.name, m.name, r.file, r.inner_name FROM resources r
        JOIN mods m ON m.id = r.mod_id JOIN vanilla v ON v.key = r.key
        WHERE r.ext = 'gda' AND r.name IS NOT NULL AND m.name != ?
        ORDER BY r.key, r.tier, r.rank""", (MERGE_DIR,)).fetchall()
    out: dict[str, dict[str, tuple]] = {}
    for key, mod, file, inner in rows:
        out.setdefault(key, {})[mod] = (mod, file, inner)   # last copy within a mod wins
    return {k: list(v.values()) for k, v in out.items() if len(v) >= 2}


def run(root: Path, vanilla_dirs: list[Path], db: Path, *, apply: bool = False, extra_roots=(),
        log=print) -> list[MergeResult]:
    con = scan(db, [*extra_roots, root], vanilla=vanilla_dirs, log=log)
    if not con.execute("SELECT 1 FROM vanilla LIMIT 1").fetchone():
        raise SystemExit("no vanilla resources found: point --vanilla at <DA2>/packages/core/data")
    results = []
    for key, copies in sorted(candidates(con).items()):
        van = vanilla_table(vanilla_dirs, key)
        if van is None:
            r = MergeResult(key, error="vanilla copy not readable (encrypted/compressed?)")
        else:
            mods = []
            for mod, file, inner in copies:
                try:
                    mods.append((mod, _read_resource(file, inner)))
                except (ErfError, OSError) as e:
                    log(f"! {key} from {mod}: {e}")
            r = merge(key, van, mods)
        results.append(r)

    if apply:
        out = root / MERGE_DIR
        _clear_tables(out)                    # only the tables: the chargen merge lives here too
        good = [r for r in results if r.data and r.mods_merged >= 2]
        if good:
            out.mkdir(exist_ok=True)
            for r in good:
                (out / r.table).write_bytes(r.data)
            (out / "da2lm_merge_report.txt").write_text(report_text(results), "utf-8")
    return results


def _clear_tables(out: Path) -> bool:
    found = False
    if out.is_dir():
        from .rowmerge import is_rowmerge_file
        for f in list(out.glob("*.gda")) + list(out.glob("da2lm_merge_report.txt")):
            if is_rowmerge_file(f.name):
                continue                      # row merges are rebuilt by rowmerge, not here
            f.unlink()
            found = True
        from .outputs import tidy
        tidy(out.parent)
    return found


def clear(root: Path) -> bool:
    return _clear_tables(root / MERGE_DIR)


def report_text(results: list[MergeResult]) -> str:
    lines = []
    for r in results:
        status = "ERROR: " + r.error if r.error else (
            f"merged {r.mods_merged} mods" if r.mods_merged >= 2 else "not merged (fewer than 2 usable copies)")
        lines.append(f"{r.table}: {status}")
        for d in r.diffs:
            if d.skipped:
                lines.append(f"   - {d.mod}: SKIPPED, {d.skipped}")
            else:
                extra = f", {d.missing_rows} vanilla rows absent (kept)" if d.missing_rows else ""
                lines.append(f"   - {d.mod}: {d.changed_cells} cells in {d.changed_rows} rows changed, "
                             f"{d.added_rows} rows added{extra}")
        for c in r.conflicts:
            lines.append(f"   ! row {c.row_id}, {c.column}: {c.earlier_mod}={c.earlier_value!r} "
                         f"overridden by {c.later_mod}={c.later_value!r}")
        lines.append("")
    return "\n".join(lines)
