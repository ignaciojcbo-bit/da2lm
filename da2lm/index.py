"""Scan DA2 mod folders into a SQLite resource index and find conflicts.

Model
- A *root* is a folder the game reads resources from (e.g. Documents/BioWare/Dragon Age 2/
  packages/core/override). Roots are given in load order: later roots win.
- A *mod* is a first-level subfolder of a root (loose files directly in the root belong to
  the pseudo-mod "(root)").
- A *resource* is either a loose file or an entry inside an .erf found under a root.
  Resources are keyed by the FNV-1 64 hash of the lowercased base file name (the engine
  resolves by name; DA2's own archives mostly store only that hash, not the name),
  not by folder.

Winner rule (confirmed in game, Nov 2026, with tinted main-menu textures):
  1. Two tiers. Any LOOSE file beats any copy inside an .erf/.rim, wherever the
     archive sits and whatever it's called (tests B3, B4).
  2. Inside a tier, paths are compared like Windows does (uppercase, so '_' sorts after
     letters: A_mod loads after AB_mod) and the later one wins (test B2).
  3. No 'art/...' folder structure is needed: files are found by name (test B1).
  Archive vs archive across folders is assumed to follow rule 2 (not yet tested);
  a later root beats an earlier one. Run `da2lm order` to see the order the tool assumes.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .erf import ARCHIVE_EXTS, hkey_of, ErfError, iter_archives, iter_entries_with_data, read_erf
from .loadorder import MERGE_DIR, STATE_DIR, _load_state

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (id INTEGER PRIMARY KEY, path TEXT UNIQUE, ord INTEGER);
CREATE TABLE IF NOT EXISTS mods  (id INTEGER PRIMARY KEY, root_id INTEGER, name TEXT,
                                  UNIQUE(root_id, name));
CREATE TABLE IF NOT EXISTS erfs  (path TEXT PRIMARY KEY, mod_id INTEGER, version TEXT,
                                  encryption TEXT, compression TEXT, entries INTEGER,
                                  hash_variant TEXT, hash_mismatches INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY,
    mod_id INTEGER,
    key TEXT,              -- hex FNV-1 64 of the lowercased base name (see erf.hkey_of)
    name TEXT,             -- lowercased base name when known, else NULL
    ext TEXT,
    file TEXT,             -- file on disk (the loose file or the .erf)
    inner_name TEXT,       -- name inside the .erf, NULL for loose files
    size INTEGER,
    sha1 TEXT,             -- of unpacked content; NULL if it can't be read
    status TEXT,           -- ok | encrypted | undecodable
    tier INTEGER,          -- 0 = inside an archive, 1 = loose file (loose always wins)
    rank INTEGER           -- load position inside a tier; higher wins
);
CREATE INDEX IF NOT EXISTS ix_res_key ON resources(key);
CREATE TABLE IF NOT EXISTS vanilla (key TEXT PRIMARY KEY, name TEXT, erf TEXT);  -- game resources
CREATE TABLE IF NOT EXISTS vanilla_dirs (ord INTEGER, path TEXT);
-- scan cache: what was read from each mod file, reused while its size and mtime are unchanged
CREATE TABLE IF NOT EXISTS cache_files (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, meta TEXT);
CREATE TABLE IF NOT EXISTS cache_entries (path TEXT, seq INTEGER, key TEXT, name TEXT, ext TEXT,
                                          inner_name TEXT, size INTEGER, sha1 TEXT, status TEXT);
CREATE INDEX IF NOT EXISTS cache_entries_path ON cache_entries(path);
CREATE TABLE IF NOT EXISTS cache_meta (version INTEGER);
"""

# How dangerous it is when two mods ship the same resource name.
# Base tables that DA2 extends through M2DA. Shipping one of these names *replaces* the whole
# table instead of adding rows, so other mods' rows in that table are replaced too.
# Used only when no --vanilla folder was scanned; with it, any vanilla .gda name counts.
KNOWN_BASE_TABLES = {"abi_base.gda", "apr_base.gda", "spec_base.gda", "exptable.gda",
                     "itemprps.gda", "2da_base.gda"}

