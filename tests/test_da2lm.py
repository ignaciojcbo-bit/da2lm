import json
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from da2lm.cli import main
from da2lm.erf import (Erf, ErfEntry, _decompress, fnv1_64, fnv1a_64, name_hash, read_erf,
                       write_erf_v3)
from da2lm.index import _sha1, conflicts, connect, risk_of, scan, summary


def test_fnv_known_vectors():
    # Standard FNV test vectors for the empty string and "a"
    assert fnv1_64(b"") == 0xCBF29CE484222325
    assert fnv1a_64(b"a") == 0xAF63DC4C8601EC8C
    assert fnv1_64(b"a") == 0xAF63BD4C8601B7BE


@pytest.mark.parametrize("variant", ["fnv1", "fnv1a"])
def test_v3_roundtrip_and_variant_detection(tmp_path, variant):
    files = [("ABI_base_aw.gda", b"gda-bytes"), ("art/model.mmh", b"x" * 13), ("secret.ncs", b"ncs")]
    p = tmp_path / "t.erf"
    write_erf_v3(p, files, variant=variant, unnamed={"secret.ncs"})
    erf = read_erf(p)
    assert erf.version == "3.0"
    assert erf.hash_variant == variant and erf.hash_mismatches == 0
    assert [e.name for e in erf.entries] == ["ABI_base_aw.gda", "art/model.mmh", None]
    assert erf.entries[1].key == "model.mmh"
    assert erf.entries[2].key == f"#{name_hash('secret.ncs', variant):016x}"
    assert all(e.offset % 4 == 0 for e in erf.entries)
    assert [erf.read(e) for e in erf.entries] == [d for _, d in files]


def _v20(path, files):
    off = 32 + 72 * len(files)
    out = bytearray("ERF V2.0".encode("utf-16-le") + struct.pack("<4I", len(files), 0, 0, 0))
    for name, data in files:
        out += name.encode("utf-16-le").ljust(64, b"\0") + struct.pack("<II", off, len(data))
        off += len(data)
    for _, data in files:
        out += data
    Path(path).write_bytes(out)


def test_v20(tmp_path):
    p = tmp_path / "old.erf"
    _v20(p, [("a.ncs", b"AAA"), ("b.gda", b"BB")])
    erf = read_erf(p)
    assert erf.version == "2.0"
    assert [(e.name, erf.read(e)) for e in erf.entries] == [("a.ncs", b"AAA"), ("b.gda", b"BB")]


def test_decompress_framings():
    data = b"hello dragon age " * 50
    assert _decompress(zlib.compress(data), len(data)) == data
    raw = zlib.compressobj(wbits=-15)
    deflated = raw.compress(data) + raw.flush()
    assert _decompress(deflated, len(data)) == data
    assert _decompress(struct.pack("<I", len(data)) + zlib.compress(data), len(data)) == data
    assert _decompress(b"garbage", 10) is None


def test_bad_files(tmp_path):
    p = tmp_path / "bad.erf"
    p.write_bytes(b"not an erf at all, sorry")
    with pytest.raises(Exception):
        read_erf(p)


def _setup_override(root: Path):
    # Mod A: loose chargen + ERF with a spec table and a shared texture
    (root / "A_ArcaneWarrior").mkdir(parents=True)
    write_erf_v3(root / "A_ArcaneWarrior" / "aw.erf",
                 [("spec_base_aw.gda", b"aw"), ("player_core.ncs", b"A-script"), ("t_common.dds", b"same")])
    (root / "A_ArcaneWarrior" / "chargen.gda").write_bytes(b"A-chargen")
    # Mod B: same script (different bytes), same texture (identical bytes), same chargen loose
    (root / "B_Necro").mkdir()
    write_erf_v3(root / "B_Necro" / "necro.erf",
                 [("player_core.ncs", b"B-script"), ("t_common.dds", b"same")])
    (root / "B_Necro" / "chargen.gda").write_bytes(b"B-chargen")
    # Loose file in root, plus a broken ERF that must not stop the scan
    (root / "readme.txt").write_text("hi")
    (root / "B_Necro" / "broken.erf").write_bytes(b"\0" * 10)


def test_scan_and_conflicts(tmp_path):
    root = tmp_path / "override"
    _setup_override(root)
    con = scan(tmp_path / "i.db", [root], log=lambda m: None)
    s = summary(con)
    assert s["mods"] == 2 and s["erfs"] == 3 and s["erf_errors"] == 1   # readme.txt is not a resource
    cs = {c.key: c for c in conflicts(con)}
    assert set(cs) == {"player_core.ncs", "t_common.dds", "chargen.gda"}
    assert cs["player_core.ncs"].verdict == "real" and cs["player_core.ncs"].risk == "high"
    assert cs["t_common.dds"].verdict == "identical"
    assert cs["chargen.gda"].winner.mod == "B_Necro"  # alphabetical: B loads after A
    assert "spec_base_aw.gda" not in cs


def test_later_root_wins(tmp_path):
    r1, r2 = tmp_path / "game", tmp_path / "docs"
    (r1 / "M1").mkdir(parents=True)
    (r2 / "M2").mkdir(parents=True)
    (r1 / "M1" / "x.ncs").write_bytes(b"1")
    (r2 / "M2" / "x.ncs").write_bytes(b"2")
    con = scan(tmp_path / "i.db", [r2, r1], log=lambda m: None)  # r1 given last -> wins
    (c,) = conflicts(con)
    assert c.winner.mod == "M1"


def test_encrypted_marked_unverified(tmp_path):
    root = tmp_path / "o"
    (root / "A").mkdir(parents=True)
    (root / "B").mkdir()
    write_erf_v3(root / "A" / "a.erf", [("dlc.ncs", b"x")])
    p = root / "B" / "b.erf"
    write_erf_v3(p, [("dlc.ncs", b"y")])
    raw = bytearray(p.read_bytes())
    raw[24:28] = struct.pack("<I", 2 << 4)  # flags: blowfish
    p.write_bytes(raw)
    con = scan(tmp_path / "i.db", [root], log=lambda m: None)
    (c,) = conflicts(con)
    assert c.verdict == "unverified"


def test_cli(tmp_path, capsys):
    root = tmp_path / "override"
    _setup_override(root)
    db = str(tmp_path / "i.db")
    assert main(["scan", "--root", str(root), "--db", db]) == 0
    assert main(["report", "--db", db]) == 2          # real high-risk conflict present
    out = capsys.readouterr().out
    assert "player_core.ncs" in out and "<- wins" in out
    assert main(["report", "--db", db, "--json"]) == 2
    assert main(["inspect", str(root / "A_ArcaneWarrior" / "aw.erf")]) == 0
    assert main(["order", "--db", db, "chargen.gda"]) == 0


def test_risk():
    assert risk_of("char_stage.are", "are") == "high"
    assert risk_of("foo.mmh", "mmh") == "medium"
    assert risk_of("foo.dds", "dds") == "low"


# ----------------------------------------------------------------------------- load order
from da2lm import loadorder as lo


def _mods(root, *names):
    for n in names:
        (root / n).mkdir(parents=True)
        (root / n / "shared.ncs").write_bytes(n.encode())


def test_engine_order_and_underscore(tmp_path):
    _mods(tmp_path, "AB_mod", "A_mod", "zzz_Fix")
    # uppercase (NTFS-like) comparison: 'B' (0x42) < '_' (0x5F), so AB_mod comes first
    assert [m.folder for m in lo.current(tmp_path)] == ["AB_mod", "A_mod", "zzz_Fix"]


def test_plan_apply_undo(tmp_path):
    _mods(tmp_path, "Alpha", "Beta", "zz_Gamma")
    renames, warn = lo.plan(tmp_path, ["zz_Gamma", "Beta"])
    assert any("Alpha" in w for w in warn)              # unlisted goes to bottom
    lo.apply(tmp_path, renames)
    assert [m.folder for m in lo.current(tmp_path)] == ["010_zz_Gamma", "020_Beta", "030_Alpha"]
    assert all(m.managed for m in lo.current(tmp_path))
    # move again (swap path exercises the two-phase rename)
    names = [m.name for m in lo.current(tmp_path)]
    renames, _ = lo.plan(tmp_path, lo.move(names, "Alpha", top=True))
    lo.apply(tmp_path, renames)
    assert [m.name for m in lo.current(tmp_path)] == ["Alpha", "zz_Gamma", "Beta"]
    lo.undo(tmp_path)
    assert [m.folder for m in lo.current(tmp_path)] == ["010_zz_Gamma", "020_Beta", "030_Alpha"]
    lo.undo(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != ".da2lm") == ["Alpha", "Beta", "zz_Gamma"]
    assert lo._load_state(tmp_path) == {}
    assert lo.undo(tmp_path) is None


def test_new_mod_is_unmanaged_and_wins(tmp_path):
    _mods(tmp_path, "Alpha", "Beta")
    lo.apply(tmp_path, lo.plan(tmp_path, ["Beta", "Alpha"])[0])
    _mods(tmp_path, "NewMod")
    mods = lo.current(tmp_path)
    assert mods[-1].name == "NewMod" and not mods[-1].managed


