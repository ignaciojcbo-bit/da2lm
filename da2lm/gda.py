"""GDA tables (DA's binary 2DA, stored as a GFF4 of type 'G2DA') and cell-level merging.

Structure (xoreos gdafile.cpp / gff4fields.h):
  top struct: 10002 = column list, 10003 = row list
  column struct: 10001 = CRC32 of the lowercased UTF-16LE column name, 10999 = type
                 (0 string, 1 int, 2 float, 3 bool, 4 resource) when present
  row struct: field 10005 + i holds column i

Column names are not stored, only hashes. The ID column is the one whose hash is
CRC32("id"); rows are matched across files by ID.

Merging: each mod's full copy of a table is diffed against the vanilla table, cell by
cell. Only cells a mod actually changed (and rows it added) are applied, in load order.
Two mods changing the same cell to different values is a real conflict: the later
mod wins and it's reported. A mod that left a cell at its vanilla value never
overwrites another mod's change to it.
"""
from __future__ import annotations

import copy
import zlib
from dataclasses import dataclass, field

from .gff4 import Gff4, Gff4Error, Struct, parse, serialize

COL_HASH, COL_LIST, ROW_LIST, COL1, COL_TYPE = 10001, 10002, 10003, 10005, 10999
GDA_TYPES = {0: "string", 1: "int", 2: "float", 3: "bool", 4: "resource"}


def norm(v):
    """Comparison form of a cell. DA2 stores strings with a trailing NUL inside the
    string; other tools may not, and may write an empty cell as "" instead of null.
    Those are the same value to the game, so they compare equal here."""
    if isinstance(v, str):
        v = v.rstrip("\x00")
        return v if v else None
    return v


def styled_like(new, old):
    """Write `new` in the same string convention the vanilla cell uses."""
    if isinstance(new, str) or new is None:
        n = norm(new)
        if n is None:
            return old if norm(old) is None else None
        if isinstance(old, str) and old.endswith("\x00"):
            return n + "\x00"
        return n
    return new


def col_hash(name: str) -> int:
    return zlib.crc32(name.strip().lower().encode("utf-16-le"))


ID_HASH = col_hash("ID")
KNOWN_COLUMNS = {col_hash(n): n for n in ("ID", "Label", "Name", "NameStrRef", "DescStrRef", "Icon",
                                          "Type", "Model", "Value", "Comment", "Copper", "Silver", "Gold",
                                          "XP", "item1", "item2", "item3", "item4", "item5", "item6", "cost",
                                          "Cooldown", "cooldown_group")}


class GdaError(Exception):
    pass


class Gda:
    def __init__(self, g: Gff4):
        if g.ftype != b"G2DA":
            raise GdaError(f"not a GDA (GFF type {g.ftype!r})")
        self.g = g
        top = g.top.values
        self.cols: list[Struct] = top.get(COL_LIST) or []
        self.rows: list[Struct] = top.get(ROW_LIST)
        if self.rows is None:
            self.rows = top[ROW_LIST] = []
        self.hashes = [c.values.get(COL_HASH) for c in self.cols]
        self.types = [c.values.get(COL_TYPE) for c in self.cols]
        self.id_col = self.hashes.index(ID_HASH) if ID_HASH in self.hashes else 0
        row_fd = next((f for f in g.structs[0].fields if f.label == ROW_LIST), None)
        if row_fd is None:
            raise GdaError("GDA has no row list")
        self.row_sdef = row_fd.type

    @classmethod
    def from_bytes(cls, data: bytes) -> "Gda":
        try:
            return cls(parse(data))
        except Gff4Error as e:
            raise GdaError(str(e)) from e

    def to_bytes(self) -> bytes:
        return serialize(self.g)

    def col_name(self, i: int) -> str:
        h = self.hashes[i]
        return KNOWN_COLUMNS.get(h, f"col{i}#{h:08x}")

    def cell(self, row: Struct, i: int):
        return row.values.get(COL1 + i)

    def row_id(self, row: Struct):
        return self.cell(row, self.id_col)

    def cells(self, row: Struct) -> tuple:
        return tuple(self.cell(row, i) for i in range(len(self.cols)))

    def index(self) -> dict:
        return {self.row_id(r): r for r in self.rows}

    def same_layout(self, other: "Gda") -> str | None:
        if self.hashes != other.hashes:
            return f"different columns ({len(other.hashes)} vs vanilla {len(self.hashes)})"
        for i, (a, b) in enumerate(zip(self.types, other.types)):
            if a is not None and b is not None and a != b:
                return f"column {self.col_name(i)} has type {b} instead of {a}"
        return None