RISK = {
    "high": {"ncs", "are", "cnv", "dlg", "mor", "gda", "plo", "utc", "uti", "stg", "tlk", "xml"},
    "medium": {"mmh", "msh", "mao", "phy", "ani", "gad", "evt", "rim"},
}
HIGH_NAME_HINTS = ("chargen", "char_stage", "player_core", "rules_core", "2da_base")


def risk_of(key: str, ext: str) -> str:
    if ext in RISK["high"] or any(h in key for h in HIGH_NAME_HINTS):
        return "high"
    if ext in RISK["medium"]:
        return "medium"
    return "low"


def _sha1(data: bytes | None) -> str | None:
    return hashlib.sha1(data).hexdigest() if data is not None else None


def _sort_key(p: Path) -> tuple[str, ...]:
    # Per path component, uppercase comparison (how NTFS orders names). See loadorder.py.
    return tuple(part.upper() for part in p.parts)


NOT_RESOURCES = {"txt", "jpg", "jpeg", "png", "gif", "bmp", "md", "pdf", "url", "htm", "html", "doc", "docx",
                 "rtf", "zip", "rar", "7z", "ini", "log", "nss", "cif", "bak"}


def _not_a_resource(p: Path) -> bool:
    """Readmes, screenshots, archives and add-in manifests sit next to mods but the game never
    loads them as resources, so two mods shipping a readme.txt is not a clash."""
    n = p.name.lower()
    return n == "manifest.xml" or n.rsplit(".", 1)[-1] in NOT_RESOURCES