def test_move_helpers():
    n = ["a", "b", "c"]
    assert lo.move(n, "a", after="c") == ["b", "c", "a"]
    assert lo.move(n, "c", before="a") == ["c", "a", "b"]
    assert lo.move(n, "b", bottom=True) == ["a", "c", "b"]


def test_order_changes_conflict_winner(tmp_path, capsys):
    root = tmp_path / "override"
    _mods(root, "Alpha", "Beta")
    db = str(tmp_path / "i.db")
    scan(db, [root], log=lambda m: None)
    assert conflicts(scan(db, [root], log=lambda m: None))[0].winner.mod == "Beta"
    assert main(["lo", "move", "Alpha", "--bottom", "--root", str(root)]) == 0   # preview only
    assert (root / "Alpha").exists()
    assert main(["lo", "move", "Alpha", "--bottom", "--root", str(root), "--yes"]) == 0
    (c,) = conflicts(scan(db, [root], log=lambda m: None))
    assert c.winner.mod == "Alpha"
    assert main(["lo", "list", "--root", str(root)]) == 0
    assert main(["lo", "export", "--root", str(root)]) == 0
    assert ".da2lm" not in capsys.readouterr().out


# ----------------------------------------------------------------------------- base tables
from da2lm.index import replacements


def test_single_mod_full_table_is_critical_without_vanilla(tmp_path):
    root = tmp_path / "override"
    (root / "BadMod").mkdir(parents=True)
    (root / "GoodMod").mkdir()
    (root / "BadMod" / "ABI_base.gda").write_bytes(b"whole table")
    (root / "GoodMod" / "abi_base_aw.gda").write_bytes(b"rows")
    con = scan(tmp_path / "i.db", [root], log=lambda m: None)
    reps = replacements(con)
    assert [(r.key, r.risk, r.mods) for r in reps] == [("abi_base.gda", "critical", ["BadMod"])]
    assert main(["report", "--db", str(tmp_path / "i.db")]) == 0      # one mod overriding the game: not a clash


def test_vanilla_reference(tmp_path, capsys):
    data = tmp_path / "game" / "data"
    data.mkdir(parents=True)
    write_erf_v3(data / "2da.erf", [("apr_base.gda", b"v"), ("custom_vanilla.gda", b"v")])
    write_erf_v3(data / "textures.erf", [("uh_hed_mora_0d.dds", b"v")])
    root = tmp_path / "override"
    (root / "Tex").mkdir(parents=True)
    (root / "Tex" / "uh_hed_mora_0d.dds").write_bytes(b"new")
    (root / "Tex" / "custom_vanilla.gda").write_bytes(b"new")   # not in the hardcoded list
    (root / "Tex" / "mymod_new.gda").write_bytes(b"new")         # new table: fine
    db = str(tmp_path / "i.db")
    assert main(["scan", "--root", str(root), "--vanilla", str(data), "--db", db]) == 0
    reps = {r.key: r for r in replacements(connect(db))}
    assert reps["custom_vanilla.gda"].risk == "critical"
    assert reps["uh_hed_mora_0d.dds"].risk == "low" and reps["uh_hed_mora_0d.dds"].vanilla_erf == "textures.erf"
    assert "mymod_new.gda" not in reps
    assert main(["report", "--db", db, "--all"]) == 0
    out = capsys.readouterr().out
    assert "WHOLE TABLES REPLACED" in out and "uh_hed_mora_0d.dds" in out


# ----------------------------------------------------------------------------- GFF4 / GDA / merge
from da2lm.gda import Gda, build, col_hash, merge as merge_tables, roundtrip_ok
from da2lm.gff4 import parse as gff_parse, serialize as gff_serialize

COLS = [("ID", "int"), ("Label", "string"), ("ManaCost", "int"), ("Cooldown", "float"), ("Hostile", "bool")]
VANILLA = [[1, "fireball", 30, 10.0, 1], [2, "heal", 20, 15.0, 0], [3, "stonefist", 25, 8.5, 1]]


def _rows(*edits, add=(), drop=()):
    rows = [list(r) for r in VANILLA if r[0] not in drop]
    for rid, col, val in edits:
        next(r for r in rows if r[0] == rid)[col] = val
    return rows + [list(a) for a in add]


@pytest.mark.parametrize("version", ["V4.0", "V4.1"])
def test_gda_roundtrip(version):
    data = build(COLS, VANILLA, version=version)
    t = Gda.from_bytes(data)
    assert t.hashes[0] == col_hash("id") == col_hash(" ID ")
    assert [t.cells(r) for r in t.rows] == [tuple(r) for r in VANILLA]
    assert roundtrip_ok(data)
    assert gff_serialize(gff_parse(data)) == data      # byte-identical for our own layout
    assert t.col_name(2).startswith("col2#")           # unknown names shown as hashes


def test_merge_cell_level():
    van = build(COLS, VANILLA)
    a = build(COLS, _rows((1, 2, 40)))                          # fireball mana 30 -> 40
    b = build(COLS, _rows((2, 3, 12.0), add=[[100, "aw_aura", 0, 0.0, 0]], drop=[3]))
    c = build(COLS, _rows((1, 2, 35)), version="V4.1")          # also fireball mana, different layout
    bad = build(COLS[:4], [r[:4] for r in VANILLA])             # different columns
    r = merge_tables("abi_base.gda", van, [("A", a), ("B", b), ("C", c), ("Bad", bad)])
    assert r.error is None and r.mods_merged == 3
    out = {t[0]: t for t in (Gda.from_bytes(r.data).cells(x) for x in Gda.from_bytes(r.data).rows)}
    assert out[1][2] == 35                  # C loads after A on the same cell -> wins
    assert out[2][3] == 12.0                # B's change survives: A and C left it at vanilla
    assert out[3] == tuple(VANILLA[2])      # B dropped row 3 -> kept anyway
    assert out[100][1] == "aw_aura"         # added row
    assert [(x.row_id, x.earlier_mod, x.later_mod) for x in r.conflicts] == [(1, "A", "C")]
    d = {x.mod: x for x in r.diffs}
    assert d["B"].missing_rows == 1 and d["B"].added_rows == 1 and d["B"].changed_cells == 1
    assert d["Bad"].skipped.startswith("different columns")


def test_merge_same_change_is_not_a_conflict():
    van = build(COLS, VANILLA)
    same = build(COLS, _rows((1, 2, 40)))
    r = merge_tables("t.gda", van, [("A", same), ("B", same)])
    assert r.conflicts == []


def test_merge_pipeline(tmp_path, capsys):
    data = tmp_path / "game" / "data"
    data.mkdir(parents=True)
    write_erf_v3(data / "2da.erf", [("abi_base.gda", build(COLS, VANILLA))])
    root = tmp_path / "override"
    (root / "ManaTweaks").mkdir(parents=True)
    (root / "ManaTweaks" / "abi_base.gda").write_bytes(build(COLS, _rows((1, 2, 40))))
    (root / "CooldownMod").mkdir()
    write_erf_v3(root / "CooldownMod" / "cd.erf", [("abi_base.gda", build(COLS, _rows((2, 3, 5.0))))])
    db = str(tmp_path / "i.db")
    args = ["--root", str(root), "--vanilla", str(data), "--db", db]

    assert main(["merge", *args]) == 0                        # preview
    assert not (root / "~da2lm_merged").exists()
    assert main(["merge", *args, "--yes"]) == 0
    merged = root / "~da2lm_merged" / "abi_base.gda"
    t = Gda.from_bytes(merged.read_bytes())
    vals = {r[0]: r for r in (t.cells(x) for x in t.rows)}
    assert vals[1][2] == 40 and vals[2][3] == 5.0             # both mods' changes present

    assert "~da2lm_merged" not in [m.folder for m in lo.current(root)]   # never renumbered
    assert main(["scan", "--root", str(root), "--vanilla", str(data), "--db", db]) == 0
    capsys.readouterr()
    assert main(["report", "--db", db]) == 0                  # critical is resolved now
    out = capsys.readouterr().out
    assert "[merged by da2lm]" in out
    assert main(["gda", "check", str(merged)]) == 0
    assert main(["gda", "dump", str(data / "2da.erf"), "--entry", "abi_base.gda"]) == 0
    assert main(["merge", "--root", str(root), "--clear"]) == 0
    assert not (root / "~da2lm_merged").exists()


def test_rim_vanilla_and_dlc(tmp_path):
    core = tmp_path / "DA2" / "packages" / "core" / "data"
    dlc = tmp_path / "DA2" / "addins" / "da2_prc_seb" / "core" / "data"
    core.mkdir(parents=True)
    dlc.mkdir(parents=True)
    write_erf_v3(core / "2da.rim", [("abi_base.gda", build(COLS, VANILLA))])
    write_erf_v3(dlc / "2da.erf", [("abi_base_seb.gda", b"x")])
    root = tmp_path / "override"
    (root / "M").mkdir(parents=True)
    write_erf_v3(root / "M" / "stuff.rim", [("abi_base.gda", build(COLS, _rows((1, 2, 99))))])
    db = str(tmp_path / "i.db")
    assert main(["scan", "--root", str(root), "--vanilla", str(core), "--vanilla", str(dlc), "--db", db]) == 0
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM vanilla").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM resources WHERE name='abi_base.gda'").fetchone()[0] == 1
    assert [r.key for r in replacements(con)] == ["abi_base.gda"]
    assert main(["gda", "check", str(core / "2da.rim"), "--entry", "abi_base.gda"]) == 0


