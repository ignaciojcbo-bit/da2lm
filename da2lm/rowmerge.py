"""Combine mods that edit the same rows of a multi-part (M2DA) table.

DA2 reads a table like abilities as the game's base file plus every suffixed part
(abi_*.gda). When two mods ship a part with the same row ID, the game takes the WHOLE
row from the one that loads later, so the other mod's edits to that row are lost even
when the two changed different columns (one moved a talent in the tree, the other
removed its level requirement).

This builds one more part, <prefix>_da2lm_rows.gda in ~da2lm_merged (which loads
last), holding only those shared rows: the game's values, then each mod's changed
cells applied in load order. Cells two mods set differently are reported; the later
mod's value is used, the same as without merging. Rows a DLC part also defines are
left alone: DLC rows win over every mod part (confirmed in game), so a merged row
would change nothing.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from .gda import COL1, Gda, GdaError, styled_like
from .gff4 import Gff4Error, Struct
from .loadorder import MERGE_DIR
from .rows import MOD_LEVEL, analyze

TAG = "da2lm_rows"

# Combining shared rows column by column is OPT-IN per table. Most tables' rows are one
# whole thing: an appearance or item-variation row is a complete look (model + tints), a
# rewards row is one designed list of items. Mixing two mods' columns there gives a result
# neither mod made, so those rows are kept whole and the player picks one (load order).
#
# Abilities are the exception, but only for columns known to be independent settings
# (icon, tree position, prerequisites, level/points requirements, cost, cooldown). An
# ability's script and its effect parameters belong together: if two mods both change
# anything outside the safe set, the row is kept whole too.
from .gda import col_hash as _h

SAFE_COLUMNS = {
    "abi_": {_h("Icon"), _h("cost"), _h("Cooldown"), _h("cooldown_group"),
             0xb2de6b6b, 0x99f338a8,          # prerequisite abilities 1 and 2
             0x390b1e40, 0x38681c66,          # points-invested and level requirements
             0xafe104a2, 0xb6fa35e3},         # position in the talent tree
}
SAFE_COLUMNS["rewards"] = {_h("Copper"), _h("Silver"), _h("Gold"), _h("XP")}

# Columns that only make sense together: taken whole from one mod, never mixed.
GROUPS = {
    "rewards": [{_h(n) for n in ("item1", "item2", "item3", "item4", "item5", "item6")}],
}

KEEP_WHOLE_WHY = ("rows of this table are kept whole: each one is a single complete thing (a look, a list "
                  "of rewards...), so mixing two mods' columns would make something neither mod made")


def part_name(prefix: str) -> str:
    return f"{prefix}{'' if prefix.endswith('_') else '_'}{TAG}.gda"


def is_rowmerge_file(name: str) -> bool:
    return TAG in name.lower() and name.lower().endswith(".gda")


@dataclass
class RowResult:
    row_id: object
    mods: list
    cells: tuple = ()
    clashes: list = field(default_factory=list)     # (column, [(mod, value)...]) later wins


@dataclass
class FamilyMerge:
    prefix: str
    rows: list = field(default_factory=list)
    skipped: list = field(default_factory=list)     # (row_id, why)
    data: bytes | None = None
    error: str | None = None

    @property
    def mods(self):
        return sorted({m for r in self.rows for m in r.mods})

    @property
    def clean(self):
        return [r for r in self.rows if not r.clashes]


def combine(o, col_name=lambda i: f"col{i}", prefix=None, hashes=None):
    """Merge one RowOverlap. Returns RowResult, or (None, why) when it can't or needn't."""
    if prefix not in SAFE_COLUMNS:
        return None, KEEP_WHOLE_WHY
    provs = [(p, c) for p, c in o.providers if p.source != MERGE_DIR]
    mods = [(p, c) for p, c in provs if p.is_mod and p.level == MOD_LEVEL]
    if len({p.source for p, _ in mods}) < 2:
        return None, "only one mod"
    if any(p.level > MOD_LEVEL for p, _ in provs):
        return None, "a DLC part defines this row and wins over every mod"
    base = [c for p, c in provs if not p.is_mod or p.level < MOD_LEVEL]
    if not base:
        return None, "new row added by more than one mod with the same ID (not a game row)"
    base = base[-1]
    singles = SAFE_COLUMNS[prefix]
    hashes = hashes or [None] * len(base)
    groups = [[i for i, h in enumerate(hashes) if h in g] for g in GROUPS.get(prefix, [])]
    in_group = {i for g in groups for i in g}
    unsafe = [i for i in range(len(base)) if hashes[i] not in singles and i not in in_group]
    # changes outside the safe columns belong together (an ability's script and its values); identical
    # ones (both mods carry the same fix) are fine, different ones mean the row is kept whole
    behaviour = {tuple((i, c[i]) for i in unsafe if i < len(c) and c[i] != base[i]) for p, c in mods}
    behaviour.discard(())
    if len(behaviour) > 1:
        return None, ("more than one mod changes other values of this entry that belong together "
                      "(for an ability: its script and effect values), so the row is kept whole")
    out, owner, clashes = list(base), {}, {}
    for p, c in mods:
        for i in range(min(len(c), len(base))):
            if i in in_group or c[i] == base[i]:
                continue                                   # this mod didn't touch the cell
            if i in owner and owner[i][1] != c[i]:
                clashes.setdefault(col_name(i), [owner[i]]).append((p.source, c[i]))
            owner[i] = (p.source, c[i])
            out[i] = c[i]
        for g in groups:                                   # a group (e.g. a reward's item list) comes whole from one mod
            if not any(i < len(c) and c[i] != base[i] for i in g):
                continue
            vals = tuple(c[i] for i in g)
            key = ("group",) + tuple(g)
            label = f"{col_name(g[0])}..{col_name(g[-1])}"
            if key in owner and owner[key][1] != vals:
                clashes.setdefault(label, [(owner[key][0], "its list")]).append((p.source, "its list"))
            owner[key] = (p.source, vals)
            for i, v in zip(g, vals):
                out[i] = v
    return RowResult(o.row_id, list(dict.fromkeys(p.source for p, _ in mods)), tuple(out),
                     [(col, vals) for col, vals in clashes.items()]), None


def plan(con, vanilla_dirs, read_resource) -> list[FamilyMerge]:
    out = []
    for rep in analyze(con, vanilla_dirs, read_resource, skip_own_rows=True):
        template = next((p.gda() for p in rep.parts if not p.is_mod and p.gda() is not None), None)
        if template is None:
            continue
        fm = FamilyMerge(rep.prefix)
        for o in rep.overlaps:
            if o.kind != "conflict":
                continue
            res, why = combine(o, template.col_name, rep.prefix, template.hashes)
            if res is None:
                if why != "only one mod":
                    fm.skipped.append((o.row_id, why))
                continue
            fm.rows.append(res)
        if fm.rows or fm.skipped:
            if fm.rows:
                _build(fm, template)
            out.append(fm)
    return out


def _build(fm: FamilyMerge, template: Gda) -> None:
    try:
        t = Gda(copy.deepcopy(template.g))
        styles = template.cells(template.rows[0]) if template.rows else None
        t.rows[:] = [Struct(t.row_sdef, {COL1 + i: styled_like(v, styles[i]) if styles else v
                                         for i, v in enumerate(r.cells)}) for r in fm.rows]
        fm.data = t.to_bytes()
        Gda.from_bytes(fm.data)                            # we can read what we wrote
    except (GdaError, Gff4Error, KeyError, IndexError) as e:
        fm.data, fm.error = None, f"writing merged part: {e}"


def clear(root: Path) -> bool:
    d = Path(root) / MERGE_DIR
    found = False
    if d.is_dir():
        for f in d.iterdir():
            if f.is_file() and is_rowmerge_file(f.name):
                f.unlink()
                found = True
        from .outputs import tidy
        tidy(root)
    return found


def run(root: Path, vanilla_dirs, db, *, apply=False, log=print, extra_roots=()) -> list[FamilyMerge]:
    from .index import scan
    from .merge import _read_resource
    con = scan(db, [*extra_roots, root], vanilla=vanilla_dirs, log=log)
    res = plan(con, vanilla_dirs, _read_resource)
    con.close()
    if apply:
        clear(root)
        good = [f for f in res if f.data]
        if good:
            d = Path(root) / MERGE_DIR
            d.mkdir(exist_ok=True)
            for f in good:
                (d / part_name(f.prefix)).write_bytes(f.data)
    return res


def report_lines(res: list[FamilyMerge]) -> list[str]:
    lines = []
    for f in res:
        if f.rows:
            lines.append(f"{f.prefix}*: {len(f.rows)} rows combined from {', '.join(f.mods)}"
                         + (f" ({len(f.rows) - len(f.clean)} with cells both mods set differently; later mod's value used)"
                            if len(f.clean) < len(f.rows) else ""))
            for r in f.rows:
                for col, vals in r.clashes:
                    lines.append(f"    row {r.row_id} {col}: " + " -> ".join(f"{m}={v}" for m, v in vals))
        if f.error:
            lines.append(f"{f.prefix}*: ERROR {f.error}")
        for rid, why in f.skipped[:10]:
            lines.append(f"    row {rid} not combined: {why}")
        if len(f.skipped) > 10:
            lines.append(f"    ... {len(f.skipped) - 10} more rows not combined")
    return lines
