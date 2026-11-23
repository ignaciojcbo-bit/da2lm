"""Row-level overlap check for multi-part (M2DA) tables.

DA2 merges every .gda whose name starts with an M2DA prefix (listed by the game in
m2da_base*.gda: 'ABI_', 'EquipmentLayout', 'rewards', ...) into one table. Rows are
matched by ID, so two parts carrying the same ID compete for that row.

For each table family touched by a mod we collect its parts:
  game base (packages/core/data)   level 0
  game patch (packages/core/patch) level 1
  mod parts (override)             level 2   unless a mod file has the SAME NAME as a game
  DLC (addins)                     level 3   part: then it replaces that part, at its level

Precedence for one row ID, highest wins (all confirmed in game, Nov 2026):
  - DLC part > mod's suffixed part        Tallis test (equipmentlayout_eye)
  - mod's suffixed part > patch part      Varric test (apr 1070: EYP over apr_patch)
  - patch > base                          what the official patch is for
  - mod part vs mod part: the later PATH wins (load order), not the later file name.
    Arsenal's rewards beat EYP's on row 119 while EYP's folder sorted first; after renaming
    EYP's folder to 'zz_EYP Clean' (later path, earlier file name) EYP won.

Each overlapping row is classified: 'agree' (all copies have the same values, harmless)
or 'disagree' (a real conflict; the predicted winner is shown with its confidence).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .erf import ErfError, iter_archives, read_erf
from .gda import Gda, GdaError, norm
from .gff4 import Gff4Error

LEVEL_NAME = {0: "game", 1: "patch", 2: "mod", 3: "DLC"}
MOD_LEVEL = 2


@dataclass
class Part:
    name: str                 # file name, lowercase
    level: int
    source: str               # mod name, or archive path for game parts
    loader: object = None     # callable returning bytes or None
    is_mod: bool = False
    rank: int = 0             # load position of the mod copy (path order); 0 for game parts
    _gda: object = None
    error: str | None = None

    def gda(self):
        if self._gda is None and self.error is None:
            try:
                data = self.loader()
                if data is None:
                    self.error = "unreadable (encrypted DLC archive?)"
                else:
                    self._gda = Gda.from_bytes(data)
            except (GdaError, Gff4Error, ErfError, OSError, KeyError) as e:
                self.error = str(e)
        return self._gda


@dataclass
class RowOverlap:
    row_id: object
    providers: list           # [(Part, cells)] in precedence order, last = predicted winner
    agree: bool               # every copy has the same values
    confidence: str           # 'confirmed' | 'documented' | 'unconfirmed'
    differing: list = field(default_factory=list)   # column names that differ
    kind: str = ""            # conflict | overridden | changes | identical

    # conflict:   two or more MODS put different values in this row
    # overridden: the mods agree, but a game part (DLC/patch) wins with other values -> mod change has no effect
    # changes:    a mod changes a game row and wins (what the mod is for)
    # identical:  every copy has the same values


@dataclass
class FamilyReport:
    prefix: str
    parts: list
    overlaps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    copies: list = field(default_factory=list)   # (part, covered, base_rows, reverted_patch_rows)

    @property
    def disagreements(self):
        return [o for o in self.overlaps if o.kind == "conflict"]

    def of(self, kind):
        return [o for o in self.overlaps if o.kind == kind]

    @property
    def level(self):
        if self.of("conflict"):
            return "CONFLICT"
        if self.of("overridden"):
            return "NO EFFECT"
        if self.copies:
            return "WARNING"
        if self.of("changes"):
            return "OVERRIDE"
        return "ok"


def _placeholder(rid, provs, layout) -> bool:
    """Row 0 of many tables is a dummy ('****', 'INVALID_...'); mods copy it with junk values."""
    if rid not in (0, -1) or layout is None:
        return False
    idx = next((i for i in range(len(layout.cols)) if layout.col_name(i).lower() == "label"), None)
    labels = {str(c[idx]).upper() for _, c in provs if idx is not None and idx < len(c) and c[idx] is not None}
    return not labels or all("****" in l or l.startswith("INVALID") or not l.strip("* ") for l in labels)


def _level_of(path: str) -> int:
    p = path.replace("\\", "/").lower()
    if "/addins/" in p:
        return 3
    if "/patch/" in p:
        return 1
    return 0


def _game_parts(vanilla_dirs) -> dict[str, Part]:
    """Named .gda entries in the game's archives; later folders/archives replace earlier."""
    parts: dict[str, Part] = {}
    for vdir in vanilla_dirs:
        for f in iter_archives(vdir):
            try:
                erf = read_erf(f)
            except (ErfError, OSError, ValueError):
                continue
            for e in erf.entries:
                if e.basename and e.basename.endswith(".gda"):
                    parts[e.basename] = Part(e.basename, _level_of(str(f)), str(f),
                                             loader=(lambda erf=erf, e=e: erf.read(e)))
    return parts