def test_merge_ignores_string_terminator_and_empty_conventions():
    nul = [[r[0], r[1] + "\x00"] + r[2:] for r in VANILLA]      # DA2 style
    van = build(COLS, nul)
    plain = build(COLS, _rows((2, 2, 21)))                      # GDApp style, one real change
    r = merge_tables("abi_base.gda", van, [("A", plain), ("B", build(COLS, nul))])
    d = {x.mod: x for x in r.diffs}
    assert d["A"].changed_cells == 1 and d["B"].changed_cells == 0 and r.conflicts == []
    out = Gda.from_bytes(r.data)
    assert out.cells(out.rows[0])[1] == "fireball\x00"        # vanilla convention kept
    assert out.cells(out.rows[1])[2] == 21


def test_extract_and_pack(tmp_path):
    game = tmp_path / "core" / "textures"
    game.mkdir(parents=True)
    write_erf_v3(game / "gui.erf", [("ico_heal.dds", b"DDS heal"), ("other.dds", b"x")])
    out = tmp_path / "out"
    assert main(["extract", "ICO_HEAL.dds", "--from", str(tmp_path / "core"), "-o", str(out)]) == 0
    assert (out / "ico_heal.dds").read_bytes() == b"DDS heal"
    assert main(["extract", "nope.dds", "--from", str(tmp_path / "core")]) == 1
    erf = tmp_path / "t.erf"
    assert main(["pack", str(erf), str(out / "ico_heal.dds")]) == 0
    e = read_erf(erf)
    assert e.hash_variant == "fnv1" and e.read(e.entries[0]) == b"DDS heal"


