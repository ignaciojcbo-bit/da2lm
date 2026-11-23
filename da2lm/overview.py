"""Plain-language view of a scan: one list of things to check, grouped by the mods involved.

The desktop window shows this; the technical report (cli `report`) stays available as
"Details". Wording follows the project rule: describe what happens and who wins, never
assume a clash is a mistake. A clash the player marks as intended moves to "OK as is".
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .loadorder import MERGE_DIR

ATTENTION, INFO = "attention", "info"

TABLE_NAMES = {
    "abi": "abilities", "apr": "appearances", "itm": "items", "rewards": "rewards", "spec": "specializations",
    "equipmentlayout": "party equipment", "item_variations": "item variations", "properties": "item properties",
    "tal": "talents", "ui_talenttree": "talent trees", "ui_spec": "specialization screen", "gui": "interface",
    "chargen": "character creation", "mon": "monsters", "vfx": "visual effects", "persistent": "area effects",
    "class_abilities": "companion skill trees", "cla": "classes", "prcscr": "area scripts",
}
KINDS = {
    "dds": "textures", "tga": "textures", "mmh": "models", "msh": "models", "mao": "materials",
    "ncs": "scripts", "gda": "tables", "xml": "settings files", "are": "areas", "cnv": "dialogues",
    "dlg": "dialogues", "uti": "items", "utc": "characters", "mor": "face shapes", "mop": "face shapes",
    "phy": "physics", "ani": "animations", "gad": "animations", "evt": "animation events", "tnt": "tints",
    "plo": "quest flags", "stg": "stages", "tlk": "text",
}


def table_label(name: str) -> str:
    stem = name.lower().removesuffix(".gda")
    for p in sorted(TABLE_NAMES, key=len, reverse=True):
        if stem == p or stem.startswith(p + "_") or stem.startswith(p):
            return f"{TABLE_NAMES[p]} table ({stem})"
    return f"table {stem}"


ABI_NAMES = {0xb2de6b6b: "prerequisite", 0x99f338a8: "prerequisite", 0x390b1e40: "points requirement",
             0x38681c66: "level requirement", 0xafe104a2: "tree position", 0xb6fa35e3: "tree position"}


def _label_of(template, o) -> str:
    idx = next((i for i in range(len(template.cols)) if template.col_name(i).lower() == "label"), None) if template else None
    v = o.providers[-1][1][idx] if idx is not None and idx < len(o.providers[-1][1]) else None
    return v.strip() if isinstance(v, str) and v.strip() else f"entry {o.row_id}"


def _change_words(template, base, cells) -> str:
    """What a mod changed in one row, in words: 'gold', 'item list', 'level requirement, cost'..."""
    from .rowmerge import GROUPS
    if base is None:
        return "adds it"
    words = []
    for i, (a, b) in enumerate(zip(base, cells)):
        if a == b:
            continue
        h = template.hashes[i] if template else None
        name = template.col_name(i) if template else f"col{i}"
        if any(h in g for gs in GROUPS.values() for g in gs):
            w = "item list"
        elif h in ABI_NAMES:
            w = ABI_NAMES[h]
        elif "#" not in name:
            w = name.lower()
        else:
            w = "other values"
        if w not in words:
            words.append(w)
    return ", ".join(words) or "nothing"


def _options(rep, conf, template) -> list:
    """For every mod involved: what it wins if it loads last, and what the others lose."""
    bulk = {part.source for part, *_ in rep.copies}
    per_mod: dict = {}
    for o in conf:
        game = [c for p, c in o.providers if not p.is_mod]
        base = game[-1] if game else None
        for p, c in o.providers:
            if p.is_mod and p.source != MERGE_DIR and c != base:
                per_mod.setdefault(p.source, {})[o.row_id] = (o, _change_words(template, base, c))
    if len(per_mod) < 2:
        return []
    return [f"{c['label']}: {c['text']}" for c in _option_choices(rep, conf, template)]


GAME_PICK = "the game"          # folder in ~da2lm_merged/preferred holding game tables the player chose


def _n(k, one, many):
    return f"{k} {one if k == 1 else many}"


def _option_choices(rep, conf, template) -> list:
    """The same as _options, as choices the window can act on (each one: load that mod last)."""
    bulk = {part.source for part, *_ in rep.copies}
    per_mod: dict = {}
    for o in conf:
        game = [c for p, c in o.providers if not p.is_mod]
        base = game[-1] if game else None
        for p, c in o.providers:
            if p.is_mod and p.source != MERGE_DIR and c != base:
                per_mod.setdefault(p.source, {})[o.row_id] = (o, _change_words(template, base, c))
    if len(per_mod) < 2:
        return []
    others = [m for m in per_mod if m not in bulk]
    suggest = others[-1] if bulk & set(per_mod) and others else None
    out = []
    for m, rows in per_mod.items():
        lost = []
        for other, orows in per_mod.items():
            if other == m:
                continue
            shared = [orows[r] for r in rows if r in orows]
            if shared:
                what = sorted({w for _, w in shared})
                lost.append(f"{other} loses its {', '.join(what)} on " +
                            (_label_of(template, shared[0][0]) if len(shared) == 1 else f"{len(shared)} entries"))
        now = sum(1 for o in conf if o.providers[-1][0].source == m)
        wins = ", ".join(_label_of(template, o) + ("" if w == "other values" else f" ({w})")
                         for o, w in list(rows.values())[:3])
        wins += f" (+{len(rows) - 3} more)" if len(rows) > 3 else ""
        lost = [l.replace("its other values on", "its version of") for l in lost]
        text = f"Its version of {wins} is used" + (". " + "; ".join(lost) + "." if lost else ".")
        if 0 < now < len(conf):
            text += f" Right now it wins {now} of these {len(conf)}."
        if m == suggest:
            b = _join(sorted(bulk & set(per_mod)))
            text += (f" Suggested: {b} carries a copy of the whole table with a broad tweak, so on these few entries "
                     f"it only loses that tweak, while {m} keeps a change made for each one.")
        out.append({"label": f"{m} wins", "text": text, "do": "last", "mod": m, "files": None,
                    "now": now == len(conf), "best": m == suggest})
    return out


GFF3_EXT = {"uti", "utc", "utp", "utm"}


def _item_choices(cs, winner) -> list:
    """For items/creatures: what you get with each mod's version, read from the files themselves."""
    from .gff3 import contains, item_view
    from .merge import _read_resource
    cs = [c for c in cs if c.ext in GFF3_EXT]
    if not cs:
        return [], None, []
    views = {}                       # key -> {mod: view}
    for c in cs:
        for cp in c.copies:
            try:
                views.setdefault(c.key, {})[cp.mod] = (item_view(_read_resource(cp.file, cp.inner_name)),
                                                        bool(cp.inner_name))
            except Exception:
                pass
    mods = sorted({m for v in views.values() for m in v}, key=lambda m: m != winner)
    if len(mods) < 2:
        return [], None, []
    lines, choices = [], []
    best = None
    for m in mods:
        keeps_all = lost = extra = 0
        for key, v in views.items():
            if m not in v:
                continue
            mine, others = v[m][0], [x[0] for o, x in v.items() if o != m]
            if all(contains(mine, o) for o in others):
                keeps_all += 1
                extra += sum(len(set(mine[1]) - set(o[1])) for o in others)
            else:
                lost += 1
        n = sum(1 for v in views.values() if m in v)
        if keeps_all == n:
            t = (f"On all {n} it already includes everything the other versions have"
                 + (f", plus {extra} extra item properties" if extra else "") + ". Nothing is lost.")
            best = best or m
        else:
            t = f"On {lost} of {n} the other versions have properties or settings this one lacks, and those are lost."
        files = [(cp.file, cp.inner_name, c.key) for c in cs for cp in c.copies if cp.mod == m]
        choices.append({"label": f"{m}'s version", "text": t, "do": "none" if m == winner else "prefer",
                        "mod": m, "files": files, "now": m == winner, "best": False})
    for c in choices:
        c["best"] = c["mod"] == best
    if best and best != winner:
        archived = any(v.get(best, (None, False))[1] for v in views.values())
        lines.append(f"Suggestion: use {best}'s version." + (
            f" Its copies are packed in an .erf and the others are loose files, which always win, so moving it in "
            f"the list isn't enough. 'Use {best}'s version' places loose copies of its files in da2lm's own folder, "
            f"which loads last." if archived else f" Pick it below: da2lm places loose copies of its files in its own folder, which loads last."))
        files = [(cp.file, cp.inner_name, c.key) for c in cs for cp in c.copies if cp.mod == best]
        return lines, [best] + files, choices
    if best == winner:
        lines.append(f"Nothing to do: {winner}'s version, the one in use, already includes the others.")
    return lines, None, choices