def prefixes(game_parts: dict[str, Part], mod_parts: dict[str, Part]) -> list[str]:
    out = set()
    for name, part in {**game_parts, **mod_parts}.items():
        if not name.startswith("m2da_base"):
            continue
        t = part.gda()
        if t is None or len(t.cols) < 3:
            continue
        for r in t.rows:
            row_id = t.row_id(r)
            if isinstance(row_id, int) and 10000 <= row_id <= 10999:
                continue          # these IDs are plain 2DAs, not M2DAs (DA toolset wiki)
            p = norm(t.cell(r, 2))
            if isinstance(p, str) and p:
                out.add(p.lower())
    return sorted(out, key=len, reverse=True)


def family_of(name: str, pfx: list[str]) -> str | None:
    for p in pfx:                 # longest prefix first
        if name.startswith(p):
            return p
    return None


def _precedence(part: Part):
    return (part.level, part.rank)


def analyze(con, vanilla_dirs, read_resource, skip_own_rows=False) -> list[FamilyReport]:
    """skip_own_rows: leave out da2lm's own combined-rows files, to plan them from the mods alone.
    (Planning with the previous output in view would see its rows as already settled, drop them
    from the next output, and flip back and forth on every rebuild.)"""
    from .loadorder import MERGE_DIR
    game = _game_parts([Path(d) for d in vanilla_dirs])
    # winning mod copy of each .gda name (loose beats archive, later path wins)
    mod: dict[str, Part] = {}
    for name, modname, file, inner, tier, rank in con.execute("""
            SELECT r.name, m.name, r.file, r.inner_name, r.tier, r.rank FROM resources r JOIN mods m ON m.id=r.mod_id
            WHERE r.ext='gda' AND r.name IS NOT NULL ORDER BY r.name, r.tier, r.rank"""):
        if skip_own_rows and modname == MERGE_DIR and "da2lm_rows" in name.lower():
            continue
        level = game[name].level if name in game else MOD_LEVEL   # same name replaces the game part
        mod[name] = Part(name, level, modname, loader=(lambda f=file, i=inner: read_resource(f, i)),
                         is_mod=True, rank=tier * 10**9 + rank)
    pfx = prefixes(game, mod)
    families: dict[str, dict[str, Part]] = {}
    for name, part in {**game, **mod}.items():          # mod copy replaces game copy by name
        fam = family_of(name, pfx)
        if fam:
            families.setdefault(fam, {})[name] = part
    reports = []
    for fam, parts in sorted(families.items()):
        mod_names = [n for n in parts if n in mod]
        if not mod_names:
            continue
        rep = FamilyReport(fam, sorted(parts.values(), key=lambda p: (_precedence(p), p.name)))
        rows: dict[object, list] = {}
        layout = None
        unreadable_dlc = []
        for part in rep.parts:
            t = part.gda()
            if t is None:
                if part.is_mod:
                    rep.notes.append(f"{part.name} ({part.source}): {part.error}")
                elif part.level == 3:
                    unreadable_dlc.append(part.name)
                continue
            if layout is None:
                layout = t
            elif t.hashes != layout.hashes:
                rep.notes.append(f"{part.name} has different columns; its rows are not compared")
                continue
            seen_here, dups = set(), []
            for r in t.rows:
                rid = t.row_id(r)
                if rid in seen_here:
                    dups.append(rid)          # a repeated ID inside one file: first copy is used
                    continue
                seen_here.add(rid)
                rows.setdefault(rid, []).append((part, tuple(norm(x) for x in t.cells(r))))
            if dups and part.is_mod:
                rep.notes.append(f"{part.name} ({part.source}) repeats row IDs {sorted(set(dups))[:8]};"
                                 " only the first copy is compared")
        for rid, provs in rows.items():
            if len(provs) < 2 or not any(p.is_mod for p, _ in provs):
                continue
            provs.sort(key=lambda pc: _precedence(pc[0]))
            values = {c for _, c in provs}
            agree = len(values) == 1
            top = provs[-1][0].level
            top_parts = [p for p, _ in provs if p.level == top]
            below = {p.level for p, _ in provs if p.level < top}
            if len(top_parts) > 1:
                if not all(p.is_mod for p in top_parts):
                    conf = "unconfirmed"
                elif len({p.rank >= 10**9 for p in top_parts}) > 1:
                    # tested in game only between loose files (EYP vs Arsenal, rewards row 119);
                    # a loose part against an archived (.erf) part may follow the file rule instead
                    conf = "probable: later path wins (loose vs .erf parts not tested)"
                else:
                    conf = "confirmed: later path wins"
            elif top > max(below, default=-1):
                conf = "confirmed"                    # DLC > mod > patch > base, all tested
            else:
                conf = "unconfirmed"
            diff = []
            if not agree and layout is not None:
                first = provs[0][1]
                for _, c in provs[1:]:
                    diff += [layout.col_name(i) for i, (a, b) in enumerate(zip(first, c)) if a != b]
            mod_vals = {c for p, c in provs if p.is_mod}
            winner_part, winner_vals = provs[-1]
            game_vals = [c for p, c in provs if not p.is_mod and p.level < MOD_LEVEL]
            game_vals = game_vals[-1] if game_vals else None
            # mods that really changed this row (a full-table copy repeating the game's values changed nothing)
            changed_vals = {c for p, c in provs if p.is_mod and c != game_vals}
            if agree:
                kind = "identical"
            elif _placeholder(rid, provs, layout):
                kind = "identical"                    # row 0 '****' / INVALID: a dummy row, never used
            elif len(mod_vals) > 1 and winner_part.is_mod and game_vals is not None and len(changed_vals) == 1 \
                    and winner_vals in changed_vals:
                kind = "changes"                      # the one mod that changed it wins; the others are plain copies
            elif len(mod_vals) > 1:
                kind = "conflict"
            elif not winner_part.is_mod and winner_vals not in mod_vals:
                kind = "overridden"
            else:
                kind = "changes"
            rep.overlaps.append(RowOverlap(rid, provs, agree, conf, sorted(set(diff)), kind))
        # extensions that carry (almost) the whole base table
        base_parts = [p for p in rep.parts if p.level == 0 and not p.is_mod and p.gda() is not None]
        base_ids = {r for p in base_parts for r in (p.gda().row_id(x) for x in p.gda().rows)}
        if len(base_ids) >= 20:
            for part in rep.parts:
                if not part.is_mod or part.level != MOD_LEVEL or part.gda() is None:
                    continue
                ids = {part.gda().row_id(x) for x in part.gda().rows}
                covered = len(ids & base_ids)
                if covered >= 0.5 * len(base_ids):
                    reverted = [o.row_id for o in rep.overlaps
                                if any(p is part for p, _ in o.providers) and _reverts_patch(o, part)]
                    rep.copies.append((part, covered, len(base_ids), reverted))
        if unreadable_dlc:
            rep.notes.append(f"{len(unreadable_dlc)} DLC parts can't be read (encrypted DLC archives), so mod rows"
                             f" can't be checked against them: {', '.join(sorted(unreadable_dlc)[:6])}"
                             + (" ..." if len(unreadable_dlc) > 6 else ""))
        if rep.overlaps or rep.notes:
            reports.append(rep)
    return reports