def connect(db: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(resources)")}
    vcols = {r[1] for r in con.execute("PRAGMA table_info(vanilla)")}
    if (cols and not {"name", "tier"} <= cols) or (vcols and "name" not in vcols):
        # index from an older version: it's only a cache, rebuild it
        con.executescript("DROP TABLE IF EXISTS resources; DROP TABLE IF EXISTS vanilla;"
                          " DROP TABLE IF EXISTS erfs; DROP TABLE IF EXISTS mods; DROP TABLE IF EXISTS roots;")
    con.executescript(SCHEMA)
    return con


CACHE_VERSION = 2          # bump when indexing changes what is stored per file


def scan(db: str | Path, roots: list[str | Path], *, vanilla: list[str | Path] | None = None,
         log=print, use_cache: bool = True) -> sqlite3.Connection:
    """Rebuild the index from scratch for the given roots (in load order).

    Files whose size and modification time match the previous scan are not read again
    (their entries and hashes come from the cache), so rescans of big texture packs are quick.

    `vanilla`: game data folders (e.g. <DA2>/packages/core/data). Only resource *names*
    are read from their ERFs, to know which mod files replace game files.
    """
    con = connect(db)
    con.executescript("DELETE FROM resources; DELETE FROM erfs; DELETE FROM mods; DELETE FROM roots;"
                      " DELETE FROM vanilla; DELETE FROM vanilla_dirs;")
    con.executemany("INSERT INTO vanilla_dirs VALUES (?, ?)", [(i, str(v)) for i, v in enumerate(vanilla or [])])
    for vdir in vanilla or []:
        _index_vanilla(con, Path(vdir), log)
    ver = con.execute("SELECT version FROM cache_meta").fetchone()
    if not ver or ver[0] != CACHE_VERSION:          # how files are read changed: start the cache over
        con.executescript("DELETE FROM cache_files; DELETE FROM cache_entries; DELETE FROM cache_meta;")
        con.execute("INSERT INTO cache_meta VALUES (?)", (CACHE_VERSION,))
    rank = 0
    seen: set[str] = set()
    hits = reads = 0
    for ord_, root in enumerate(roots):
        root = Path(root)
        if not root.is_dir():
            log(f"! skipping missing root: {root}")
            continue
        root_id = con.execute("INSERT INTO roots(path, ord) VALUES (?, ?)", (str(root), ord_)).lastrowid
        mod_ids: dict[str, int] = {}
        display = _load_state(root)   # managed folders -> original mod names

        def mod_id(name: str) -> int:
            if name not in mod_ids:
                mod_ids[name] = con.execute("INSERT INTO mods(root_id, name) VALUES (?, ?)",
                                            (root_id, name)).lastrowid
            return mod_ids[name]

        files = (p for p in root.rglob("*")
                 if p.is_file() and STATE_DIR not in p.relative_to(root).parts and not _not_a_resource(p))
        for f in sorted(files, key=_sort_key):
            rel = f.relative_to(root)
            from .manager import display_name
            mod = (display_name(rel.parts) or display.get(rel.parts[0], rel.parts[0])) if len(rel.parts) > 1 \
                else "override root"
            mid = mod_id(mod)
            rank += 1
            st = f.stat()
            seen.add(str(f))
            if use_cache and _from_cache(con, f, st, mid, rank):
                rank = max(rank, con.execute("SELECT MAX(rank) FROM resources").fetchone()[0] or 0)
                hits += 1
                continue
            ext = f.suffix.lower().lstrip(".")
            first = con.execute("SELECT COALESCE(MAX(id), 0) FROM resources").fetchone()[0]
            if ext in ARCHIVE_EXTS:
                rank, ok = _index_erf(con, f, mid, rank, log)
            else:
                data = f.read_bytes()
                con.execute(
                    "INSERT INTO resources(mod_id,key,name,ext,file,inner_name,size,sha1,status,tier,rank)"
                    " VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                    (mid, hkey_of(f.name), f.name.lower(), ext, str(f), None, len(data), _sha1(data), "ok", rank))
                ok = True
            if ok:
                _to_cache(con, f, st, first)
            reads += 1
    # forget files that are gone
    stale = [p for (p,) in con.execute("SELECT path FROM cache_files") if p not in seen]
    con.executemany("DELETE FROM cache_files WHERE path=?", [(p,) for p in stale])
    con.executemany("DELETE FROM cache_entries WHERE path=?", [(p,) for p in stale])
    if hits:
        log(f"{hits} unchanged files taken from the last scan, {reads} read.")
    con.commit()
    return con


def _from_cache(con, f: Path, st, mid: int, rank: int) -> bool:
    row = con.execute("SELECT size, mtime, meta FROM cache_files WHERE path=?", (str(f),)).fetchone()
    if not row or row[0] != st.st_size or row[1] != st.st_mtime_ns:
        return False
    tier = 1
    if row[2]:
        meta = json.loads(row[2])
        con.execute("INSERT INTO erfs VALUES (?,?,?,?,?,?,?,?,?)", (str(f), mid, *meta))
        tier = 0
    entries = con.execute("""SELECT key, name, ext, inner_name, size, sha1, status FROM cache_entries
                             WHERE path=? ORDER BY seq""", (str(f),)).fetchall()
    con.executemany(
        "INSERT INTO resources(mod_id,key,name,ext,file,inner_name,size,sha1,status,tier,rank)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(mid, *e[:3], str(f), *e[3:], tier, rank + (i if tier == 0 else 0))
         for i, e in enumerate(entries, start=1 if tier == 0 else 0)])
    return True


def _to_cache(con, f: Path, st, first_id: int) -> None:
    p = str(f)
    erf = con.execute("SELECT version, encryption, compression, entries, hash_variant, hash_mismatches, error"
                      " FROM erfs WHERE path=?", (p,)).fetchone()
    con.execute("DELETE FROM cache_entries WHERE path=?", (p,))
    con.execute("INSERT OR REPLACE INTO cache_files VALUES (?,?,?,?)",
                (p, st.st_size, st.st_mtime_ns, json.dumps(list(erf)) if erf else None))
    con.execute("""INSERT INTO cache_entries SELECT file, id, key, name, ext, inner_name, size, sha1, status
                   FROM resources WHERE id > ? AND file = ?""", (first_id, p))


def _index_vanilla(con, vdir: Path, log) -> None:
    if not vdir.is_dir():
        log(f"! skipping missing vanilla folder: {vdir}")
        return
    for f in iter_archives(vdir):
        try:
            erf = read_erf(f)
        except (ErfError, OSError, ValueError) as e:
            log(f"! could not read vanilla {f}: {e}")
            continue
        con.executemany("INSERT INTO vanilla VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                        "name=COALESCE(vanilla.name, excluded.name)",
                        [(e.hkey, e.basename, f.name) for e in erf.entries])


def _index_erf(con, f: Path, mid: int, rank: int, log) -> int:
    try:
        erf = read_erf(f)
    except (ErfError, OSError, ValueError) as e:  # not fatal: record and move on
        con.execute("INSERT INTO erfs(path, mod_id, error) VALUES (?,?,?)", (str(f), mid, str(e)))
        log(f"! could not read {f}: {e}")
        return rank, False
    con.execute("INSERT INTO erfs VALUES (?,?,?,?,?,?,?,?,NULL)",
                (str(f), mid, erf.version, erf.encryption, erf.compression, len(erf.entries),
                 erf.hash_variant, erf.hash_mismatches))
    try:
        for entry, data, raw in iter_entries_with_data(erf):
            rank += 1
            status = "ok" if data is not None else ("encrypted" if erf.encryption != "none" else "undecodable")
            con.execute(
                "INSERT INTO resources(mod_id,key,name,ext,file,inner_name,size,sha1,status,tier,rank)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                (mid, entry.hkey, entry.basename, entry.ext, str(f), entry.name or entry.key, entry.unpacked_size,
                 _sha1(data), status, rank))
    except ErfError as e:
        con.execute("UPDATE erfs SET error=? WHERE path=?", (str(e), str(f)))
        log(f"! {f}: {e}")
        return rank, False
    return rank, True