def _plain_copiers(conf):
    """(mods that only repeat the game's values, mods that really change) when that's the whole story."""
    cp, ch = set(), set()
    for o in conf:
        game = [c for p, c in o.providers if not p.is_mod]
        if not game:
            return None
        for p, c in o.providers:
            if p.is_mod and p.source != MERGE_DIR:
                (cp if c == game[-1] else ch).add(p.source)
    cp -= ch
    return (sorted(cp), sorted(ch)) if cp and ch else None


def _row_labels(template, overlaps, n=8) -> str:
    """'Follower - Anders, Follower - Aveline (+3 more)' when the table has a Label column."""
    if template is None:
        return ""
    idx = next((i for i in range(len(template.cols)) if template.col_name(i).lower() == "label"), None)
    if idx is None:
        return ""
    labels = []
    for o in overlaps:
        v = o.providers[-1][1][idx] if idx < len(o.providers[-1][1]) else None
        if isinstance(v, str) and v.strip():
            labels.append(v.strip())
    labels = list(dict.fromkeys(labels))
    if not labels:
        return ""
    return ", ".join(labels[:n]) + (f" (+{len(labels) - n} more)" if len(labels) > n else "")


def _join(names) -> str:
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _ids(ids, n=6) -> str:
    ids = list(ids)
    return ", ".join(map(str, ids[:n])) + (f" (+{len(ids) - n} more)" if len(ids) > n else "")