def _reverts_patch(o, part) -> bool:
    """The mod copy carries the pre-patch value of a row the official patch changed."""
    base = next((c for p, c in o.providers if p.level == 0 and not p.is_mod), None)
    patch = next((c for p, c in o.providers if p.level == 1 and not p.is_mod), None)
    mine = next((c for p, c in o.providers if p is part), None)
    if base is None or patch is None or mine is None or base == patch:
        return False
    return any(m == b != pt for m, b, pt in zip(mine, base, patch))


def redundant_with(reports, part_name: str, row_ids) -> dict:
    """For a mod part, which other mod parts set the very same values on these rows."""
    row_ids, out = set(row_ids), {}
    for rep in reports:
        for o in rep.overlaps:
            if o.row_id not in row_ids:
                continue
            mine = next((c for p, c in o.providers if p.is_mod and p.name == part_name), None)
            if mine is None:
                continue
            for p, c in o.providers:
                if p.is_mod and p.name != part_name and c == mine:
                    out[p.source] = out.get(p.source, 0) + 1
    return out


def _chain(o):
    return " < ".join(f"{p.name}[{LEVEL_NAME[p.level]}{':' + p.source if p.is_mod else ''}]" for p, _ in o.providers)


def lines_for(rep, limit=12) -> list[str]:
    """Neutral facts for one table family, most important first."""
    out = []
    for o in rep.of("conflict")[:limit]:
        out.append(f"row {o.row_id}: mods disagree on {', '.join(o.differing[:5]) or '?'} | {_chain(o)}"
                   f" -> {o.providers[-1][0].name} wins ({o.confidence})")
    for o in rep.of("overridden")[:limit]:
        mod = next(p for p, _ in o.providers if p.is_mod)
        out.append(f"row {o.row_id}: change from {mod.source} has no effect, {o.providers[-1][0].name}"
                   f" [{LEVEL_NAME[o.providers[-1][0].level]}] wins ({o.confidence}) | {_chain(o)}")
    for part, covered, total, reverted in rep.copies:
        out.append(f"{part.name} ({part.source}) carries {covered} of the {total} base rows: it re-states the"
                   f" game's table, so its values win over the base for all of them")
        if reverted:
            out.append(f"   it has pre-patch values in {len(reverted)} rows the official patch changed:"
                       f" {_ids_short(reverted)}; mod rows win over the patch (confirmed), so the old values apply")
    ch = rep.of("changes")
    if ch:
        srcs = sorted({p.source for o in ch for p, _ in o.providers if p.is_mod})
        out.append(f"{len(ch)} game rows changed by {', '.join(srcs)}: {_ids_short([o.row_id for o in ch])}")
    same = rep.of("identical")
    if same:
        out.append(f"{len(same)} rows repeated with identical values (harmless)")
    out += [f"note: {n}" for n in rep.notes]
    return out


def _ids_short(ids, n=10):
    return ", ".join(map(str, ids[:n])) + (f" (+{len(ids) - n} more)" if len(ids) > n else "")


def describe(reports) -> list[str]:
    out = []
    for rep in reports:
        mods = ", ".join(f"{p.name} ({p.source})" for p in rep.parts if p.is_mod)
        out.append(f"[{rep.level:9}] table {rep.prefix}*  - mod parts: {mods}")
        out += [f"            {line}" for line in lines_for(rep)]
    return out