def test_find_and_extract_unnamed(tmp_path, capsys):
    d = tmp_path / "core"
    d.mkdir()
    write_erf_v3(d / "gui.erf", [("ico_health_1.dds", b"named"), ("hidden_icon.dds", b"hashonly")],
                 unnamed={"hidden_icon.dds"})
    assert main(["find", "HEALTH", "--from", str(d)]) == 0
    out = capsys.readouterr().out
    assert "ico_health_1.dds" in out and "1 entries have no stored name" in out
    assert main(["extract", "hidden_icon.dds", "--from", str(d), "-o", str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / "hidden_icon.dds").read_bytes() == b"hashonly"


def test_hash_only_entries_match_named_files(tmp_path, capsys):
    """DA2 game archives store most entries as hash only. A mod's loose ico_x.dds must
    match the game's hash-only copy, and another mod's hash-only copy."""
    game = tmp_path / "game"
    game.mkdir()
    write_erf_v3(game / "textures.erf", [("ico_health_1.dds", b"vanilla")], unnamed={"ico_health_1.dds"})
    root = tmp_path / "override"
    (root / "LooseIcons").mkdir(parents=True)
    (root / "LooseIcons" / "ICO_Health_1.dds").write_bytes(b"red")
    (root / "PackedIcons").mkdir()
    write_erf_v3(root / "PackedIcons" / "p.erf", [("ico_health_1.dds", b"blue")], unnamed={"ico_health_1.dds"})
    db = str(tmp_path / "i.db")
    assert main(["scan", "--root", str(root), "--vanilla", str(game), "--db", db]) == 0
    con = connect(db)
    (c,) = conflicts(con)
    assert c.key == "ico_health_1.dds" and c.verdict == "real"     # name recovered from the loose copy
    assert [x.mod for x in c.copies] == ["PackedIcons", "LooseIcons"]   # loose tier wins
    reps = replacements(con)
    assert [(r.key, r.mods) for r in reps] == [("ico_health_1.dds", ["PackedIcons", "LooseIcons"])]
    assert main(["extract", "ico_health_1.dds", "--from", str(game), "-o", str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / "ico_health_1.dds").read_bytes() == b"vanilla"


def test_old_index_is_rebuilt(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript("CREATE TABLE resources (id INTEGER PRIMARY KEY, key TEXT);"
                      "CREATE TABLE vanilla (key TEXT PRIMARY KEY, erf TEXT);")
    old.close()
    con = connect(db)
    assert "name" in {r[1] for r in con.execute("PRAGMA table_info(resources)")}


def test_bioware_zlib_framing():
    data = b"dragon age 2 " * 100
    c = zlib.compressobj(wbits=-15)
    framed = b"\xf9" + c.compress(data) + c.flush()
    from da2lm.erf import _decompress
    assert _decompress(framed, len(data)) == data


def test_loose_beats_archive_across_folders(tmp_path):
    """In-game tests B3/B4: a loose file wins over any archived copy, even one in a
    folder that sorts later or an archive whose name sorts later."""
    root = tmp_path / "override"
    (root / "A_early").mkdir(parents=True)
    (root / "A_early" / "x.dds").write_bytes(b"loose")
    (root / "zz_late").mkdir()
    write_erf_v3(root / "zz_late" / "zzz.erf", [("x.dds", b"archived")])
    (c,) = conflicts(scan(tmp_path / "i.db", [root], log=lambda m: None))
    assert c.winner.mod == "A_early" and c.winner.inner_name is None
    # two loose copies: later path wins
    (root / "zz_late" / "x.dds").write_bytes(b"loose2")
    (c,) = conflicts(scan(tmp_path / "i.db", [root], log=lambda m: None))
    assert c.winner.mod == "zz_late" and c.winner.inner_name is None


def test_full_replacement_classified(tmp_path, capsys):
    """EYP-style: a whole table that only deletes rows is a WARNING, not CRITICAL;
    one that overrides values is CRITICAL."""
    data = tmp_path / "game"
    data.mkdir()
    write_erf_v3(data / "2da.rim", [("itm_base.gda", build(COLS, VANILLA)), ("other_t.gda", build(COLS, VANILLA))])
    root = tmp_path / "override"
    (root / "EYP").mkdir(parents=True)
    (root / "EYP" / "itm_base.gda").write_bytes(build(COLS, _rows(drop=[2])))
    (root / "Bad").mkdir()
    (root / "Bad" / "other_t.gda").write_bytes(build(COLS, _rows((1, 2, 99))))
    db = str(tmp_path / "i.db")
    main(["scan", "--root", str(root), "--vanilla", str(data), "--db", db])
    capsys.readouterr()
    assert main(["report", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "[WARNING ] itm_base.gda" in out and "deletes 1 rows: 2" in out
    assert "[OVERRIDE] other_t.gda" in out and "overrides values in 1 rows: 1" in out
    # only the deleting table -> report passes
    import shutil; shutil.rmtree(root / "Bad")
    main(["scan", "--root", str(root), "--vanilla", str(data), "--db", db])
    assert main(["report", "--db", db]) == 0


# ----------------------------------------------------------------------------- row overlaps
from da2lm.rows import analyze as rows_analyze
from da2lm.merge import _read_resource

EL = [("ID", "int"), ("Label", "string"), ("Slots", "int"), ("Locked", "int")]


def _m2da_base(prefixes):
    return build([("ID", "int"), ("Label", "string"), ("Prefix", "string"), ("X", "int")],
                 [[i, p, p, 0] for i, p in enumerate(prefixes)])


def test_row_overlaps_like_dfa_eyp_tallis(tmp_path, capsys):
    data = tmp_path / "DA2" / "packages" / "core" / "data"
    dlc = tmp_path / "DA2" / "addins" / "da2_prc_eye" / "core" / "data"
    data.mkdir(parents=True); dlc.mkdir(parents=True)
    write_erf_v3(data / "2da.rim", [
        ("m2da_base.gda", _m2da_base(["EquipmentLayout"])),
        ("equipmentlayout.gda", build(EL, [[0, "DEFAULT", 16375, 0], [1, "anders", 1064711, 1], [2, "aveline", 1064711, 1]]))])
    write_erf_v3(dlc / "2da.erf", [("equipmentlayout_eye.gda", build(EL, [[10, "tallis", 1064711, 1]]))])
    root = tmp_path / "override"
    for d in ("DFA", "EYP", "Other"):
        (root / d).mkdir(parents=True)
    (root / "DFA" / "equipmentlayout.gda").write_bytes(
        build(EL, [[0, "DEFAULT", 16375, 0], [1, "anders", 16375, 0], [2, "aveline", 16375, 0], [0, None, 0, 0]]))
    (root / "EYP" / "equipmentlayout_eyp_clean.gda").write_bytes(
        build(EL, [[1, "anders", 16375, 0], [2, "aveline", 16375, 0], [10, "tallis", 16375, 0]]))
    (root / "Other" / "equipmentlayout_other.gda").write_bytes(build(EL, [[1, "anders", 999, 0]]))
    db = str(tmp_path / "i.db")
    main(["scan", "--root", str(root), "--vanilla", str(tmp_path / "DA2"), "--db", db])
    reps = rows_analyze(connect(db), [str(tmp_path / "DA2")], _read_resource)
    (rep,) = reps
    ov = {o.row_id: o for o in rep.overlaps}
    assert ov[2].agree                                             # DFA and EYP set the same values
    assert not ov[1].agree and ov[1].confidence.startswith("confirmed")   # EYP vs Other: two mod parts
    assert ov[1].providers[-1][0].source == "Other"               # later path (load order) predicted to win
    assert not ov[10].agree and ov[10].providers[-1][0].name == "equipmentlayout_eye.gda"
    assert ov[10].confidence == "confirmed"                        # DLC row beats the mod's suffixed row
    assert ov[10].kind == "overridden" and ov[1].kind == "conflict" and ov[2].kind == "identical"
    assert 0 not in ov or ov[0].kind != "conflict"                 # DFA's repeated row 0 is not a conflict
    assert any("repeats row IDs [0]" in n for n in rep.notes)
    capsys.readouterr()
    assert main(["rows", "--db", db]) == 2
    out = capsys.readouterr().out
    assert "identical values" in out and "row 10" in out and "(confirmed)" in out


def test_within_mod_optional_folder(tmp_path, capsys):
    root = tmp_path / "override"
    (root / "Arsenal" / "Final").mkdir(parents=True)
    (root / "Arsenal" / "Optional files" / "Default icons").mkdir(parents=True)
    (root / "Arsenal" / "Nerfs").mkdir(parents=True)
    write_erf_v3(root / "Arsenal" / "Final" / "m2.erf", [("sword.uti", b"op"), ("icon.dds", b"custom")])
    write_erf_v3(root / "Arsenal" / "Optional files" / "Default icons" / "m2.erf",
                 [("sword.uti", b"op2"), ("icon.dds", b"default")])
    (root / "Arsenal" / "Nerfs" / "sword.uti").write_bytes(b"nerfed")
    db = str(tmp_path / "i.db")
    main(["scan", "--root", str(root), "--db", db])
    capsys.readouterr()
    main(["report", "--db", db])
    out = capsys.readouterr().out
    assert "OVERRIDES INSIDE A SINGLE MOD FOLDER" in out
    assert "Nerfs\\*.uti (loose)" in out.replace("/", "\\") and "Optional files" in out


def test_paths_detection_and_saved_fallback(tmp_path, monkeypatch, capsys):
    from da2lm import paths
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(paths, "game_candidates", lambda: [tmp_path / "nope"])
    assert paths.find_game() is None
    assert main(["where"]) == 1 and "NOT FOUND" in capsys.readouterr().out
    game = tmp_path / "My Games" / "DA2"
    (game / "packages" / "core" / "data").mkdir(parents=True)
    write_erf_v3(game / "packages" / "core" / "data" / "2da.rim", [])
    (game / "addins").mkdir()
    assert main(["where", "--set-game", str(tmp_path / "nope")]) == 1      # rejected: not a DA2 install
    assert main(["where", "--set-game", str(game)]) == 0                   # saved and found next time
    assert paths.find_game() == game
    assert [d.name for d in paths.vanilla_dirs(game)] == ["data", "addins"]
    ov = tmp_path / "ov"; ov.mkdir()
    assert main(["where", "--set-override", str(ov)]) == 0
    assert paths.find_override() == ov


def test_mod_changing_game_rows_is_not_a_conflict(tmp_path):
    data = tmp_path / "DA2" / "packages" / "core" / "data"
    data.mkdir(parents=True)
    write_erf_v3(data / "2da.rim", [("m2da_base.gda", _m2da_base(["item_variations"])),
                                    ("item_variations.gda", build(EL, [[0, "a", 1, 1], [5, "b", 1, 1]]))])
    root = tmp_path / "override"; (root / "DFA").mkdir(parents=True)
    (root / "DFA" / "item_variations_ishdfa.gda").write_bytes(build(EL, [[5, "b", 2, 1], [900, "new", 1, 1]]))
    db = str(tmp_path / "i.db")
    main(["scan", "--root", str(root), "--vanilla", str(tmp_path / "DA2"), "--db", db])
    (rep,) = rows_analyze(connect(db), [str(tmp_path / "DA2")], _read_resource)
    assert rep.level == "OVERRIDE" and [o.kind for o in rep.overlaps] == ["changes"]


def test_base_copy_and_patch_revert_and_redundancy(tmp_path, capsys):
    G = tmp_path / "DA2"
    data, patch = G / "packages" / "core" / "data", G / "packages" / "core" / "patch"
    data.mkdir(parents=True); patch.mkdir(parents=True)
    base = [[i, f"r{i}", 1, 0] for i in range(30)]
    write_erf_v3(data / "2da.rim", [("m2da_base.gda", _m2da_base(["rewards", "EquipmentLayout"])),
                                    ("rewards.gda", build(EL, base)),
                                    ("equipmentlayout.gda", build(EL, [[1, "a", 5, 1], [2, "b", 5, 1]]))])
    write_erf_v3(patch / "patch.erf", [("rewards_patch.gda", build(EL, [[3, "r3", 9, 0]]))])
    root = tmp_path / "override"
    for d in ("Arsenal", "DFA", "EYP"):
        (root / d).mkdir(parents=True)
    copy = [list(r) for r in base]
    copy[5][2] = 2                                            # one real change; row 3 keeps pre-patch value
    (root / "Arsenal" / "rewards_arsenal.gda").write_bytes(build(EL, copy))
    (root / "DFA" / "equipmentlayout.gda").write_bytes(build(EL, [[1, "a", 7, 0], [2, "b", 7, 0]]))
    (root / "EYP" / "equipmentlayout_eyp.gda").write_bytes(build(EL, [[1, "a", 7, 0], [2, "b", 7, 0]]))
    db = str(tmp_path / "i.db")
    main(["scan", "--root", str(root), "--vanilla", str(G), "--db", db])
    capsys.readouterr()
    assert main(["report", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "carries 30 of the 30 base rows" in out and "patch changed: 3;" in out
    assert "[OVERRIDE] equipmentlayout.gda" in out
    assert "redundant with EYP: same values in 2 of its 2 changed rows (no effect while both are installed)" in out


# ----------------------------------------------------------------------------- chargen
from da2lm import chargen as cg

GAME_CG = """<?xml version="1.0" encoding="utf-8"?>
<morph_config>
    <hairs>
        <human_male>
            <resource name="art\\hm_har_blda" cut="0"/>
            <resource name="art\\hm_har_blda" cut="2"/>
        </human_male>
    </hairs>
    <beards><human_male><resource name=""/> <!-- no beard --></human_male></beards>
    <hair_colors>
        <resource name ="t3_har_wht"/>
    </hair_colors>
</morph_config>"""


def test_chargen_parse_merge_render():
    hair_mod = GAME_CG.replace('<resource name ="t3_har_wht"/>',
                               '<resource name ="t3_har_wht"/><resource name="T3_HAR_PNK"/>')
    hair_mod = hair_mod.replace('cut="2"/>', 'cut="2"/><resource name="art\\hm_har_new" cut="1"/>')
    old_copy = "<morph_config><hair_colors><resource name='x'/><resource name=\"t3_har_pnk\"/></hair_colors>" \
               "<eyes_colors><resource name=\"t3_hey_new\"/></eyes_colors></morph_config>"
    out = cg.merge(GAME_CG, [("HairMod", hair_mod), ("Old", old_copy)])
    c = out.config
    assert [e.name for e in c.sections["hair_colors"]] == ["t3_har_wht", "T3_HAR_PNK", "x"]   # case-insensitive dedupe
    assert [e.name for e in c.sections["hairs/human_male"]][-1] == "art\\hm_har_new"
    assert c.sections["beards/human_male"][0].name == ""                                  # 'no beard' kept
    r = {x.mod: x for x in out.results}
    assert r["HairMod"].n_added == 2 and r["Old"].missing_from_game >= 3                   # old copy: nothing removed
    assert "eyes_colors" in r["Old"].new_sections
    again = cg.parse(cg.render(c))
    assert again.order == c.order and not again.errors
    assert [e.key for e in again.sections["hairs/human_male"]] == [e.key for e in c.sections["hairs/human_male"]]


def test_chargen_cli_and_merge_folder_coexist(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    data = tmp_path / "DA2" / "packages" / "core" / "data"
    data.mkdir(parents=True)
    write_erf_v3(data / "misc.erf", [("chargenmorphcfg.xml", GAME_CG.encode())])
    root = tmp_path / "override"
    (root / "Hair").mkdir(parents=True); (root / "Eyes").mkdir()
    (root / "Hair" / "chargenmorphcfg.xml").write_text(GAME_CG.replace("t3_har_wht", "t3_har_wht\"/><resource name=\"t3_har_pnk"))
    write_erf_v3(root / "Eyes" / "eyes.erf", [("chargenmorphcfg.xml", b"<morph_config><eyes_colors><resource name='t3_hey_x'/></eyes_colors></morph_config>")])
    db = str(tmp_path / "i.db")
    main(["scan", "--root", str(root), "--vanilla", str(tmp_path / "DA2"), "--db", db])
    capsys.readouterr()
    assert main(["chargen", "--root", str(root), "--db", db, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Hair: adds 1 entries" in out and "Eyes:" in out
    merged = (root / "~da2lm_merged" / "chargenmorphcfg.xml").read_text()
    assert "t3_har_pnk" in merged and "t3_hey_x" in merged
    # table merge clearing must not delete the chargen file
    from da2lm import merge as mg
    mg.clear(root)
    assert (root / "~da2lm_merged" / "chargenmorphcfg.xml").exists()
    # exclusion is remembered
    assert main(["chargen", "--root", str(root), "--db", db, "--exclude", "Eyes", "--yes"]) == 0
    assert "t3_hey_x" not in (root / "~da2lm_merged" / "chargenmorphcfg.xml").read_text()
    # chargen is not listed among general conflicts
    main(["scan", "--root", str(root), "--vanilla", str(tmp_path / "DA2"), "--db", db])
    capsys.readouterr(); main(["report", "--db", db, "--all"])
    assert "chargenmorphcfg" not in capsys.readouterr().out


def test_scan_cache_reuses_unchanged_files(tmp_path):
    import os
    root = tmp_path / "override"
    (root / "A").mkdir(parents=True); (root / "B").mkdir()
    write_erf_v3(root / "A" / "a.erf", [("x.dds", b"1" * 50), ("y.gda", b"2" * 10)])
    (root / "B" / "x.dds").write_bytes(b"3" * 20)
    (root / "B" / "z.erf").write_bytes(b"broken")
    db = tmp_path / "i.db"
    q = "SELECT m.name,r.key,r.name,r.ext,r.file,r.inner_name,r.size,r.sha1,r.status,r.tier,r.rank FROM resources r JOIN mods m ON m.id=r.mod_id ORDER BY rank"
    con = scan(db, [root], log=lambda *_: None)
    first = con.execute(q).fetchall(); erfs1 = con.execute("SELECT * FROM erfs ORDER BY path").fetchall()
    con.close()
    logs = []
    con = scan(db, [root], log=logs.append)
    assert con.execute(q).fetchall() == first
    assert con.execute("SELECT * FROM erfs ORDER BY path").fetchall() == erfs1
    assert any("2 unchanged files" in l for l in logs)          # the broken erf is re-read every time
    con.close()
    p = root / "B" / "x.dds"
    p.write_bytes(b"4" * 20)
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 5_000_000_000))
    (root / "A" / "a.erf").unlink()
    con = scan(db, [root], log=lambda *_: None)
    rows = con.execute(q).fetchall()
    assert [r[7] for r in rows if r[4].endswith("x.dds")] == [_sha1(b"4" * 20)]
    assert con.execute("SELECT COUNT(*) FROM cache_files WHERE path LIKE '%a.erf'").fetchone()[0] == 0


# ----------------------------------------------------------------------------- overview (window model)
from da2lm import overview as ov


def test_overview_plain_issues_and_layout(tmp_path):
    data = tmp_path / "game" / "data"
    data.mkdir(parents=True)
    write_erf_v3(data / "2da.erf", [("abi_base.gda", build(COLS, VANILLA))])
    root = tmp_path / "override"
    (root / "ManaTweaks").mkdir(parents=True)
    (root / "ManaTweaks" / "abi_base.gda").write_bytes(build(COLS, _rows((1, 2, 40))))
    (root / "ManaTweaks" / "fx.ncs").write_bytes(b"A")
    (root / "CooldownMod").mkdir()
    write_erf_v3(root / "CooldownMod" / "cd.erf", [("abi_base.gda", build(COLS, _rows((2, 3, 5.0))))])
    (root / "CooldownMod" / "fx.ncs").write_bytes(b"B")
    (root / "Textures").mkdir()
    (root / "Textures" / "a.dds").write_bytes(b"1")
    (root / "ZTextures").mkdir()
    (root / "ZTextures" / "a.dds").write_bytes(b"2")
    con = scan(tmp_path / "i.db", [root], vanilla=[data], log=lambda *_: None)
    issues = ov.build(con)
    by = {i.id: i for i in issues}
    m = by["merge:abi_base.gda"]
    assert m.severity == ov.ATTENTION and m.action == "merge" and m.winner == "ManaTweaks"
    assert "abilities table" in m.title
    f = by["files:CooldownMod|ManaTweaks"]
    assert f.severity == ov.ATTENTION and "script" in f.title and f.winner == "ManaTweaks"
    t = by["files:Textures|ZTextures"]
    assert t.severity == ov.INFO and t.title.endswith("the same texture")
    assert set(ov.mod_badges(issues)) == {"ManaTweaks", "CooldownMod"}
    # marking as intended moves it out of "needs a look"
    accepted = ov.build(con, accepted=[f.id])
    assert f.id not in {i.id for i in ov.needs_look(accepted)}
    con.close()

    # turn a mod off and reorder in one step; nothing is deleted
    wanted = [("ZTextures", True), ("Textures", False), ("CooldownMod", True), ("ManaTweaks", True)]
    assert any("Turn off: Textures" in l for l in lo.preview_layout(root, wanted))
    done = lo.apply_layout(root, wanted, log=lambda *_: None)
    assert done["disabled"] == ["Textures"]
    assert (lo.disabled_dir(root) / "Textures" / "a.dds").exists()
    assert [m.name for m in lo.current(root)] == ["ZTextures", "CooldownMod", "ManaTweaks"]
    lo.apply_layout(root, [("Textures", True), ("ZTextures", True), ("CooldownMod", True), ("ManaTweaks", True)],
                    log=lambda *_: None)
    assert [m.name for m in lo.current(root)][0] == "Textures" and not lo.disabled(root)


def test_chargen_open_close_resource_tags():
    c = cg.parse("<morph_config><skin_colors><resource name=\"a\"></resource>\n<resource name='b'></resource>"
                 "</skin_colors></morph_config>")
    assert [e.name for e in c.sections["skin_colors"]] == ["a", "b"] and not c.errors


# ----------------------------------------------------------------------------- row merge (multi-part tables)
from da2lm import rowmerge
from da2lm import merge as mg_mod

AB = [("ID", "int"), ("Label", "string"), ("Icon", "string"), ("Level", "int"), ("PosX", "int"), ("Cost", "float")]


def test_rowmerge_combines_different_columns_like_stimt_and_susi(tmp_path, capsys, monkeypatch):
    from da2lm.gda import col_hash
    monkeypatch.setitem(rowmerge.SAFE_COLUMNS, "abi_", {col_hash(n) for n in ("Icon", "Level", "PosX", "Cost")})
    data = tmp_path / "DA2" / "packages" / "core" / "data"
    dlc = tmp_path / "DA2" / "addins" / "da2_prc_x" / "core" / "data"
    data.mkdir(parents=True); dlc.mkdir(parents=True)
    write_erf_v3(data / "2da.rim", [
        ("m2da_base.gda", _m2da_base(["ABI_"])),
        ("abi_base.gda", build(AB, [[1, "bodyguard", "guard", 8, 250, 15.0], [2, "shout", "shout", 4, 100, 20.0],
                                    [3, "dlcrow", "x", 1, 1, 1.0]]))])
    write_erf_v3(dlc / "2da.erf", [("abi_x.gda", build(AB, [[3, "dlcrow", "dlc", 1, 1, 1.0]]))])
    root = tmp_path / "override"
    for d in ("STiMT", "Susi"):
        (root / d).mkdir(parents=True)
    write_erf_v3(root / "STiMT" / "g.erf", [("abi_guardian.gda", build(AB, [
        [1, "bodyguard", "sarth", 8, 375, 15.0], [2, "shout", "shout", 4, 100, 10.0], [3, "dlcrow", "a", 1, 1, 1.0]]))])
    (root / "Susi" / "abi_ave.gda").write_bytes(build(AB, [
        [1, "bodyguard", "guard", 0, 250, 15.0], [2, "shout", "shout", 0, 100, 5.0], [3, "dlcrow", "b", 1, 1, 1.0]]))
    db = tmp_path / "i.db"
    vd = [tmp_path / "DA2" / "packages" / "core" / "data", tmp_path / "DA2" / "addins"]
    res = rowmerge.run(root, vd, db, apply=True, log=lambda *_: None)
    (fm,) = res
    rows = {r.row_id: r for r in fm.rows}
    assert rows[1].cells[2:5] == ("sarth", 0, 375) and not rows[1].clashes        # both mods' changes kept
    assert rows[2].cells[3] == 0 and rows[2].cells[5] == 5.0                       # Susi loads later: its cost
    assert rows[2].clashes and rows[2].clashes[0][1] == [("STiMT", 10.0), ("Susi", 5.0)]
    assert 3 not in rows and any(rid == 3 for rid, _ in fm.skipped)                # DLC row wins anyway
    out = root / "~da2lm_merged" / "abi_da2lm_rows.gda"
    assert out.exists() and Gda.from_bytes(out.read_bytes()).index().keys() == {1, 2}
    # the table merge never deletes the combined rows
    mg_mod.clear(root)
    assert out.exists()
    # the window's issue list sees the combined rows as merged
    con = scan(db, [root], vanilla=vd, log=lambda *_: None)
    issues = ov.build(con)
    assert any(i.id == "rowmerge:abi_" and i.severity == ov.INFO for i in issues)
    assert not any(i.id.startswith("rows:abi_") for i in issues)
    con.close()
    assert main(["merge", "--root", str(root), "--clear"]) == 0
    assert not out.exists()
    # before merging, the issue offers to combine
    con = scan(db, [root], vanilla=vd, log=lambda *_: None)
    i = next(i for i in ov.build(con) if i.id.startswith("rows:abi_"))
    assert i.action == "merge" and i.choices[0]["do"] == "merge" and i.choices[0]["best"]
    assert {c["mod"] for c in i.choices if c["do"] == "last"} == {"STiMT", "Susi"}


def test_appearance_rows_never_combined_and_dfa_rule(tmp_path):
    from da2lm import known
    APR = [("ID", "int"), ("Label", "string"), ("Model", "string"), ("Head", "string")]
    data = tmp_path / "DA2" / "packages" / "core" / "data"
    data.mkdir(parents=True)
    write_erf_v3(data / "2da.rim", [("m2da_base.gda", _m2da_base(["APR_"])),
                                    ("apr_base.gda", build(APR, [[1000, "Follower - Anders", "m0", "h0"]]))])
    root = tmp_path / "override"
    (root / "zz_EYP Clean").mkdir(parents=True)
    (root / "apr_base_ishdfa.gda").write_bytes(build(APR, [[1000, "Follower - Anders", "dfa_model", "h0"]]))
    (root / "zz_EYP Clean" / "apr_base_eyp_clean.gda").write_bytes(build(APR, [[1000, "Follower - Anders", "m0", "eyp_head"]]))
    vd = [data]
    res = rowmerge.run(root, vd, tmp_path / "i.db", apply=True, log=lambda *_: None)
    assert not any(f.rows for f in res)                                    # never mixed column by column
    assert any("kept whole" in why for f in res for _, why in f.skipped)
    con = scan(tmp_path / "i.db", [root], vanilla=vd, log=lambda *_: None)
    issues = {i.id: i for i in ov.build(con)}
    rows = next(i for k, i in issues.items() if k.startswith("rows:apr_"))
    assert rows.action == "order" and "Follower - Anders" in " ".join(rows.text)
    rule = issues["known:dfa-apr-with-eyp"]
    assert rule.action == "setaside" and rule.files and "readme" in " ".join(rule.text)
    con.close()
    moved = known.set_aside(root, Path(rule.files[0]))
    assert moved.exists() and not (root / "apr_base_ishdfa.gda").exists()


def test_merged_files_fingerprint_detects_changed_sources(tmp_path):
    from da2lm import outputs
    root = tmp_path / "override"
    (root / "A").mkdir(parents=True)
    (root / "A" / "abi_a.gda").write_bytes(build(AB, [[1, "x", "i", 1, 1, 1.0]]))
    db = tmp_path / "i.db"
    con = scan(db, [root], log=lambda *_: None)
    assert outputs.is_stale(root, con) is None                            # nothing merged yet
    (root / "~da2lm_merged").mkdir()
    (root / "~da2lm_merged" / "abi_da2lm_rows.gda").write_bytes(b"x")
    assert outputs.is_stale(root, con) is True                            # no stamp: treat as stale
    outputs.stamp(root, con)
    assert outputs.is_stale(root, con) is False
    con.close()
    (root / "A" / "abi_a.gda").write_bytes(build(AB, [[1, "x", "i", 0, 1, 1.0]]))   # a source changed
    con = scan(db, [root], log=lambda *_: None)
    assert outputs.is_stale(root, con) is True


def test_rowmerge_keeps_ability_whole_when_two_mods_change_behaviour(tmp_path, monkeypatch):
    from da2lm.gda import col_hash
    monkeypatch.setitem(rowmerge.SAFE_COLUMNS, "abi_", {col_hash(n) for n in ("Icon", "Level")})
    data = tmp_path / "DA2" / "packages" / "core" / "data"
    data.mkdir(parents=True)
    write_erf_v3(data / "2da.rim", [("m2da_base.gda", _m2da_base(["ABI_"])),
                                    ("abi_base.gda", build(AB, [[1, "x", "i", 8, 250, 15.0]]))])
    root = tmp_path / "override"
    (root / "A").mkdir(parents=True); (root / "B").mkdir()
    (root / "A" / "abi_a.gda").write_bytes(build(AB, [[1, "x", "i", 0, 300, 15.0]]))   # PosX: not a safe column
    (root / "B" / "abi_b.gda").write_bytes(build(AB, [[1, "x", "j", 8, 250, 5.0]]))    # Cost: not safe either
    res = rowmerge.run(root, [data], tmp_path / "i.db", apply=False, log=lambda *_: None)
    (fm,) = res
    assert not fm.rows and "belong together" in fm.skipped[0][1]


def test_same_mod_installed_twice(tmp_path):
    root = tmp_path / "override"
    for d in ("EVP CLEAN", "zz_EYP Clean", "Other"):
        (root / d).mkdir(parents=True)
    for n in ("apr_base_eyp_clean.gda", "equipmentlayout_eye.gda", "rewards_eyp_clean.gda"):
        (root / "EVP CLEAN" / n).write_bytes(b"old " + n.encode())
        (root / "zz_EYP Clean" / n).write_bytes((b"new " if n.startswith("apr") else b"old ") + n.encode())
    (root / "Other" / "rewards_eyp_clean.gda").write_bytes(b"x")
    (root / "Other" / "a.dds").write_bytes(b"1"); (root / "Other" / "b.dds").write_bytes(b"1")
    con = scan(tmp_path / "i.db", [root], log=lambda *_: None)
    dups = [i for i in ov.build(con) if i.id.startswith("dup:")]
    assert [i.id for i in dups] == ["dup:EVP CLEAN|zz_EYP Clean"]
    assert dups[0].severity == ov.ATTENTION and "apr_base_eyp_clean.gda" in " ".join(dups[0].text)


def test_rewards_items_stay_one_list_gold_combines(tmp_path):
    RW = [("ID", "int"), ("Label", "string"), ("Gold", "int"), ("XP", "int")] + [(f"item{i}", "string") for i in range(1, 7)]
    data = tmp_path / "DA2" / "packages" / "core" / "data"
    data.mkdir(parents=True)
    write_erf_v3(data / "2da.rim", [("m2da_base.gda", _m2da_base(["REWARDS"])),
                                    ("rewards.gda", build(RW, [[1, "dragon", 0, 0, None, None, None, None, None, None],
                                                              [2, "quest", 1, 0, "a", None, None, None, None, None]]))])
    root = tmp_path / "override"
    for d in ("Arsenal", "Staff", "Zbooks"):
        (root / d).mkdir(parents=True)
    (root / "Arsenal" / "rewards_ars.gda").write_bytes(build(RW, [[1, "dragon", 2, 0, None, None, None, None, None, None],
                                                                 [2, "quest", 3, 0, "a", None, None, None, None, None]]))
    (root / "Staff" / "rewards_staff.gda").write_bytes(build(RW, [[1, "dragon", 7, 0, "staff", None, None, None, None, None]]))
    (root / "Zbooks" / "rewards_books.gda").write_bytes(build(RW, [[2, "quest", 1, 50, "a", "tome", None, None, None, None]]))
    (fm,) = rowmerge.run(root, [data], tmp_path / "i.db", apply=False, log=lambda *_: None)
    rows = {r.row_id: r for r in fm.rows}
    assert rows[1].cells[2] == 7 and rows[1].cells[4] == "staff"            # Staff loads after Arsenal: its gold
    assert rows[1].clashes and rows[1].clashes[0][0] == "Gold"
    assert rows[2].cells[2:6] == (3, 50, "a", "tome") and not rows[2].clashes  # gold from one, XP + list from the other


# ----------------------------------------------------------------------------- library + deployment
from da2lm import manager as mm


def test_manager_takes_over_orders_without_renaming_and_undoes(tmp_path):
    import os
    app = tmp_path / "app"
    root = tmp_path / "override"
    (root / "Zeta Mod").mkdir(parents=True); (root / "Alpha Mod").mkdir()
    (root / "Zeta Mod" / "a.dds").write_bytes(b"zeta"); (root / "Zeta Mod" / "readme.txt").write_text("hi")
    (root / "Alpha Mod" / "a.dds").write_bytes(b"alpha")
    z = mm.manage(root, "Zeta Mod", base=app)
    a = mm.manage(root, "Alpha Mod", base=app)
    assert not (root / "Zeta Mod").exists() and (z.files_dir / "a.dds").read_bytes() == b"zeta"
    order = [{"id": z.id, "on": True}, {"id": a.id, "on": True}]            # Alpha last: wins
    p = mm.plan(root, order, app)
    mm.apply(root, p, app, log=lambda *_: None)
    placed = sorted(str(f.relative_to(root)).replace("\\", "/") for f in root.rglob("*") if f.is_file())
    assert placed == ["da2lm_mods/010_Zeta Mod/a.dds", "da2lm_mods/020_Alpha Mod/a.dds"]   # readme stays in the library
    assert os.stat(root / "da2lm_mods/010_Zeta Mod/a.dds").st_nlink >= 2                    # a link, not a copy
    con = scan(tmp_path / "i.db", [root], log=lambda *_: None)
    (c,) = conflicts(con)
    assert c.winner.mod == "Alpha Mod" and set(c.mods) == {"Zeta Mod", "Alpha Mod"}       # names, not folders
    con.close()
    # reorder: only da2lm's own folders change
    mm.apply(root, mm.plan(root, list(reversed(order)), app), app, log=lambda *_: None)
    assert (root / "da2lm_mods/020_Zeta Mod/a.dds").exists() and not (root / "da2lm_mods/010_Zeta Mod").exists()
    # a file changed by hand is never overwritten
    hand = root / "da2lm_mods/020_Zeta Mod/a.dds"
    hand.unlink(); hand.write_bytes(b"edited by hand")
    p = mm.plan(root, order, app)
    assert "da2lm_mods/020_Zeta Mod/a.dds" in p.blocked
    # undo returns to the previous order; turning off keeps the library copy
    assert mm.undo(root, app, log=lambda *_: None)
    mm.apply(root, mm.plan(root, [{"id": z.id, "on": False}, {"id": a.id, "on": True}], app), app, log=lambda *_: None)
    assert (z.files_dir / "a.dds").exists()
    assert [m["name"] for m in mm.current_order(root, app)] == ["Zeta Mod", "Alpha Mod"]


def test_library_archive_import(tmp_path):
    import zipfile as zf
    arc = tmp_path / "Cool Hair-123-1-0.zip"
    with zf.ZipFile(arc, "w") as z:
        z.writestr("Cool Hair/hair.mmh", b"m"); z.writestr("Cool Hair/chargenmorphcfg.xml", b"<morph_config/>")
    m = mm.Library(tmp_path / "app").add_archive(arc)
    assert m.name == "Cool Hair" and sorted(m.files) == ["chargenmorphcfg.xml", "hair.mmh"]


def _winners(override):
    """Loose resources: which file the game uses for each name (later path wins, uppercase order)."""
    from da2lm.index import _not_a_resource
    out = {}
    files = [f for f in override.rglob("*") if f.is_file() and ".da2lm" not in f.parts and not _not_a_resource(f)
             and f.suffix.lower() != ".erf"]
    for f in sorted(files, key=lambda f: tuple(x.upper() for x in f.relative_to(override).parts)):
        out[f.name.lower()] = f.read_bytes()
    return out


def _tree(d):
    return sorted((p.relative_to(d).as_posix(), p.read_bytes()) for p in d.rglob("*") if p.is_file())


def test_adopt_keeps_every_winner_and_undo_gives_back(tmp_path):
    from da2lm import loadorder as lo
    ov = tmp_path / "override"; app = tmp_path / "app"
    for folder, files in {"Wardens": {"set_a.uti": b"wardens", "x.gda": b"w"},
                          "Alpha": {"x.gda": b"alpha"}}.items():
        (ov / folder).mkdir(parents=True)
        for n, b in files.items():
            (ov / folder / n).write_bytes(b)
    (ov / "set_a.uti").write_bytes(b"loose-root")          # beaten by zz_Sunnie, beats the numbered folders
    (ov / "Valuable Junk.erf").write_bytes(b"erf")
    (ov / "readme.txt").write_bytes(b"hi")
    renames, _ = lo.plan(ov, ["Wardens", "Alpha"])          # the old numbered layout
    lo.apply(ov, renames)
    for folder, n, b in [("zz_Sunnie evolving", "set_a.uti", b"sunnie"), ("no more BDA", "bda.ncs", b"n")]:
        (ov / folder).mkdir(); (ov / folder / n).write_bytes(b)    # installed after the numbering, like yours
    before_w, before_t = _winners(ov), _tree(ov)
    assert before_w["set_a.uti"] == b"sunnie"

    hand = mm.hand_installed(ov)
    kinds = [(e["kind"], e["name"]) for e in hand]
    assert ("folder", "Wardens") in kinds and ("erf", "Valuable Junk") in kinds and ("loose", "Loose files") in kinds
    done = mm.adopt(ov, [{"hand": e, "on": e["on"]} for e in hand if e["kind"] != "deploy"], app, log=lambda *_: None)
    assert done["taken"] == 6
    assert sorted(p.name for p in ov.iterdir()) == ["da2lm_mods", "readme.txt"]
    assert _winners(ov) == before_w                          # same file wins every name
    assert [m["name"] for m in mm.current_order(ov, app)][:2] == ["Wardens", "Alpha"]   # numbers gone, order kept

    assert mm.undo(ov, app, log=lambda *_: None)
    assert _tree(ov) == before_t                             # back exactly as it was, old record included
    assert not any((app / "library").iterdir())



def test_rowmerge_is_stable_across_rebuilds(tmp_path, monkeypatch):
    """Like Arsenal (changes Gold) + Kirkwall Questing (later, copies the row unchanged): the combined
    row must survive every rebuild, not drop out because the previous output already 'settled' it."""
    from da2lm.gda import col_hash
    monkeypatch.setitem(rowmerge.SAFE_COLUMNS, "abi_", {col_hash(n) for n in ("Icon", "Level", "PosX", "Cost")})
    data = tmp_path / "DA2" / "packages" / "core" / "data"; data.mkdir(parents=True)
    write_erf_v3(data / "2da.rim", [("m2da_base.gda", _m2da_base(["ABI_"])),
                                    ("abi_base.gda", build(AB, [[1, "a", "i", 1, 1, 1.0]]))])
    root = tmp_path / "override"
    for d, row in (("010_Arsenal", [1, "a", "i", 1, 1, 9.0]), ("020_Questing", [1, "a", "i", 1, 1, 1.0])):
        (root / d).mkdir(parents=True)
        (root / d / f"abi_{d[4:].lower()}.gda").write_bytes(build(AB, [row]))
    out = root / "~da2lm_merged" / "abi_da2lm_rows.gda"
    for _ in range(3):
        rowmerge.run(root, [data], tmp_path / "i.db", apply=True, log=lambda *_: None)
        assert out.exists() and Gda.from_bytes(out.read_bytes()).index().keys() == {1}


def _lib_setup(tmp_path):
    ov, app, src = tmp_path / "override", tmp_path / "app", tmp_path / "src"
    ov.mkdir()
    for name, files in {"Alpha": {"a.gda": b"a1", "x.uti": b"alpha"}, "Beta": {"x.uti": b"beta"}}.items():
        (src / name).mkdir(parents=True)
        for n, b in files.items():
            (src / name / n).write_bytes(b)
    lib = mm.Library(app)
    a, b = lib.add_folder(src / "Alpha"), lib.add_folder(src / "Beta")
    order = [{"id": a.id, "on": True}, {"id": b.id, "on": True}]
    mm.apply(ov, mm.plan(ov, order, app), app, log=lambda *_: None)
    return ov, app, src, a, b


def test_update_keeps_place_and_undo_restores(tmp_path):
    ov, app, src, a, b = _lib_setup(tmp_path)
    new = tmp_path / "Alpha v2"; new.mkdir()
    (new / "a.gda").write_bytes(b"a2"); (new / "extra.ncs").write_bytes(b"e")
    mm.update_mod(ov, a.id, new, app, log=lambda *_: None)
    assert (ov / "da2lm_mods/010_Alpha/a.gda").read_bytes() == b"a2"
    assert (ov / "da2lm_mods/010_Alpha/extra.ncs").exists() and not (ov / "da2lm_mods/010_Alpha/x.uti").exists()
    assert [i["id"] for i in mm.current_order(ov, app)] == [a.id, b.id]
    assert mm.undo(ov, app, log=lambda *_: None) == "update_undone"
    assert (ov / "da2lm_mods/010_Alpha/a.gda").read_bytes() == b"a1" and not (ov / "da2lm_mods/010_Alpha/extra.ncs").exists()


def test_remove_and_undo(tmp_path):
    ov, app, src, a, b = _lib_setup(tmp_path)
    mm.remove_mod(ov, b.id, app, log=lambda *_: None)
    assert not (ov / "da2lm_mods/020_Beta").exists() and [m["name"] for m in mm.current_order(ov, app)] == ["Alpha"]
    assert mm.undo(ov, app, log=lambda *_: None) == "remove_undone"
    assert (ov / "da2lm_mods/020_Beta/x.uti").read_bytes() == b"beta"


def test_edited_library_file_is_picked_up(tmp_path):
    ov, app, src, a, b = _lib_setup(tmp_path)
    libfile = mm.Library(app).mods()[a.id].files_dir / "a.gda"
    libfile.unlink(); libfile.write_bytes(b"edited")            # editors often save a new file (breaks the link)
    assert mm.refresh(ov, app, log=lambda *_: None)
    assert (ov / "da2lm_mods/010_Alpha/a.gda").read_bytes() == b"edited"
    with open(libfile, "r+b") as f:                              # editing in place: same data through the link
        f.write(b"EDITED")
    p = mm.plan(ov, mm.Record.load(ov, app).order, app)
    assert not p.blocked and not p.add


def test_lost_record_is_recovered_from_da2lm_mods(tmp_path):
    ov, app, src, a, b = _lib_setup(tmp_path)
    shutil.rmtree(app)                                           # %LOCALAPPDATA%\da2lm gone
    hand = [e for e in mm.hand_installed(ov, app) if e["kind"] != "deploy"]
    assert [e["name"] for e in hand] == ["Alpha", "Beta"] and all(e.get("orphan") for e in hand)
    mm.adopt(ov, [{"hand": e, "on": True} for e in hand], app, log=lambda *_: None)
    assert [m["name"] for m in mm.current_order(ov, app)] == ["Alpha", "Beta"]
    assert (ov / "da2lm_mods/020_Beta/x.uti").read_bytes() == b"beta"


# ----------------------------------------------------------------------------- 0.29: add-ins, new library mods, inventory
ADDIN_MANIFEST = b'<?xml version="1.0"?><Manifest Type="AddIn"><AddInsList><AddInItem UID="X"/></AddInsList></Manifest>'


def _addin_setup(tmp_path):
    docs = tmp_path / "Dragon Age 2"
    ov = docs / "packages" / "core" / "override"; ov.mkdir(parents=True)
    return ov, docs / "addins", tmp_path / "app"


def test_addin_archive_splits_and_places_in_addins(tmp_path):
    import zipfile as zf
    ov, addins, app = _addin_setup(tmp_path)
    arc = tmp_path / "Kirkwall Expanded-123.zip"
    with zf.ZipFile(arc, "w") as z:
        z.writestr("kirkwall_expanded/manifest.xml", ADDIN_MANIFEST)
        z.writestr("kirkwall_expanded/core/data/ke.erf", b"erf")
        z.writestr("Kirkwall Expanded - Override/Bounty.erf", b"bounty")
        z.writestr("installation.txt", b"read me")
    mods = mm.Library(app).add_any(arc)
    kinds = sorted((m.kind, m.folder) for m in mods)
    assert kinds == [("addin", "kirkwall_expanded"), ("override", "")]
    order = [{"id": m.id, "on": True} for m in mods]
    mm.apply(ov, mm.plan(ov, order, app), app, log=lambda *_: None)
    assert (addins / "kirkwall_expanded/manifest.xml").read_bytes() == ADDIN_MANIFEST
    assert (addins / "kirkwall_expanded/core/data/ke.erf").exists()
    assert (ov / "da2lm_mods/010_Kirkwall Expanded/Kirkwall Expanded - Override/Bounty.erf").exists()
    # turning the add-in off removes its folder and warns about saves; undo puts it back
    off = [{"id": m.id, "on": m.kind != "addin"} for m in mods]
    p = mm.plan(ov, off, app)
    assert any("Saves made" in l for l in p.lines)
    mm.apply(ov, p, app, log=lambda *_: None)
    assert not (addins / "kirkwall_expanded").exists() and addins.is_dir()
    assert mm.undo(ov, app, log=lambda *_: None)
    assert (addins / "kirkwall_expanded/core/data/ke.erf").exists()


def test_hand_installed_addin_is_adopted_and_given_back(tmp_path):
    ov, addins, app = _addin_setup(tmp_path)
    d = addins / "MyAddin"; (d / "core/data").mkdir(parents=True)
    (d / "manifest.xml").write_bytes(ADDIN_MANIFEST); (d / "core/data/a.erf").write_bytes(b"a")
    (addins / "not_an_addin").mkdir(); (addins / "not_an_addin/x.txt").write_text("x")
    hand = [e for e in mm.hand_installed(ov, app) if e["kind"] == "addin"]
    assert [e["name"] for e in hand] == ["MyAddin"]
    mm.adopt(ov, [{"hand": e, "on": True} for e in hand], app, log=lambda *_: None)
    assert (d / "core/data/a.erf").read_bytes() == b"a"                      # same place, now da2lm's
    assert [m["kind"] for m in mm.current_order(ov, app)] == ["addin"]
    assert not [e for e in mm.hand_installed(ov, app) if e["kind"] == "addin"]
    assert mm.undo(ov, app, log=lambda *_: None) == "gave_back"
    assert (d / "core/data/a.erf").read_bytes() == b"a" and not mm.Library(app).mods()


def test_hand_addin_blocks_instead_of_overwriting(tmp_path):
    ov, addins, app = _addin_setup(tmp_path)
    src = tmp_path / "src" / "Dup"; (src / "core").mkdir(parents=True)
    (src / "manifest.xml").write_bytes(ADDIN_MANIFEST); (src / "core/x.erf").write_bytes(b"new")
    (m,) = mm.Library(app).add_any(src)
    (addins / "Dup/core").mkdir(parents=True); (addins / "Dup/core/x.erf").write_bytes(b"hand")
    p = mm.plan(ov, [{"id": m.id, "on": True}], app)
    assert "@addins/Dup/core/x.erf" in p.blocked
    mm.apply(ov, p, app, log=lambda *_: None)
    assert (addins / "Dup/core/x.erf").read_bytes() == b"hand"


def test_library_mod_missing_from_record_is_listed_off(tmp_path):
    ov, app, src, a, b = _lib_setup(tmp_path)
    extra = tmp_path / "Extra"; extra.mkdir(); (extra / "e.gda").write_bytes(b"e")
    x = mm.Library(app).add_folder(extra)
    order = mm.current_order(ov, app)
    assert order[-1] == {"id": x.id, "name": "Extra", "on": False, "kind": "override", "new": True}
    assert [i["id"] for i in order[:2]] == [a.id, b.id]


def test_inventory_lists_archive_entries(tmp_path):
    from da2lm.erf import write_erf_v3
    ov, app, src, a, b = _lib_setup(tmp_path)
    d = tmp_path / "ErfMod"; d.mkdir()
    write_erf_v3(d / "pack.erf", [("one.gda", b"1"), ("two.uti", b"22")])
    m = mm.Library(app).add_folder(d)
    order = mm.Record.load(ov, app).order + [{"id": m.id, "on": True}]
    mm.apply(ov, mm.plan(ov, order, app), app, log=lambda *_: None)
    assert mm.write_inventory(ov, app) >= 1
    inv = json.loads((app / "inventory" / f"{m.id}.json").read_text("utf-8"))
    (entry,) = inv["files"].values()
    assert sorted(n for n, _ in entry["erf"]["entries"]) == ["one.gda", "two.uti"]
    assert mm.write_inventory(ov, app) == 0                                   # unchanged: nothing rewritten


def test_identical_copy_left_by_another_tool_is_still_ours(tmp_path):
    ov, app, src, a, b = _lib_setup(tmp_path)
    f = ov / "da2lm_mods/010_Alpha/a.gda"
    data = f.read_bytes(); f.unlink(); f.write_bytes(data)            # same bytes, new file, new date
    import os
    os.utime(f, ns=(1, 1))
    p = mm.plan(ov, [{"id": a.id, "on": False}, {"id": b.id, "on": True}], app)
    assert "da2lm_mods/010_Alpha/a.gda" in p.remove and not p.blocked


def test_leftovers_in_da2lm_mods_are_found_and_set_aside(tmp_path):
    from da2lm import overview as ovw, known
    ov, app, src, a, b = _lib_setup(tmp_path)
    assert mm.deploy_leftovers(ov, app) == []
    # an old copy in a later folder hides Alpha's current a.gda; a mod file edited by hand
    old = ov / "da2lm_mods/020_Beta/a.gda"; old.write_bytes(b"old")      # in a folder da2lm knows
    orphan = ov / "da2lm_mods/030_Gone/z.uti"; orphan.parent.mkdir(); orphan.write_bytes(b"z")  # no record: mod list
    ed = ov / "da2lm_mods/020_Beta/x.uti"; ed.unlink(); ed.write_bytes(b"hand edit")
    lo = {x["rel"]: x for x in mm.deploy_leftovers(ov, app)}
    assert set(lo) == {"da2lm_mods/020_Beta/a.gda", "da2lm_mods/020_Beta/x.uti"}
    assert lo["da2lm_mods/020_Beta/a.gda"]["covers"] == [("Alpha", "da2lm_mods/010_Alpha/a.gda")]
    assert lo["da2lm_mods/020_Beta/x.uti"]["edited"] and not lo["da2lm_mods/020_Beta/a.gda"]["edited"]
    issue = ovw.leftover_issue(list(lo.values()))
    assert issue.severity == ovw.ATTENTION and issue.action == "setaside" and len(issue.files) == 2
    assert issue.id == ovw.leftover_issue(list(lo.values())).id
    for f in issue.files:
        known.set_aside(ov, f)
    mm.refresh(ov, app, log=lambda *_: None)                     # Beta's own x.uti comes back
    assert (ov / "da2lm_mods/020_Beta/x.uti").read_bytes() == b"beta"
    assert mm.deploy_leftovers(ov, app) == []
