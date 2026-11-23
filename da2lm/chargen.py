"""Character-creation config merge (chargenmorphcfg.xml).

The game reads ONE chargenmorphcfg.xml; every cosmetic mod ships its own full copy, so
normally only the last one loaded counts and the others' heads, hairs, colours etc.
disappear from the character creator. This module merges them:

  base   = the game's own file (packages/core/data/misc.erf)
  then   = every enabled mod's copy, in load order
  result = for each section (e.g. hairs/human_female) the game's entries in their order,
           then each mod's NEW entries appended in that mod's order

An entry is identified by its name (case-insensitive) plus its other attributes, so
'hm_har_blda cut=0' and 'hm_har_blda cut=2' are different entries, as in the game file.
Entries a mod's copy lacks are never removed (older copies often miss newer entries);
that is reported, not acted on.

The merged file is written LOOSE into override/~da2lm_merged/: a loose file beats any
archived copy and that folder loads last, so it's the copy the game uses (rules confirmed
in game). The mods' own files stay untouched.

The parser is tolerant on purpose: many mod copies are hand-edited (stray indentation,
comments, spaces in 'name ="..."'), so it reads tags with a small tokenizer instead of a
strict XML parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FILE_NAME = "chargenmorphcfg.xml"
ROOT_TAG = "morph_config"

_TAG = re.compile(r"<!--.*?-->|<\?.*?\?>|<\s*(/?)\s*([A-Za-z_][\w.-]*)([^>]*?)(/?)\s*>", re.S)
_ATTR_RE = re.compile(r'''([A-Za-z_][\w.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')''')


class _Attrs:
    @staticmethod
    def findall(text):
        return [(k, a if q == '"' else b) for k, a, b, q in
                ((m.group(1), m.group(2), m.group(3), '"' if m.group(2) is not None else "'")
                 for m in _ATTR_RE.finditer(text))]


_ATTR = _Attrs


@dataclass(frozen=True)
class Entry:
    attrs: tuple            # (('name', 'x'), ('cut', '1')) in file order

    @property
    def key(self):
        return tuple(sorted((k.lower(), v.lower() if k.lower() == "name" else v) for k, v in self.attrs))

    @property
    def name(self):
        return dict((k.lower(), v) for k, v in self.attrs).get("name", "")

    def xml(self):
        return "<resource " + " ".join(f'{k}="{v}"' for k, v in self.attrs) + "/>"


@dataclass
class Config:
    sections: dict = field(default_factory=dict)     # "hairs/human_female" -> [Entry]
    order: list = field(default_factory=list)        # section paths in file order
    errors: list = field(default_factory=list)


def parse(text: str) -> Config:
    cfg, stack = Config(), []
    for m in _TAG.finditer(text):
        if m.group(2) is None:
            continue                                   # comment / declaration
        closing, tag, rest, selfclose = m.group(1), m.group(2), m.group(3), m.group(4)
        tag_l = tag.lower()
        if tag_l == "resource":
            if closing:
                continue                               # <resource name="x"></resource> form
            path = "/".join(stack[1:]) if stack and stack[0].lower() == ROOT_TAG else "/".join(stack)
            if not path:
                cfg.errors.append(f"resource outside any section: {rest.strip()[:60]}")
                continue
            if path not in cfg.sections:
                cfg.sections[path] = []
                cfg.order.append(path)
            cfg.sections[path].append(Entry(tuple(_ATTR.findall(rest))))
            continue
        if closing:
            if stack and stack[-1].lower() == tag_l:
                stack.pop()
            else:
                cfg.errors.append(f"unexpected </{tag}>")
        elif not selfclose:
            stack.append(tag)
            path = "/".join(stack[1:])
            if path and path not in cfg.sections and stack[0].lower() == ROOT_TAG:
                pass                                   # created lazily when it gets entries
    if stack:
        cfg.errors.append(f"unclosed tags: {stack}")
    return cfg


@dataclass
class ModResult:
    mod: str
    added: dict = field(default_factory=dict)        # section -> [Entry]
    already: int = 0                                 # entries already present (from game or earlier mod)
    missing_from_game: int = 0                       # game entries this copy lacks (kept anyway)
    new_sections: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def n_added(self):
        return sum(len(v) for v in self.added.values())


@dataclass
class MergeOutcome:
    config: Config
    results: list
    sources: dict                                    # (section, entry.key) -> who added it ('game' or mod)


def merge(game_text: str | None, mods: list[tuple[str, str]]) -> MergeOutcome:
    base = parse(game_text) if game_text else Config()
    out = Config(sections={s: list(v) for s, v in base.sections.items()}, order=list(base.order))
    sources = {(s, e.key): "game" for s, v in base.sections.items() for e in v}
    results = []
    for mod, text in mods:
        cfg = parse(text)
        r = ModResult(mod, errors=list(cfg.errors))
        for s, entries in base.sections.items():
            have = {e.key for e in cfg.sections.get(s, [])}
            r.missing_from_game += sum(1 for e in entries if e.key not in have)
        for s in cfg.order:
            if s not in out.sections:
                out.sections[s] = []
                out.order.append(s)
                r.new_sections.append(s)
            for e in cfg.sections[s]:
                if (s, e.key) in sources:
                    r.already += 1
                    continue
                out.sections[s].append(e)
                sources[(s, e.key)] = mod
                r.added.setdefault(s, []).append(e)
        results.append(r)
    return MergeOutcome(out, results, sources)


def render(cfg: Config) -> str:
    """Write the merged config with the game's nesting (section/subsection)."""
    lines = ['<?xml version="1.0" encoding="utf-8"?>', f"<{ROOT_TAG}>",
             "    <!-- merged by da2lm: game entries first, then each mod's additions in load order -->"]
    open_parent = None
    for path in cfg.order:
        parts = path.split("/")
        parent, leaf = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
        if open_parent and parent != open_parent:
            lines.append(f"    </{open_parent}>")
            open_parent = None
        if parent and parent != open_parent:
            lines.append(f"    <{parent}>")
            open_parent = parent
        ind = "        " if parent else "    "
        lines.append(f"{ind}<{leaf}>")
        for e in cfg.sections[path]:
            lines.append(f"{ind}    {e.xml()}")
        lines.append(f"{ind}</{leaf}>")
    if open_parent:
        lines.append(f"    </{open_parent}>")
    lines.append(f"</{ROOT_TAG}>")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- integration