def _singular(kind: str) -> str:
    one = {"textures": "texture", "models": "model", "materials": "material", "scripts": "script", "tables": "table",
           "settings files": "settings file", "areas": "area", "dialogues": "dialogue", "items": "item",
           "characters": "character", "face shapes": "face shape", "animations": "animation", "tints": "tint",
           "animation events": "animation event", "quest flags": "quest flag", "stages": "stage"}
    return one.get(kind, kind.removesuffix("s") if kind.endswith(" files") is False else kind[:-1])


@dataclass
class Issue:
    id: str                          # stable, used to remember "OK as is"
    severity: str                    # attention | info
    mods: list
    title: str
    text: list                       # plain sentences
    winner: str | None = None
    action: str | None = None        # merge | order | chargen | setaside | None
    files: list = field(default_factory=list)      # loose files the 'setaside' action moves out of override
    details: list = field(default_factory=list)   # technical lines
    accepted: bool = False
    # what the player can pick, each with its consequence:
    # {"label", "text", "do": merge|last|prefer|none, "mod", "files", "now": in use, "best": suggested}
    choices: list = field(default_factory=list)


def build(con, accepted=()) -> list[Issue]:
    """Every issue from a scanned index, attention first."""
    from .chargen import FILE_NAME as CHARGEN
    from .cli import _classify, _describe, _redundancy, _row_reports, _within_mod
    from .index import conflicts, replacements
    from .loadorder import MERGE_DIR
    from .merge import candidates
    from .rows import LEVEL_NAME, lines_for

    out: list[Issue] = []
    rreps = _row_reports(con) or []
    merge_keys = set(candidates(con))
    merged_now = {r.key for r in replacements(con) if r.merged}

    # 1. the same game table repacked by 2+ mods: only one copy can be used, merging keeps both
    for key, copies in sorted(candidates(con).items()):
        mods = [c[0] for c in copies]
        if key in merged_now:
            out.append(Issue(f"merge:{key}", INFO, mods, f"{_join(mods)}: {table_label(key)} merged",
                             [f"Each of these mods ships its own full copy of the {table_label(key)}. "
                              "da2lm merged them, so every mod's changes apply; where two mods changed the same "
                              f"value, {mods[-1]}'s is used.",
                              "da2lm keeps the merged file up to date after every change."],
                             winner="merged", action=None))
        else:
            choices = [{"label": "Combine them", "do": "merge", "mod": None, "files": None, "now": False, "best": True,
                        "text": "One table with every mod's changes; where two mods changed the same value, the one "
                                "that loads later wins. Your mod files stay untouched."}]
            for m in mods:
                rest = [x for x in mods if x != m]
                choices.append({"label": f"{m}'s copy wins", "do": "last", "mod": m, "files": None,
                                "now": m == mods[-1], "best": False,
                                "text": f"Only {m}'s changes to this table apply; whatever {_join(rest)} changed in it "
                                        "is lost."})
            out.append(Issue(f"merge:{key}", ATTENTION, mods, f"{_join(mods)} both replace the {table_label(key)}",
                             [f"Each mod ships its own full copy of this table and the game uses only one: {mods[-1]}'s, "
                              f"because it loads later. Whatever {_join(mods[:-1])} changed in it does not apply."],
                             winner=mods[-1], action="merge", choices=choices))

    # 2. one mod replaces a whole game table: normal for many mods, shown so nothing is hidden
    for r in replacements(con):
        if r.risk != "critical" or r.key in merge_keys or r.key == CHARGEN:
            continue
        if r.merged:
            pref = [f for (f,) in con.execute(
                "SELECT r.file FROM resources r JOIN mods m ON m.id=r.mod_id WHERE m.name=? AND r.key=?",
                (MERGE_DIR, r.hkey)) if "preferred" in Path(f).parts and GAME_PICK in Path(f).parts]
            if pref and r.mods:
                out.append(Issue(f"gametable:{r.key}", INFO, list(r.mods),
                                 f"Using the game's {table_label(r.key)} instead of {_join(r.mods)}'s (your choice)",
                                 [f"{_join(r.mods)}'s copy is installed but not used: da2lm placed the game's original "
                                  f"in {MERGE_DIR}\\preferred, which loads last."], winner="the game",
                                 choices=[{"label": f"Go back to {_join(r.mods)}'s copy", "do": "unprefer",
                                           "mod": r.mods[-1], "files": pref, "now": False, "best": False,
                                           "text": "Removes da2lm's copy of the game table, so the mod's applies again."}]))
            continue
        d, src = _classify(con, r)
        mod = r.mods[-1]
        text = [f"{mod} replaces the game's {table_label(r.key)} with its own copy."]
        if d and not d.error:
            bits = []
            if d.changed:
                bits.append("changes " + _n(len(d.changed), "row", "rows"))
            if d.deleted:
                bits.append("removes " + _n(len(d.deleted), "row", "rows") + f" ({_ids(d.deleted)})")
            if d.added:
                bits.append("adds " + _n(len(d.added), "row", "rows"))
            text.append("Compared with the game's copy, it " + (_join(bits) if bits else "changes nothing") + ".")
            if d.changed:
                text.append("Other mods that add their own rows with the same IDs (as separate files) still win "
                            "over this copy for those rows.")
        red = _redundancy(rreps, r, d)
        sev = INFO
        if red:
            text.append("Another installed mod already sets the same values in some of these rows, so part of this "
                        "copy changes nothing while both are installed.")
        choices = [{"label": f"Keep {mod}'s table", "do": "none", "mod": mod, "files": None, "now": True,
                    "best": False, "text": "Its changes apply. Usual for mods that rework this part of the game."}]
        if r.vanilla_erf:
            choices.append({"label": "Use the game's table", "do": "prefer_game", "mod": mod, "files": [r.key],
                            "now": False, "best": False,
                            "text": f"The original table is used and {mod}'s changes to it no longer apply (other "
                                    f"files of {mod} stay in use, which may expect its table). Your mod files "
                                    "aren't touched, and you can go back."})
        out.append(Issue(f"table:{r.key}", sev, [mod], f"{mod} replaces the {table_label(r.key)}", text,
                         winner=mod, details=_describe(d, src) + red, choices=choices))

    # 3. rows of multi-part tables
    for rep in rreps:
        if rep.level == "ok":
            continue
        mods = list(dict.fromkeys(p.source for p in rep.parts if p.is_mod))
        label = table_label(rep.prefix)
        details = lines_for(rep, limit=40)
        conf = [o for o in rep.of("conflict") if o.providers[-1][0].is_mod and o.providers[-1][0].source != MERGE_DIR]
        dlc_wins = [o for o in rep.of("conflict") if not o.providers[-1][0].is_mod]
        merged_rows = [o for o in rep.overlaps if o.providers[-1][0].source == MERGE_DIR
                       and len({p.source for p, _ in o.providers if p.is_mod and p.source != MERGE_DIR}) > 1]
        if merged_rows:
            who = sorted({p.source for o in merged_rows for p, _ in o.providers if p.is_mod and p.source != MERGE_DIR})
            out.append(Issue(f"rowmerge:{rep.prefix}", INFO, who, f"{_join(who)}: shared {label} entries combined",
                             [f"{len(merged_rows)} entries ({_ids(o.row_id for o in merged_rows)}) are edited by more "
                              "than one mod. da2lm combined them, so each mod's changes apply; where two mods set the "
                              "same value differently, the one that loads later is used.",
                              "da2lm keeps the combined file up to date after every change."],
                             winner="merged", details=details))
        if conf:
            from .rowmerge import combine
            template = next((p.gda() for p in rep.parts if not p.is_mod and p.gda() is not None), None)
            results = [combine(o, template.col_name if template else str, rep.prefix,
                               template.hashes if template else None)[0] for o in conf]
            names = _row_labels(template, conf)
            mergeable = [r for r in results if r is not None]
            clean = [r for r in mergeable if not r.clashes]
            winners = Counter(o.providers[-1][0].source for o in conf)
            win = winners.most_common(1)[0][0]
            involved = sorted({p.source for o in conf for p, _ in o.providers if p.is_mod},
                              key=lambda s: mods.index(s) if s in mods else 0)
            what_rows = f"{len(conf)} entries ({names or _ids(o.row_id for o in conf)})"
            copiers = _plain_copiers(conf)
            text = [f"{what_rows} are defined by more than one mod with "
                    f"different values. The game takes each whole entry from the mod that loads later (here mostly "
                    f"{win}), so the other mod's edits to those entries are lost."]
            if copiers:
                cp, changer = copiers
                text = [f"{what_rows}: only {_join(changer)} actually change{'s' if len(changer) == 1 else ''} them. "
                        f"{_join(cp)} also include{'s' if len(cp) == 1 else ''} these entries, but with the game's "
                        f"original values (part of a full copy of the table). The game takes each whole entry from the "
                        f"mod that loads later, here {win}, so "
                        + (f"{_join(changer)}'s change is undone." if win in cp else "the change applies.")]
            if not mergeable and template is not None:
                pass
            if mergeable and template is None:
                text.append(f"da2lm can combine {len(mergeable)} of them: "
                            + (f"in {len(clean)} the mods changed different values, so both apply"
                               if clean else "")
                            + (("; " if clean else "") + f"in {len(mergeable) - len(clean)} both set the same value "
                               "differently, and the later mod's value is used" if len(clean) < len(mergeable) else "")
                            + ". Your mod files stay untouched.")
            elif not mergeable:
                from .rowmerge import KEEP_WHOLE_WHY, SAFE_COLUMNS
                if rep.prefix not in SAFE_COLUMNS:
                    text.append("These aren't combined: " + KEEP_WHOLE_WHY + ". Decide which mod's version you "
                                "want and move that mod below in the mod list, or set the other file aside.")
                else:
                    text.append("They can't be combined safely. If you want the other mod's version, move it "
                                "below in the mod list.")
            clash_pairs: dict = {}
            for r in mergeable:
                for col, vals in r.clashes:
                    key = (col, tuple(m for m, _ in vals))
                    clash_pairs.setdefault(key, []).append(r.row_id)
            for (col, who), ids in clash_pairs.items():
                text.append(f"Where both set {col.lower()} ({len(ids)} entr{'y' if len(ids) == 1 else 'ies'}), combining "
                            f"keeps {who[-1]}'s value because it loads later; picking which mod wins changes that.")
            not_combined = [o for o, r in zip(conf, results) if r is None]
            choices = []
            if mergeable:
                choices.append({"label": "Combine them", "do": "merge", "mod": None, "files": None, "now": False,
                                "best": True, "text": (f"Each mod's changes apply on {_n(len(clean), 'entry', 'entries')}" if clean else "")
                                + (("; " if clean else "") + f"on {_n(len(mergeable) - len(clean), 'entry', 'entries')} "
                                   "both set the same value and the later mod's is used" if len(clean) < len(mergeable)
                                   else "")
                                + (f"; {len(not_combined)} can't be combined and keep the order's winner"
                                   if not_combined else "") + ". Your mod files stay untouched."})
            if template is not None:
                extra = _option_choices(rep, not_combined or conf, template)
                if mergeable:
                    for c in extra:
                        c["best"] = False           # combining is the suggestion; one suggestion at a time
                choices += extra
            if any(o.confidence.startswith("probable") for o in conf):
                text.append("Note: which one wins is probable, not tested: one copy is a loose file and the other "
                            "is packed in an .erf, and the game's rule for that mix hasn't been checked in game.")
            out.append(Issue(f"rows:{rep.prefix}:" + "|".join(involved), ATTENTION, involved,
                             f"{_join(involved)} edit the same {label} entries", text, choices=choices,
                             winner=win, action="merge" if mergeable else "order",
                             details=details + [f"combine: row {r.row_id} {c}: " + " -> ".join(f"{m}={v}" for m, v in vals)
                                                for r in mergeable for c, vals in r.clashes]))
        noeff = rep.of("overridden") + dlc_wins
        if noeff:
            src = {LEVEL_NAME[o.providers[-1][0].level] for o in noeff}
            who = sorted({p.source for o in noeff for p, _ in o.providers if p.is_mod})
            who = [w for w in who if w != MERGE_DIR] or who
            out.append(Issue(f"noeffect:{rep.prefix}:" + "|".join(who), ATTENTION, who,
                             f"Some of {_join(who)}'s {label} changes don't apply",
                             [f"{len(noeff)} entries ({_ids(o.row_id for o in noeff)}) are also defined by the "
                              f"{_join(sorted(src))}, which always wins over mods for these rows, so the mod's values "
                              "are not used.",
                              "To change them, a mod has to replace the whole game file by name."],
                             winner=_join(sorted(src)), details=details))
        for part, covered, base_rows, reverted in rep.copies:
            if part.source == MERGE_DIR:
                continue
            out.append(Issue(f"copy:{rep.prefix}:{part.source}", INFO, [part.source],
                             f"{part.source} carries a full copy of the {label}",
                             [f"Its file repeats {covered} of the game's {base_rows} rows instead of only the ones it "
                              "changes. Mods that load before it lose their changes to any of those rows."
                              + (f" It also brings back pre-patch values in {len(reverted)} rows ({_ids(reverted)})." if reverted else "")],
                             winner=part.source, details=details))
        if not conf and not noeff and not rep.copies and rep.of("changes"):
            out.append(Issue(f"changes:{rep.prefix}:" + "|".join(mods), INFO, mods,
                             f"{_join(mods)} change{'s' if len(mods) == 1 else ''} game {label} entries",
                             [f"{len(rep.of('changes'))} game entries are changed by mods and the changes apply."],
                             details=details))

    # 4. the same file in 2+ mods
    groups: dict[tuple, list] = {}
    chosen: dict[tuple, list] = {}
    for c in conflicts(con):
        if c.verdict == "identical" or c.key == CHARGEN or c.key in merge_keys:
            continue
        mods = tuple(m for m in c.mods if m != MERGE_DIR)
        if len(mods) < 2:
            continue
        if c.winner.mod == MERGE_DIR:            # a version the player chose ('use the suggested version')
            same = [cp.mod for cp in c.copies if cp.mod != MERGE_DIR and cp.sha1 == c.winner.sha1]
            chosen.setdefault((mods, same[0] if same else MERGE_DIR), []).append(c)
            continue
        groups.setdefault((mods, c.winner.mod), []).append(c)
    for (mods, win), cs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        kinds = Counter(KINDS.get(c.ext, f".{c.ext} files") for c in cs)
        what = _join(f"{n} {k}" if n > 1 else _singular(k) for k, n in kinds.most_common())
        risky = any(c.risk in ("high", "critical") for c in cs)
        loose_vs_archive = any(len({cp.file.lower().endswith((".erf", ".rim", ".rimp")) for cp in c.copies}) > 1
                               for c in cs)
        text = [f"{_join(mods)} include different versions of the same {what}. The game uses {win}'s."]
        lines, prefer, choices = _item_choices(cs, win)
        text += lines
        if loose_vs_archive:
            text.append("Note: a loose file always beats a copy inside an .erf archive, whatever the order, so "
                        "moving mods in the list may not change the winner for some files.")
        else:
            text.append("To use the other version, move that mod below in the mod list.")
        out.append(Issue("files:" + "|".join(mods), ATTENTION if risky else INFO, list(mods),
                         f"{_join(mods)} include the same {what}", text, winner=win, choices=choices,
                         action="prefer" if prefer else "order", files=prefer or [],
                         details=[f"{c.key}: " + " -> ".join(f"{cp.mod}" for cp in c.copies) for c in cs[:60]]
                         + ([f"... {len(cs) - 60} more"] if len(cs) > 60 else [])))

    for (mods, pick), cs in chosen.items():
        out.append(Issue("chosen:" + "|".join(mods), INFO, list(mods),
                         f"{_join(mods)}: using {pick}'s version of {len(cs)} file{'s' if len(cs) > 1 else ''} (your choice)",
                         [f"You chose {pick}'s version. da2lm keeps loose copies of it in {MERGE_DIR}\\preferred, which "
                          "loads last."], winner=pick,
                         choices=[{"label": "Go back to the normal order", "do": "unprefer", "mod": pick,
                                   "files": [c.winner.file for c in cs], "now": False, "best": False,
                                   "text": "Removes da2lm's copies, so the mod that loads later wins again."}]))

    # 5. a mod overriding its own files (optional variants etc.)
    for (mod, chain), n in sorted(_within_mod(con).items()):
        out.append(Issue(f"within:{mod}:{chain}", INFO, [mod], f"{mod} overrides {n} of its own files",
                         ["The mod has the same files in more than one of its folders (often optional variants "
                          "next to the main files). The later folder wins:", chain], winner=None))

    # 6. incompatibilities a mod's own readme warns about
    from .known import check as known_check
    for rule, hits in known_check(con):
        loose = [f for _, f, inner in hits if not inner]
        mods_hit = list(dict.fromkeys(m for m, _, _ in hits))
        out.append(Issue(f"known:{rule.id}", ATTENTION, mods_hit, rule.title,
                         rule.text + [f"Source: {rule.source}."]
                         + (["'Set the file aside' moves it out of override (into da2lm_disabled), so you can put it "
                             "back any time."] if loose else []),
                         action="setaside" if loose else "order", files=loose,
                         details=[f"{m}: {f}" + (f" :: {i}" if i else "") for m, f, i in hits]))

    for rule_issue in [i for i in out if i.id.startswith("known:")]:
        for i in out:
            if i.id.startswith("rows:") and set(i.mods) & set(rule_issue.mods):
                i.text.append(f"Suggestion: see \u201c{rule_issue.title}\u201d in this list; its fix also settles this.")

    # 7. the same mod installed twice (e.g. an old folder next to a renamed copy)
    out += _duplicates(con)

    # 8. something that loads after da2lm's merged folder would beat the merged copies
    root = con.execute("SELECT path FROM roots ORDER BY ord DESC LIMIT 1").fetchone()
    if root and Path(root[0], MERGE_DIR).is_dir():
        from .loadorder import sorts_after_merge
        late = sorts_after_merge(root[0])
        if late:
            out.append(Issue("after-merge", ATTENTION, late, f"{_join(late)} load{'s' if len(late) == 1 else ''} after the merged files",
                             [f"da2lm's merged files live in {MERGE_DIR}, which normally loads last. {_join(late)} "
                              "sorts after it, so its copies win over the merged ones.",
                              "Rename it, or apply the mod list order so it gets a number in front."], action="order"))

    acc = set(accepted)
    for i in out:
        if i.id in acc:
            i.accepted = True
    out.sort(key=lambda i: (i.severity != ATTENTION or i.accepted, i.accepted))
    return out


