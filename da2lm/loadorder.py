"""Load order management for the DA2 override folder.

The engine walks override in alphabetical order and the last copy of a resource wins,
which is why people prefix folders with 'z', 'zz', 'zzz' to make a mod win.

We replace that with explicit numeric prefixes: each managed mod folder is renamed to
'NNN_<name>' (010, 020, ...; gaps of 10 leave room). Digits sort before letters, so the
order is exact and no longer depends on mod names.

Safety
- Dry run by default; nothing is renamed without apply=True.
- Every apply writes a journal (.da2lm/journal-<n>.json) so `undo` reverses it.
- A state file (.da2lm/order.json) keeps the original name of each managed folder.
- Folders created after the last apply (new mods) are "unmanaged": they keep their own
  name, and since letters sort after digits they load AFTER every managed mod. `plan`
  appends them at the bottom in their current order and warns.

Sorting caveat: Windows (NTFS) enumerates names by comparing UPPERCASE characters, where
'_' (0x5F) sorts after letters; a lowercase comparison puts '_' before letters. 'A_mod'
vs 'AB_mod' can flip depending on which the game does. Numeric prefixes avoid the issue.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

PREFIX_RE = re.compile(r"^(\d{3})_(.+)$")
STATE_DIR = ".da2lm"
MERGE_DIR = "~da2lm_merged"   # output of `da2lm merge`; "~" sorts after letters, digits and "_", so it loads last; never renumbered
RESERVED = {STATE_DIR, MERGE_DIR, "da2lm_mods"}
STEP = 10


def engine_key(name: str) -> str:
    """Approximation of the order NTFS hands names to the game (uppercase comparison)."""
    return name.upper()


def _sort_key_path(p: Path) -> tuple[str, ...]:
    return tuple(engine_key(part) for part in p.parts)


@dataclass
class Mod:
    folder: str      # current folder name on disk
    name: str        # display name (original name, without our prefix)
    managed: bool


def _state_path(root: Path) -> Path:
    return root / STATE_DIR / "order.json"


def _load_state(root: Path) -> dict[str, str]:
    p = _state_path(root)
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def _save_state(root: Path, state: dict[str, str]) -> None:
    p = _state_path(root)
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), "utf-8")


def current(root: str | Path) -> list[Mod]:
    """Mod folders in the order the game loads them (first = lowest priority)."""
    root = Path(root)
    state = _load_state(root)
    mods = []
    for d in sorted((p for p in root.iterdir() if p.is_dir() and p.name not in RESERVED),
                    key=lambda p: engine_key(p.name)):
        if d.name in state:
            mods.append(Mod(d.name, state[d.name], True))
        else:
            mods.append(Mod(d.name, d.name, False))
    return mods


def loose_files(root: str | Path) -> list[str]:
    return sorted(p.name for p in Path(root).iterdir() if p.is_file())


def plan(root: str | Path, order: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Turn a desired order (display names, first = loads first, last = wins) into renames.

    Returns (renames [(old_folder, new_folder)], warnings). Names not in `order` are
    appended at the bottom in their current order.
    """
    mods = current(root)
    by_name = {m.name.lower(): m for m in mods}
    warnings, seen, final = [], set(), []
    for n in order:
        m = by_name.get(n.strip().lower())
        if not m:
            warnings.append(f"not found, ignored: {n}")
        elif m.name.lower() in seen:
            warnings.append(f"listed twice, using first position: {n}")
        else:
            seen.add(m.name.lower())
            final.append(m)
    rest = [m for m in mods if m.name.lower() not in seen]
    if rest:
        warnings.append("not in your list, put at the bottom (they win): " + ", ".join(m.name for m in rest))
    final += rest
    if len(final) * STEP > 999:
        raise ValueError("too many mods for 3-digit prefixes")
    renames = []
    for i, m in enumerate(final, 1):
        new = f"{i * STEP:03d}_{m.name}"
        if new != m.folder:
            renames.append((m.folder, new))
    return renames, warnings


def apply(root: str | Path, renames: list[tuple[str, str]]) -> Path | None:
    """Rename folders (two-phase, so A->B / B->A swaps can't collide) and journal it."""
    root = Path(root)
    if not renames:
        return None
    state = _load_state(root)
    for old, new in renames:
        if (root / new).exists() and new not in {o for o, _ in renames}:
            raise FileExistsError(f"target exists and isn't being moved: {new}")
    tmp = [(old, f"__da2lm_tmp_{i}", new) for i, (old, new) in enumerate(renames)]
    for old, t, _ in tmp:
        (root / old).rename(root / t)
    for _, t, new in tmp:
        (root / t).rename(root / new)

    names = {old: state.pop(old, old) for old, _ in renames}
    for old, new in renames:
        state[new] = names[old]
    _save_state(root, state)

    jdir = root / STATE_DIR
    n = len(list(jdir.glob("journal-*.json"))) + 1
    journal = jdir / f"journal-{n:04d}.json"
    journal.write_text(json.dumps({"renames": renames}, indent=2, ensure_ascii=False), "utf-8")
    return journal