# --------------------------------------------------------------------------- conflicts

@dataclass
class Copy:
    mod: str
    file: str
    inner_name: str | None
    sha1: str | None
    status: str
    rank: int


@dataclass
class Conflict:
    key: str
    ext: str
    risk: str
    verdict: str        # real | identical | unverified | merged
    copies: list[Copy]  # in load order; last one is the assumed winner
    hkey: str = ""

    @property
    def winner(self) -> Copy:
        return self.copies[-1]

    @property
    def mods(self) -> list[str]:
        return list(dict.fromkeys(c.mod for c in self.copies))


def conflicts(con: sqlite3.Connection, *, include_same_mod: bool = False) -> list[Conflict]:
    rows = con.execute("""
        SELECT r.key, r.ext, m.name, r.file, r.inner_name, r.sha1, r.status, r.rank, r.name
        FROM resources r JOIN mods m ON m.id = r.mod_id
        WHERE r.key IN (SELECT key FROM resources GROUP BY key HAVING COUNT(*) > 1)
        ORDER BY r.key, r.tier, r.rank""").fetchall()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)

    out = []
    for key, rs in grouped.items():
        copies = [Copy(r[2], r[3], r[4], r[5], r[6], r[7]) for r in rs]
        if not include_same_mod and len({c.mod for c in copies}) < 2:
            continue
        hashes = {c.sha1 for c in copies}
        if None in hashes:
            verdict = "unverified"
        elif len(hashes) == 1:
            verdict = "identical"
        else:
            verdict = "real"
        if copies[-1].mod == MERGE_DIR:
            verdict = "merged"
        ext = rs[0][1]
        name = display_name(con, key, [r[8] for r in rs])
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        risk = "critical" if _is_base_table(con, key, name, ext) else risk_of(name, ext)
        out.append(Conflict(name, ext, risk, verdict, copies, key))
    order = {"critical": -1, "high": 0, "medium": 1, "low": 2}
    vorder = {"real": 0, "unverified": 1, "merged": 2, "identical": 3}
    out.sort(key=lambda c: (vorder[c.verdict], order[c.risk], c.key))
    return out