# --------------------------------------------------------------------------- building (tests, tools)

def build(columns: list[tuple[str, str]], rows: list[list], version: str = "V4.0") -> bytes:
    """Make a GDA from scratch. columns: [(name, gda_type)], gda_type in GDA_TYPES values."""
    from .gff4 import F_LIST, F_STRUCT, FieldDef, StructDef
    tcode = {v: k for k, v in GDA_TYPES.items()}
    ftype = {"int": (5, 4), "float": (8, 4), "bool": (0, 1), "string": (14, 4), "resource": (14, 4)}
    row_fields, off = [], 0
    for i, (_, t) in enumerate(columns):
        typ, size = ftype[t]
        row_fields.append(FieldDef(COL1 + i, typ, 0, off))
        off += size
    structs = [
        StructDef(b"gtop", 8, [FieldDef(COL_LIST, 1, F_LIST | F_STRUCT, 0),
                               FieldDef(ROW_LIST, 2, F_LIST | F_STRUCT, 4)]),
        StructDef(b"colm", 5, [FieldDef(COL_HASH, 4, 0, 0), FieldDef(COL_TYPE, 0, 0, 4)]),
        StructDef(b"rows", off, row_fields),
    ]
    cols = [Struct(1, {COL_HASH: col_hash(n), COL_TYPE: tcode[t]}) for n, t in columns]
    rws = [Struct(2, {COL1 + i: v for i, v in enumerate(r)}) for r in rows]
    g = Gff4(version, b"PC  ", b"G2DA", b"V0.2", structs, Struct(0, {COL_LIST: cols, ROW_LIST: rws}),
             shared_strings=(version == "V4.1"))
    return serialize(g)


# --------------------------------------------------------------------------- merging

@dataclass
class CellConflict:
    row_id: object
    column: str
    earlier_mod: str
    earlier_value: object
    later_mod: str
    later_value: object


@dataclass
class ModDiff:
    mod: str
    changed_cells: int = 0
    changed_rows: int = 0
    added_rows: int = 0
    missing_rows: int = 0
    skipped: str | None = None


@dataclass
class MergeResult:
    table: str
    data: bytes | None = None
    diffs: list[ModDiff] = field(default_factory=list)
    conflicts: list[CellConflict] = field(default_factory=list)
    error: str | None = None

    @property
    def mods_merged(self) -> int:
        return sum(d.skipped is None for d in self.diffs)


def roundtrip_ok(data: bytes) -> bool:
    """True if parsing and re-writing the table loses nothing."""
    a = Gda.from_bytes(data)
    b = Gda.from_bytes(a.to_bytes())
    return a.g == b.g