def _duplicates(con, min_shared=2, ratio=0.6) -> list[Issue]:
    from .loadorder import MERGE_DIR
    files: dict[str, dict[str, str]] = {}
    for mod, name, sha in con.execute("""SELECT m.name, r.name, IFNULL(r.sha1, '') FROM resources r
                                         JOIN mods m ON m.id = r.mod_id WHERE r.name IS NOT NULL"""):
        if mod in (MERGE_DIR, "override root"):
            continue
        files.setdefault(mod, {})[name] = sha
    mods = sorted(files)
    out = []
    for i, a in enumerate(mods):
        for b in mods[i + 1:]:
            shared = files[a].keys() & files[b].keys()
            small = min(len(files[a]), len(files[b]))
            big = max(len(files[a]), len(files[b]))
            if len(shared) < min_shared or len(shared) < ratio * small:
                continue
            if not (_similar_names(a, b) or len(shared) >= 0.9 * big):
                continue                  # two different mods that replace the same items/textures: a normal clash
            differ = sorted(n for n in shared if files[a][n] != files[b][n])
            text = [f"{a} and {b} share {len(shared)} of their files"
                    + (f" ({len(shared) * 100 // small}% of the smaller one)" if len(shared) < small else
                       " (all of the smaller one)") + ". This usually means the same mod is installed twice, "
                    "for example an old folder left next to a renamed or updated copy."]
            text.append(f"{len(differ)} of those files are different versions ({_ids(differ, 4)}), so which one you "
                        "get depends on load order." if differ else "The shared files are identical, so it changes "
                        "nothing, but the extra copy can be removed.")
            text.append("If one of them is an old copy, untick it in the mod list.")
            out.append(Issue(f"dup:{a}|{b}", ATTENTION if differ else INFO, [a, b],
                             f"{a} and {b} look like the same mod installed twice", text, action="order",
                             details=[f"shared: {n}" + ("  (differs)" if n in differ else "") for n in sorted(shared)][:80]))
    return out