def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def game_copy(vanilla_dirs) -> str | None:
    from .merge import vanilla_table
    data = vanilla_table([Path(d) for d in vanilla_dirs], FILE_NAME)
    return _decode(data) if data else None


def mod_copies(con, exclude_dirs=()) -> list[tuple[str, str, str | None]]:
    """(mod, file, inner) for every copy shipped by a mod, in load order (winner last)."""
    from .erf import hkey_of
    from .loadorder import MERGE_DIR
    rows = con.execute("""SELECT m.name, r.file, r.inner_name FROM resources r JOIN mods m ON m.id=r.mod_id
                          WHERE r.key=? ORDER BY r.tier, r.rank""", (hkey_of(FILE_NAME),)).fetchall()
    return [r for r in rows if r[0] != MERGE_DIR and r[0] not in exclude_dirs]


def build(con, vanilla_dirs, excluded=()) -> MergeOutcome:
    from .merge import _read_resource
    mods = []
    for mod, file, inner in mod_copies(con):
        if mod in excluded:
            continue
        try:
            mods.append((mod, _decode(_read_resource(file, inner))))
        except Exception as e:                       # report, don't stop the merge
            mods.append((mod, ""))
    out = merge(game_copy(vanilla_dirs), mods)
    for r in out.results:
        if not any(m == r.mod for m, t in mods if t):
            r.errors.append("could not read this mod's copy")
    return out


def write(override: Path, outcome: MergeOutcome) -> Path:
    from .loadorder import MERGE_DIR
    d = Path(override) / MERGE_DIR
    d.mkdir(exist_ok=True)
    p = d / FILE_NAME
    p.write_text(render(outcome.config), "utf-8")
    return p


def clear(override: Path) -> bool:
    from .loadorder import MERGE_DIR
    p = Path(override) / MERGE_DIR / FILE_NAME
    if p.exists():
        p.unlink()
        from .outputs import tidy
        tidy(Path(override))
        return True
    return False