def merge(table: str, vanilla: bytes, mods: list[tuple[str, bytes]]) -> MergeResult:
    res = MergeResult(table)
    try:
        van = Gda.from_bytes(vanilla)
        if not roundtrip_ok(vanilla):
            raise GdaError("vanilla table doesn't survive a read/write round trip; refusing to merge")
    except (GdaError, Gff4Error, KeyError) as e:
        res.error = f"vanilla: {e}"
        return res

    out = Gda(copy.deepcopy(van.g))
    van_idx, out_idx = van.index(), out.index()
    owner: dict[tuple, tuple[str, object]] = {}        # (row_id, col) -> (mod, value)
    sample = van.cells(van.rows[0]) if van.rows else None  # vanilla string convention per column
    added_owner: dict[object, tuple[str, tuple]] = {}  # row_id -> (mod, cells)

    for mod_name, data in mods:
        d = ModDiff(mod_name)
        res.diffs.append(d)
        try:
            m = Gda.from_bytes(data)
        except (GdaError, Gff4Error, KeyError) as e:
            d.skipped = f"unreadable: {e}"
            continue
        if (why := van.same_layout(m)):
            d.skipped = why
            continue
        seen = set()
        for row in m.rows:
            rid = m.row_id(row)
            seen.add(rid)
            if rid in van_idx:
                vrow, orow, touched = van_idx[rid], out_idx[rid], False
                for i in range(len(van.cols)):
                    raw, vv = m.cell(row, i), van.cell(vrow, i)
                    mv = norm(raw)
                    if mv == norm(vv):
                        continue                       # untouched by this mod: never overwrite
                    key = (rid, i)
                    if key in owner and owner[key][1] != mv:
                        res.conflicts.append(CellConflict(rid, van.col_name(i), owner[key][0],
                                                          owner[key][1], mod_name, mv))
                    owner[key] = (mod_name, mv)
                    orow.values[COL1 + i] = styled_like(raw, vv)
                    d.changed_cells += 1
                    touched = True
                d.changed_rows += touched
            else:
                cells = tuple(norm(c) for c in m.cells(row))
                if rid in added_owner and added_owner[rid][1] != cells:
                    res.conflicts.append(CellConflict(rid, "(whole new row)", added_owner[rid][0],
                                                      "...", mod_name, "..."))
                added_owner[rid] = (mod_name, cells)
                new = Struct(out.row_sdef, {COL1 + i: styled_like(v, sample[i]) if sample else v
                                            for i, v in enumerate(m.cells(row))})
                if rid in out_idx:
                    out.rows[out.rows.index(out_idx[rid])] = new
                else:
                    out.rows.append(new)
                out_idx[rid] = new
                d.added_rows += 1
        d.missing_rows = sum(1 for rid in van_idx if rid not in seen)

    try:
        res.data = out.to_bytes()
        Gda.from_bytes(res.data)                       # sanity: we can read what we wrote
    except (GdaError, Gff4Error) as e:
        res.data, res.error = None, f"writing merged table: {e}"
    return res


# --------------------------------------------------------------------------- full-replacement diff

@dataclass
class TableDiff:
    deleted: list = field(default_factory=list)      # vanilla row IDs missing from the mod copy
    changed: dict = field(default_factory=dict)      # row ID -> [column names whose value differs]
    added: list = field(default_factory=list)        # row IDs not in vanilla
    duplicates: list = field(default_factory=list)   # row IDs appearing more than once in the mod copy
    error: str | None = None

    @property
    def verdict(self) -> str:
        if self.error:
            return "unverified"
        if self.changed:
            return "changes"        # overrides vanilla values -> other mods' edits to these rows lose
        if self.deleted:
            return "deletes"        # only removes rows: intentional pattern, can't be done with M2DA
        if self.added:
            return "adds"
        return "identical"


def diff_table(vanilla: bytes, mod: bytes) -> TableDiff:
    d = TableDiff()
    try:
        v, m = Gda.from_bytes(vanilla), Gda.from_bytes(mod)
    except (GdaError, Gff4Error, KeyError) as e:
        d.error = str(e)
        return d
    if (why := v.same_layout(m)):
        d.error = why
        return d
    vrows = {v.row_id(r): r for r in v.rows}
    seen = {}
    for r in m.rows:
        rid = m.row_id(r)
        seen[rid] = seen.get(rid, 0) + 1
        if seen[rid] > 1:
            if rid not in d.duplicates:
                d.duplicates.append(rid)
            continue
        if rid not in vrows:
            d.added.append(rid)
            continue
        cols = [v.col_name(i) for i in range(len(v.cols)) if norm(m.cell(r, i)) != norm(v.cell(vrows[rid], i))]
        if cols:
            d.changed[rid] = cols
    d.deleted = [rid for rid in vrows if rid not in seen]
    return d
