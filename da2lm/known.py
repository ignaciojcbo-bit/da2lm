"""Known incompatibilities that the files alone can't reveal: things a mod's own readme
warns about. Each rule names its source, so the player can check it."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Rule:
    id: str
    files: tuple              # resource names that trigger the rule (lowercase)
    with_any: tuple           # substrings; the rule fires if another .gda name contains one of them
    title: str
    text: list
    source: str


RULES = [
    Rule("dfa-apr-with-eyp", ("apr_base_ishdfa.gda",), ("eyp",),
         "DFA's appearance file is installed together with EYP",
         ["Diversified Follower Armors' readme, section Incompatibility: \u201cIf you DO use dragonagefun\u2019s mod, "
          "I recommend that you do NOT use either of the \u201capr_base.gda\u201d files included with my mod.\u201d "
          "dragonagefun's mod is Equip Your Party, which EYP Clean rebuilds.",
          "Both files define the same companions' appearance rows (Anders, Aveline, Fenris, Isabela, Varric, "
          "Sebastian...), and only one version of each row can be used."],
         "Diversified Follower Armors readme, section Incompatibility"),
]


def check(con) -> list[tuple[Rule, list]]:
    """[(rule, [(mod, file path)...])] for every rule that applies to the scanned mods."""
    from .loadorder import MERGE_DIR
    rows = con.execute("""SELECT r.name, m.name, r.file, r.inner_name FROM resources r JOIN mods m ON m.id=r.mod_id
                          WHERE r.name IS NOT NULL AND m.name != ?""", (MERGE_DIR,)).fetchall()
    names = {n for n, *_ in rows}
    out = []
    for rule in RULES:
        hits = [(m, f, inner) for n, m, f, inner in rows if n in rule.files]
        if hits and any(any(w in n for w in rule.with_any) for n in names if n not in rule.files and n.endswith(".gda")):
            out.append((rule, hits))
    return out


def set_aside(override: Path, file: Path) -> Path:
    """Move one loose file out of override into da2lm_disabled/_set aside files (never deleted)."""
    from .loadorder import disabled_dir
    file, override = Path(file), Path(override)
    dst_dir = disabled_dir(override) / "_set aside files" / file.parent.relative_to(override)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / file.name
    if dst.exists():
        raise FileExistsError(dst)
    file.rename(dst)
    return dst