def _has_vanilla(con) -> bool:
    return con.execute("SELECT 1 FROM vanilla LIMIT 1").fetchone() is not None


def display_name(con, hkey: str, names=()) -> str:
    for n in names:
        if n:
            return n
    row = con.execute("SELECT name FROM vanilla WHERE key=?", (hkey,)).fetchone()
    return row[0] if row and row[0] else f"#{hkey}"


def _is_base_table(con, hkey: str, name: str, ext: str) -> bool:
    if ext != "gda":
        return False
    if _has_vanilla(con):
        return con.execute("SELECT 1 FROM vanilla WHERE key=?", (hkey,)).fetchone() is not None
    return name in KNOWN_BASE_TABLES


def winning_copy(con, hkey: str):
    """(mod, file, inner_name) of the copy the game loads for this resource."""
    return con.execute("""SELECT m.name, r.file, r.inner_name FROM resources r JOIN mods m ON m.id=r.mod_id
                          WHERE r.key=? ORDER BY r.tier DESC, r.rank DESC LIMIT 1""", (hkey,)).fetchone()


def vanilla_dirs(con) -> list[str]:
    return [r[0] for r in con.execute("SELECT path FROM vanilla_dirs ORDER BY ord")]


@dataclass
class Replacement:
    key: str
    risk: str          # critical for whole-table GDA replacement, else the usual risk
    mods: list[str]    # in load order; last wins
    vanilla_erf: str | None
    merged: bool = False   # da2lm merge output overrides it
    hkey: str = ""


def replacements(con: sqlite3.Connection) -> list[Replacement]:
    """Mod resources that overwrite a game file (needs --vanilla), plus — always —
    whole-table replacements of known base GDAs, even if only one mod does it."""
    if _has_vanilla(con):
        rows = con.execute("""SELECT r.key, COALESCE(r.name, v.name, '#' || r.key), m.name, v.erf
                              FROM resources r
                              JOIN mods m ON m.id=r.mod_id JOIN vanilla v ON v.key=r.key
                              ORDER BY r.key, r.tier, r.rank""").fetchall()
    else:
        rows = [(k, n, m, None) for k, n, m in con.execute(
            "SELECT r.key, r.name, m.name FROM resources r JOIN mods m ON m.id=r.mod_id "
            "WHERE r.ext='gda' ORDER BY r.key, r.tier, r.rank") if n in KNOWN_BASE_TABLES]
    grouped: dict[str, Replacement] = {}
    for key, name, mod, verf in rows:
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        if key not in grouped:
            risk = "critical" if ext == "gda" else risk_of(name, ext)
            grouped[key] = Replacement(name, risk, [], verf, hkey=key)
        elif grouped[key].key.startswith("#") and not name.startswith("#"):
            grouped[key].key = name                     # a later copy knows the real name
            if ext == "gda":
                grouped[key].risk = "critical"
            elif grouped[key].risk != "critical":
                grouped[key].risk = risk_of(name, ext)
        if mod == MERGE_DIR:
            grouped[key].merged = True
        elif mod not in grouped[key].mods:
            grouped[key].mods.append(mod)
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(grouped.values(), key=lambda r: (order[r.risk], r.key))


def summary(con: sqlite3.Connection) -> dict:
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "roots": q("SELECT COUNT(*) FROM roots"),
        "mods": q("SELECT COUNT(*) FROM mods"),
        "resources": q("SELECT COUNT(*) FROM resources"),
        "erfs": q("SELECT COUNT(*) FROM erfs"),
        "vanilla": q("SELECT COUNT(*) FROM vanilla"),
        "erf_errors": q("SELECT COUNT(*) FROM erfs WHERE error IS NOT NULL"),
        "unreadable": q("SELECT COUNT(*) FROM resources WHERE status != 'ok'"),
        "hash_variants": dict(con.execute(
            "SELECT COALESCE(hash_variant,'n/a'), COUNT(*) FROM erfs WHERE version='3.0' GROUP BY 1")),
    }