def _similar_names(a: str, b: str) -> bool:
    import difflib, re
    na, nb = (re.sub(r"[^a-z0-9]", "", re.sub(r"^\d{3}_", "", x.lower())) for x in (a, b))
    na, nb = na.replace("evp", "eyp"), nb.replace("evp", "eyp")
    return bool(na and nb) and (na in nb or nb in na or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.6)


def needs_look(issues) -> list[Issue]:
    return [i for i in issues if i.severity == ATTENTION and not i.accepted]


def mod_badges(issues) -> Counter:
    """How many open attention issues involve each mod."""
    c = Counter()
    for i in needs_look(issues):
        for m in i.mods:
            c[m] += 1
    return c


def leftover_issue(items) -> Issue | None:
    """One entry for files in da2lm_mods that no mod in the list accounts for (see manager.deploy_leftovers)."""
    if not items:
        return None
    covering = [x for x in items if x["covers"]]
    edited = [x for x in items if x["edited"]]
    stray = [x for x in items if not x["edited"]]
    text = []
    if stray:
        text.append(f"{_n(len(stray), 'file sits', 'files sit')} in da2lm's mod folders without belonging to any mod "
                    "in your list: copies left from an earlier order, or files put there by hand. The game still "
                    "loads them.")
    if edited:
        text.append(f"{_n(len(edited), 'mod file was', 'mod files were')} changed by hand after da2lm placed "
                    f"{'it' if len(edited) == 1 else 'them'}, so da2lm leaves {'it' if len(edited) == 1 else 'them'} "
                    "alone and can't put the mod's current version there.")
    if covering:
        mods = sorted({m for x in covering for m, _ in x["covers"]})
        who = _join(mods) if len(mods) <= 3 else f"{', '.join(mods[:3])} and {len(mods) - 3} more mods"
        text.append(f"{_n(len(covering), 'of them loads', 'of them load')} after a file with the same name from "
                    f"{who}, so the game uses the old copy instead of the mod's current one.")
    text.append("Moving them out puts them in da2lm_disabled\\_set aside files (next to override), nothing is deleted. "
                "Mod files then get the mod's current version back on the next check.")
    details = []
    for x in items:
        tag = "edited by hand" if x["edited"] else "no mod"
        cov = "; wins over " + ", ".join(f"{m} ({r})" for m, r in x["covers"]) if x["covers"] else ""
        details.append(f"{x['rel']}  [{tag}{cov}]")
    n = len(items)
    title = (f"Leftover files in da2lm's mod folders ({n})"
             + (": they hide newer mod files" if covering else ""))
    import hashlib
    key = hashlib.sha1("\n".join(sorted(x["rel"] for x in items)).encode()).hexdigest()[:10]
    return Issue("leftovers:" + key,
                 ATTENTION if covering or edited else INFO, [], title, text,
                 action="setaside", files=[x["path"] for x in items], details=details)