def undo(root: str | Path) -> Path | None:
    """Reverse the most recent apply."""
    root = Path(root)
    journals = sorted((root / STATE_DIR).glob("journal-*.json"))
    if not journals:
        return None
    last = journals[-1]
    renames = json.loads(last.read_text("utf-8"))["renames"]
    reverse = [(new, old) for old, new in renames]
    state = _load_state(root)
    tmp = [(cur, f"__da2lm_tmp_{i}", orig) for i, (cur, orig) in enumerate(reverse)]
    for cur, t, _ in tmp:
        (root / cur).rename(root / t)
    for _, t, orig in tmp:
        (root / t).rename(root / orig)
    for cur, orig in reverse:
        name = state.pop(cur, orig)
        if PREFIX_RE.match(orig):          # it was managed before that apply
            state[orig] = name
    _save_state(root, state)
    last.unlink()
    return last


def move(names: list[str], mod: str, *, before: str | None = None, after: str | None = None,
         top: bool = False, bottom: bool = False) -> list[str]:
    """Return a new order list with `mod` moved."""
    low = [n.lower() for n in names]
    if mod.lower() not in low:
        raise KeyError(mod)
    item = names[low.index(mod.lower())]
    rest = [n for n in names if n.lower() != mod.lower()]
    rlow = [n.lower() for n in rest]
    if top:
        return [item] + rest
    if bottom:
        return rest + [item]
    ref = before or after
    if ref is None or ref.lower() not in rlow:
        raise KeyError(ref)
    i = rlow.index(ref.lower()) + (1 if after else 0)
    return rest[:i] + [item] + rest[i:]


# --------------------------------------------------------------------------- enable / disable

DISABLED_DIR = "da2lm_disabled"


def disabled_dir(root: str | Path) -> Path:
    """Where unticked mods wait: next to override (packages/core/da2lm_disabled), so the game never reads them."""
    return Path(root).parent / DISABLED_DIR


def disabled(root: str | Path) -> list[Mod]:
    d = disabled_dir(root)
    if not d.is_dir():
        return []
    state = _load_state(Path(root))
    return [Mod(p.name, state.get(p.name, p.name), p.name in state)
            for p in sorted(d.iterdir(), key=lambda p: engine_key(p.name))
            if p.is_dir() and p.name != "_set aside files"]


def set_enabled(root: str | Path, folder: str, enabled: bool) -> None:
    """Move a mod folder between override and the disabled folder (nothing is deleted)."""
    root = Path(root)
    src, dst = ((disabled_dir(root) / folder, root / folder) if enabled
                else (root / folder, disabled_dir(root) / folder))
    if not src.is_dir():
        raise FileNotFoundError(src)
    if dst.exists():
        raise FileExistsError(f"a folder named {folder} already exists in {dst.parent}")
    dst.parent.mkdir(exist_ok=True)
    src.rename(dst)


def apply_layout(root: str | Path, wanted: list[tuple[str, bool]], log=print) -> dict:
    """One step for the window: `wanted` = [(display name, enabled)] in load order.

    Moves folders in/out of override as needed, then renumbers the enabled ones.
    Returns what was done: {"enabled": [...], "disabled": [...], "renames": [...], "warnings": [...]}.
    """
    root = Path(root)
    on = {m.name.lower(): m for m in current(root)}
    off = {m.name.lower(): m for m in disabled(root)}
    done = {"enabled": [], "disabled": [], "renames": [], "warnings": []}
    for name, en in wanted:
        k = name.lower()
        if en and k in off:
            set_enabled(root, off[k].folder, True)
            done["enabled"].append(name)
            log(f"enabled {name}")
        elif not en and k in on:
            set_enabled(root, on[k].folder, False)
            done["disabled"].append(name)
            log(f"disabled {name} (moved to {disabled_dir(root)})")
    renames, warnings = plan(root, [n for n, en in wanted if en])
    if renames:
        apply(root, renames)
    done["renames"], done["warnings"] = renames, warnings
    return done


def preview_layout(root: str | Path, wanted: list[tuple[str, bool]]) -> list[str]:
    """Human-readable list of what apply_layout would do."""
    root = Path(root)
    on = {m.name.lower() for m in current(root)}
    off = {m.name.lower() for m in disabled(root)}
    lines = [f"Turn on: {n}" for n, en in wanted if en and n.lower() in off]
    lines += [f"Turn off: {n} (moved out of override, not deleted)" for n, en in wanted if not en and n.lower() in on]
    enabled = [n for n, en in wanted if en]
    old = [m.name for m in current(root) if m.name.lower() in {e.lower() for e in enabled}]
    if [n.lower() for n in old] != [n.lower() for n in enabled if n.lower() in on] or any(
            not m.managed for m in current(root) if m.name.lower() in {e.lower() for e in enabled}):
        lines.append("New load order:")
        lines += [f"   {i}. {n}" for i, n in enumerate(enabled, 1)]
    return lines


def sorts_after_merge(root: str | Path) -> list[str]:
    """Names in override that load after da2lm's merged folder (only odd names, e.g. starting
    with an accented letter, can). Their files would beat the merged copies."""
    k = engine_key(MERGE_DIR)
    return [p.name for p in Path(root).iterdir() if p.name != MERGE_DIR and engine_key(p.name) > k]
