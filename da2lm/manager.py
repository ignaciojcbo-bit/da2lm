"""Mod library and deployment: order mods without renaming your folders.

How it works
- Each mod lives once in da2lm's library (%LOCALAPPDATA%\\da2lm\\library\\<id>\\files), with a
  mod.json listing its files. The library copy is never edited.
- Turning a mod on places its files in override\\da2lm_mods\\NNN_<name>\\. The NNN prefix is
  the load order; these folders belong to da2lm, so renumbering them never touches yours.
- Files are placed as HARD LINKS when the library and override are on the same disk: a
  second name for the same data, no extra space, created instantly. On another disk (or
  where links aren't allowed) they are copied instead.
- A record (%LOCALAPPDATA%\\da2lm\\deployments\\<key>.json) lists every file da2lm placed with
  its size and modification time. da2lm only removes or replaces files in that record
  whose size and time still match: a file you changed by hand is never overwritten; the
  plan reports it instead.
- Every apply keeps the previous order, so Undo can put it back.
- "Manage" takes a folder you installed by hand into the library (as links, so no copy
  and no extra space), then places it like any other mod. Nothing is deleted: the data
  stays in the library, and Undo / turning it off never loses a file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

DEPLOY_DIR = "da2lm_mods"
ADDIN_KEY = "@addins/"          # record key prefix for files placed in Documents\...\Dragon Age 2\addins
PREFIX_RE = re.compile(r"^(\d{3,4})_(.+)$")
STEP = 10


def app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "da2lm" if base and sys.platform == "win32" else Path.home() / ".da2lm"


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()[:40] or "mod"
    return s


def _safe_folder(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .") or "mod"


def place(src: Path, dst: Path) -> str:
    """Hard link src at dst (same disk), else copy. Returns 'link' or 'copy'."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def addins_dir(override: Path) -> Path:
    """Documents\\BioWare\\Dragon Age 2\\addins, next to packages\\core\\override."""
    return Path(override).parent.parent.parent / "addins"


def _abs(override: Path, rel: str) -> Path:
    """Where a record key lives on disk: add-in keys under addins, the rest under override."""
    if rel.startswith(ADDIN_KEY):
        return addins_dir(override) / rel[len(ADDIN_KEY):]
    return Path(override) / rel


def is_addin_dir(d: Path) -> bool:
    """An add-in folder: a manifest.xml that declares Type="AddIn"."""
    m = Path(d) / "manifest.xml"
    if not m.is_file():
        m = next((x for x in Path(d).iterdir() if x.is_file() and x.name.lower() == "manifest.xml"), None) \
            if Path(d).is_dir() else None
    if not m:
        return False
    try:
        head = m.read_bytes()[:4096].decode("utf-8", "replace").lower()
    except OSError:
        return False
    return 'type="addin"' in head.replace(" ", "")


def _seven_zip() -> Path | None:
    """7-Zip's command-line tool if installed (opens .7z and .rar)."""
    found = shutil.which("7z") or shutil.which("7z.exe")
    if found:
        return Path(found)
    for v in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(v)
        if base and (Path(base) / "7-Zip" / "7z.exe").is_file():
            return Path(base) / "7-Zip" / "7z.exe"
    return None


def _same_bytes(a: Path, b: Path, chunk: int = 1 << 20) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(chunk), fb.read(chunk)
            if x != y:
                return False
            if not x:
                return True


def _stamp(p: Path) -> list:
    st = p.stat()
    return [st.st_size, st.st_mtime_ns]


# --------------------------------------------------------------------------- library

@dataclass
class LibMod:
    id: str
    name: str
    root: Path                       # library/<id>
    files: list = field(default_factory=list)   # relative paths (posix)
    source: str = ""
    kind: str = "override"           # "override" | "addin"
    folder: str = ""                 # add-ins: the folder name under addins\\ (its manifest UID folder)

    @property
    def files_dir(self) -> Path:
        return self.root / "files"


class Library:
    def __init__(self, base: Path | None = None):
        self.base = (base or app_dir()) / "library"

    def mods(self) -> dict[str, LibMod]:
        out = {}
        if self.base.is_dir():
            for d in sorted(self.base.iterdir()):
                j = d / "mod.json"
                if j.is_file():
                    m = json.loads(j.read_text("utf-8"))
                    out[m["id"]] = LibMod(m["id"], m["name"], d, m["files"], m.get("source", ""),
                                          m.get("kind", "override"), m.get("folder", ""))
        return out

    def _new_id(self, name: str, seed: str) -> str:
        h = hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:8]
        mid, n = f"{_slug(name)}-{h}", 2
        while (self.base / mid).exists():
            mid, n = f"{_slug(name)}-{h}-{n}", n + 1
        return mid

    def _write(self, mid: str, name: str, source: str, kind: str = "override", folder: str = "") -> LibMod:
        root = self.base / mid
        files = sorted(p.relative_to(root / "files").as_posix() for p in (root / "files").rglob("*") if p.is_file())
        data = {"id": mid, "name": name, "source": source, "files": files}
        if kind == "addin":
            data.update(kind="addin", folder=folder)
        (root / "mod.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), "utf-8")
        return LibMod(mid, name, root, files, source, kind, folder)

    def add_folder(self, src: Path, name: str | None = None) -> LibMod:
        """Bring a folder in (as hard links where possible, so no extra space). An add-in folder
        (manifest.xml with Type="AddIn") becomes an add-in mod, placed in addins\\ instead of override."""
        src = Path(src)
        kind, folder = ("addin", src.name) if is_addin_dir(src) else ("override", "")
        return self.add_files([(f, f.relative_to(src).as_posix()) for f in src.rglob("*") if f.is_file()],
                              name or src.name, str(src), kind, folder)

    def add_any(self, src: Path, name: str | None = None) -> list:
        """Folder or archive, split into what it contains: every add-in folder inside becomes its own
        add-in mod, everything else one override mod (e.g. Kirkwall Expanded ships both)."""
        src = Path(src)
        if src.is_dir():
            return self._split_in(src, name or src.name, str(src), link=True)
        name = name or re.sub(r"(-\d+){1,4}.*$", "", src.stem) or src.stem
        tmp = self.base / f"_import_{hashlib.sha1(str(src).encode()).hexdigest()[:8]}"
        if tmp.exists():
            shutil.rmtree(tmp)
        try:
            inner = self.extract(src, tmp)
            return self._split_in(inner, name, str(src), link=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _split_in(self, root: Path, name: str, source: str, link: bool) -> list:
        if is_addin_dir(root):
            return [self.add_files([(f, f.relative_to(root).as_posix()) for f in root.rglob("*") if f.is_file()],
                                   name, source, "addin", root.name)]
        addins = [d for d in sorted(root.rglob("*")) if d.is_dir() and is_addin_dir(d)]
        addins = [d for d in addins if not any(a in d.parents for a in addins)]      # outermost only
        out = []
        rest = [f for f in root.rglob("*") if f.is_file() and not any(a in f.parents for a in addins)]
        for d in addins:
            label = name if not rest and len(addins) == 1 else f"{name} - {d.name} (add-in)"
            out.append(self.add_files([(f, f.relative_to(d).as_posix()) for f in d.rglob("*") if f.is_file()],
                                      label, source, "addin", d.name))
        from .index import _not_a_resource
        if any(not _not_a_resource(f) for f in rest):
            out.append(self.add_files([(f, f.relative_to(root).as_posix()) for f in rest], name, source))
        if not out:
            raise RuntimeError("that has no mod files")
        return out

    def add_files(self, files: list, name: str, source: str, kind: str = "override", folder: str = "") -> LibMod:
        """files = [(file on disk, path inside the mod)]. Checked: every file must arrive whole."""
        mid = self._new_id(name, f"{source}|{name}")
        dst = self.base / mid / "files"
        dst.mkdir(parents=True, exist_ok=True)
        for f, rel in files:
            place(Path(f), dst / rel)
        for f, rel in files:
            g = dst / rel
            if not g.is_file() or g.stat().st_size != Path(f).stat().st_size:
                shutil.rmtree(self.base / mid)
                raise OSError(f"{Path(f).name} didn't reach da2lm's library; nothing was moved")
        return self._write(mid, name, source, kind, folder)

    @staticmethod
    def extract(archive: Path, into: Path) -> Path:
        """Unpack a .zip/.7z into `into`; returns the mod's root (a single top folder is the mod)."""
        archive, into = Path(archive), Path(into)
        into.mkdir(parents=True)
        ext = archive.suffix.lower()
        if ext == ".zip":
            with zipfile.ZipFile(archive) as z:
                for i in z.infolist():
                    n = i.filename.replace("\\", "/")
                    if i.is_dir() or n.startswith("/") or ".." in n.split("/"):
                        continue
                    p = into / n
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(z.read(i))
        elif ext in (".7z", ".rar"):
            exe = _seven_zip()
            if exe:
                import subprocess
                r = subprocess.run([str(exe), "x", "-y", f"-o{into}", str(archive)], capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode != 0:
                    raise RuntimeError(f"7-Zip couldn't open {archive.name}: "
                                       + (r.stderr or r.stdout).decode("utf-8", "replace").strip()[-300:])
            elif ext == ".7z":
                try:
                    import py7zr
                except ImportError as e:
                    raise RuntimeError("Opening .7z needs 7-Zip (7-zip.org) or: py -m pip install py7zr. "
                                       "Or extract it yourself and use 'Add mod from folder'.") from e
                with py7zr.SevenZipFile(archive) as z:
                    z.extractall(into)
            else:
                raise RuntimeError("Opening .rar needs 7-Zip installed (7-zip.org). Or extract it yourself "
                                   "and use 'Add mod from folder'.")
        else:
            raise RuntimeError(f"{ext} archives aren't supported: extract it and use the folder instead.")
        for p in list(into.rglob("*")):                    # never keep anything that escaped (absolute/.. names)
            try:
                p.resolve().relative_to(into.resolve())
            except ValueError:
                raise RuntimeError(f"{archive.name} has unsafe paths inside; not imported")
        top = list(into.iterdir())
        return top[0] if len(top) == 1 and top[0].is_dir() else into

    def add_archive(self, archive: Path, name: str | None = None) -> LibMod:
        archive = Path(archive)
        name = name or re.sub(r"(-\d+){1,4}.*$", "", archive.stem) or archive.stem
        mid = self._new_id(name, f"{archive.name}|{archive.stat().st_size}")
        tmp = self.base / mid / "_extract"
        try:
            inner = self.extract(archive, tmp)
        except Exception:
            shutil.rmtree(self.base / mid, ignore_errors=True)
            raise
        inner.rename(self.base / mid / "files")
        if tmp.exists():
            shutil.rmtree(tmp)
        return self._write(mid, name, str(archive))

    def replace_files(self, mid: str, src: Path) -> LibMod:
        """New version of a mod: its files become src's (folder or archive); the old ones are kept in
        library/<id>/previous until the next update, for Undo."""
        m = self.mods()[mid]
        root = m.root
        stage = root / "_new"
        if stage.exists():
            shutil.rmtree(stage)
        src = Path(src)
        try:
            if src.is_dir():
                for f in src.rglob("*"):
                    if f.is_file():
                        place(f, stage / f.relative_to(src))
                folder = stage
            else:
                folder = self.extract(src, stage / "_x")
            if not any(p.is_file() for p in folder.rglob("*")):
                raise RuntimeError("that has no files")
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        if (root / "previous").exists():
            shutil.rmtree(root / "previous")
        m.files_dir.rename(root / "previous")
        folder.rename(m.files_dir)
        if stage.exists():
            shutil.rmtree(stage)
        return self._write(mid, m.name, str(src), m.kind, m.folder)

    def restore_previous(self, mid: str) -> bool:
        m = self.mods().get(mid)
        if not m or not (m.root / "previous").is_dir():
            return False
        shutil.rmtree(m.files_dir)
        (m.root / "previous").rename(m.files_dir)
        self._write(mid, m.name, m.source, m.kind, m.folder)
        return True


# --------------------------------------------------------------------------- deployment record

def _key(override: Path) -> str:
    return hashlib.sha1(str(Path(override).resolve()).lower().encode()).hexdigest()[:16]


@dataclass
class Record:
    path: Path
    order: list = field(default_factory=list)      # [{"id":..., "on": bool}] lowest priority first
    placed: dict = field(default_factory=dict)     # relpath (from override, posix) -> {"mod", "stamp"}
    history: list = field(default_factory=list)    # previous orders, for Undo
    last: dict = field(default_factory=dict)       # {"when": ISO time, "what": summary} of the last apply

    @classmethod
    def load(cls, override: Path, base: Path | None = None) -> "Record":
        p = (base or app_dir()) / "deployments" / f"{_key(override)}.json"
        if p.is_file():
            d = json.loads(p.read_text("utf-8"))
            return cls(p, d.get("order", []), d.get("placed", {}), d.get("history", []), d.get("last", {}))
        return cls(p)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"order": self.order, "placed": self.placed, "history": self.history[-20:],
                                         "last": self.last},
                                        indent=1, ensure_ascii=False), "utf-8")


@dataclass
class Plan:
    order: list
    add: list = field(default_factory=list)        # (library file, override relpath, mod id)
    remove: list = field(default_factory=list)     # override relpaths
    blocked: list = field(default_factory=list)    # relpaths changed by hand: left alone
    lines: list = field(default_factory=list)      # human summary

    @property
    def empty(self):
        return not self.add and not self.remove


def _target(order: list, lib: dict[str, LibMod]) -> dict[str, tuple]:
    from .index import _not_a_resource
    out = {}
    pos = 0
    width = max(3, len(str(STEP * sum(1 for i in order if i.get("on") and i["id"] in lib
                                         and lib[i["id"]].kind != "addin"))))
    for item in order:
        m = lib.get(item["id"])
        if not m or not item.get("on"):
            continue
        if m.kind == "addin":                              # add-ins: whole folder, no load-order number
            for rel in m.files:
                out[f"{ADDIN_KEY}{m.folder}/{rel}"] = (m.files_dir / rel, m.id)
            continue
        pos += 1
        folder = f"{pos * STEP:0{width}d}_{_safe_folder(m.name)}"
        for rel in m.files:
            if _not_a_resource(Path(rel)):
                continue                                   # readmes, screenshots: kept in the library only
            out[f"{DEPLOY_DIR}/{folder}/{rel}"] = (m.files_dir / rel, m.id)
    return out


def _ours(f: Path, info: dict) -> bool:
    """Still the file da2lm placed: same stamp, or (a hard link) still the very same data as the library's."""
    if _stamp(f) == info["stamp"]:
        return True
    src = info.get("lib")
    try:
        if not src or not Path(src).exists():
            return False
        if os.path.samefile(f, src):
            return True
        # rewritten with exactly the library's bytes (e.g. copied in by another tool): still ours
        return f.stat().st_size == Path(src).stat().st_size and _same_bytes(f, Path(src))
    except OSError:
        return False


def plan(override: Path, order: list, base: Path | None = None) -> Plan:
    override = Path(override)
    lib = Library(base).mods()
    rec = Record.load(override, base)
    target = _target(order, lib)
    p = Plan(order)
    for rel, info in rec.placed.items():
        f = _abs(override, rel)
        if rel in target and f.exists() and _ours(f, info):
            continue                                       # stays; re-placed below if its library copy changed
        if f.exists() and not _ours(f, info):
            p.blocked.append(rel)
            continue
        p.remove.append(rel)
    for rel, (src, mid) in target.items():
        f = _abs(override, rel)
        info = rec.placed.get(rel)
        if info and f.exists() and _ours(f, info):
            same = False
            try:
                same = os.path.samefile(f, src)
            except OSError:
                pass
            if same or not info.get("src") or _stamp(src) == info["src"]:
                continue
            p.add.append((src, rel, mid))                  # the library copy changed: place the new one
            continue
        if f.exists() and rel not in rec.placed:
            p.blocked.append(rel)                          # something not ours sits there
            continue
        if rel in p.blocked:
            continue
        p.add.append((src, rel, mid))
    names = {m.id: m.name for m in lib.values()}
    before = [i["id"] for i in rec.order if i.get("on")]
    after = [i["id"] for i in order if i.get("on")]
    on = [names.get(i, i) for i in after if i not in before]
    off = [names.get(i, i) for i in before if i not in after]
    if on:
        p.lines.append("Turn on: " + ", ".join(on))
    if off:
        p.lines.append("Turn off: " + ", ".join(off) + " (files stay in da2lm's library)")
    addins_off = [names.get(i, i) for i in before if i not in after and i in lib and lib[i].kind == "addin"]
    if addins_off:
        p.lines.append("Add-ins turned off: " + ", ".join(addins_off) + ". Saves made while they were on "
                       "won't load until they're turned back on.")
    if [i for i in after if i in before] != [i for i in before if i in after]:
        p.lines.append("New load order:")
        p.lines += [f"   {n}. {names.get(i, i)}" for n, i in enumerate(after, 1)]
    if p.blocked:
        p.lines.append(f"{len(p.blocked)} files were changed by hand and are left alone: " + ", ".join(p.blocked[:5]))
    return p


def apply(override: Path, p: Plan, base: Path | None = None, log=print, undo_entry=None, history=True) -> dict:
    override = Path(override)
    rec = Record.load(override, base)
    if history:
        rec.history.append(undo_entry if undo_entry is not None else rec.order)
    n_link = n_copy = 0
    for rel in p.remove:
        f = _abs(override, rel)
        if f.exists():
            f.unlink()
        rec.placed.pop(rel, None)
    for src, rel, mid in p.add:
        f = _abs(override, rel)
        if f.exists():
            f.unlink()
        how = place(src, f)
        n_link += how == "link"
        n_copy += how == "copy"
        rec.placed[rel] = {"mod": mid, "stamp": _stamp(f), "src": _stamp(src), "lib": str(src)}
    rec.order = p.order
    import datetime
    if not history:
        rec.save(); _prune_empty(override / DEPLOY_DIR); _prune_addins(override, p)
        log(f"Refreshed {n_link + n_copy} edited files.")
        return {"linked": n_link, "copied": n_copy, "removed": len(p.remove), "blocked": len(p.blocked)}
    rec.last = {"when": datetime.datetime.now().isoformat(timespec="minutes"),
                "what": "; ".join(l for l in p.lines if not l.startswith("   ")) or "Placed mods in order"}
    rec.save()
    _prune_empty(override / DEPLOY_DIR)
    _prune_addins(override, p)
    log(f"Placed {n_link + n_copy} files ({n_link} linked, {n_copy} copied), removed {len(p.remove)}.")
    return {"linked": n_link, "copied": n_copy, "removed": len(p.remove), "blocked": len(p.blocked)}


def undo(override: Path, base: Path | None = None, log=print) -> bool:
    rec = Record.load(override, base)
    if not rec.history:
        return False
    prev = rec.history.pop()
    rec.save()
    if isinstance(prev, dict) and "adopted" in prev:
        _give_back(Path(override), prev, base, log)
        return "gave_back"
    if isinstance(prev, dict) and "updated" in prev:
        lib = Library(base)
        _drop_placed(Path(override), prev["updated"], base)
        lib.restore_previous(prev["updated"])
        p = plan(override, prev["order"], base)
        apply(override, p, base, log, history=False)
        return "update_undone"
    if isinstance(prev, dict) and "removed" in prev:
        lib = Library(base)
        trash = lib.base / TRASH / prev["removed"]
        if trash.is_dir() and not (lib.base / prev["removed"]).exists():
            shutil.move(str(trash), str(lib.base / prev["removed"]))
        p = plan(override, prev["order"], base)
        apply(override, p, base, log, history=False)
        return "remove_undone"
    p = plan(override, prev, base)
    apply(override, p, base, log)
    rec = Record.load(override, base)
    rec.history.pop()                       # the undo itself isn't something to undo
    rec.save()
    return True


def _prune_addins(override: Path, p: "Plan"):
    """Remove add-in folders da2lm emptied (only the folders of files it removed; never addins itself)."""
    tops = {rel[len(ADDIN_KEY):].split("/", 1)[0] for rel in p.remove if rel.startswith(ADDIN_KEY)}
    base = addins_dir(override)
    for t in tops:
        d = base / t
        if d.is_dir():
            for sub in sorted((x for x in d.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
                if not any(sub.iterdir()):
                    sub.rmdir()
            if not any(d.iterdir()):
                d.rmdir()


def _prune_empty(d: Path):
    if not d.is_dir():
        return
    for sub in sorted((p for p in d.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(sub.iterdir()):
            sub.rmdir()
    if not any(d.iterdir()):
        d.rmdir()



# --------------------------------------------------------------------------- leftovers in da2lm_mods

def deploy_leftovers(override: Path, base: Path | None = None) -> list[dict]:
    """Files inside da2lm_mods that the current mod list doesn't account for: copies left from an old
    load order, files put there by hand, or mod files edited by hand. Each entry:
    {"rel", "path", "edited": a mod file changed by hand (else: belongs to no mod),
     "covers": [(mod name, rel)] newer mod files with the same name that load before it, so it wins over them}."""
    override = Path(override)
    root = override / DEPLOY_DIR
    if not root.is_dir():
        return []
    rec = Record.load(override, base)
    lib = Library(base).mods()
    target = _target(rec.order, lib) if rec.order else {}
    names = {m.id: m.name for m in lib.values()}
    by_name: dict[str, list] = {}
    for rel, (_src, mid) in target.items():
        if rel.startswith(DEPLOY_DIR + "/"):
            by_name.setdefault(rel.rsplit("/", 1)[-1].lower(), []).append((rel, names.get(mid, mid)))
    # folders da2lm has no record of at all are listed in the mod list as hand-installed ("lost its record")
    # and can be taken over from there; only folders da2lm knows are checked here
    known = {rel.split("/")[1] for rel in rec.placed if rel.startswith(DEPLOY_DIR + "/") and rel.count("/") >= 2}
    out = []
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = f.relative_to(override).as_posix()
        if rel.count("/") < 2 or rel.split("/")[1] not in known:
            continue
        info = rec.placed.get(rel)
        if rel in target and info and _ours(f, info):
            continue                                       # a file of a mod that's on: fine
        if info and rel not in target and _ours(f, info):
            continue                                       # da2lm placed it and removes it on the next apply
        folder = rel.split("/")[1] if rel.count("/") >= 2 else ""
        covers = [(m, r) for r, m in by_name.get(f.name.lower(), [])
                  if r != rel and r.split("/")[1] < folder]
        out.append({"rel": rel, "path": str(f), "edited": rel in target and bool(info), "covers": covers})
    return out

# --------------------------------------------------------------------------- taking over hand-installed mods

def manage(override: Path, folder: str, name: str | None = None, on: bool = True, src_root: Path | None = None,
           base: Path | None = None) -> LibMod:
    """Take a folder you installed by hand into the library, then remove the hand-installed copy
    (the data lives on in the library; with hard links nothing was copied). Turn it on/off with plan/apply."""
    src = Path(src_root or override) / folder
    lib = Library(base)
    m = lib.add_folder(src, name or folder)
    # verify every file reached the library before letting go of the original names
    for f in src.rglob("*"):
        if f.is_file():
            g = m.files_dir / f.relative_to(src)
            if not g.is_file() or g.stat().st_size != f.stat().st_size:
                shutil.rmtree(m.root)
                raise OSError(f"couldn't take over {folder}: {f.name} didn't reach the library")
    shutil.rmtree(src)
    rec = Record.load(override, base)
    rec.order.append({"id": m.id, "on": False})        # placed by the next plan/apply
    rec.save()
    return m


# --------------------------------------------------------------------------- update / remove / refresh

TRASH = "_removed"


def _drop_placed(override: Path, mid: str, base: Path | None):
    """Take one mod's placed files out of override (only those still da2lm's), forget them."""
    rec = Record.load(override, base)
    dropped = []
    for rel, info in list(rec.placed.items()):
        if info.get("mod") != mid:
            continue
        dropped.append(rel)
        f = _abs(override, rel)
        if f.exists() and _stamp(f) == info["stamp"]:
            f.unlink()
        rec.placed.pop(rel)
    rec.save()
    _prune_empty(override / DEPLOY_DIR)
    _prune_addins(override, Plan([], remove=[r for r in dropped if r.startswith(ADDIN_KEY)]))


def update_mod(override: Path, mid: str, src: Path, base: Path | None = None, log=print) -> dict:
    """Swap in a new version of a library mod, keeping its place and on/off state. Undo brings the old one back."""
    override = Path(override)
    lib = Library(base)
    rec = Record.load(override, base)
    order = list(rec.order)
    m = lib.replace_files(mid, src)
    _drop_placed(override, mid, base)
    p = plan(override, order, base)
    p.lines.insert(0, f"Updated {m.name}")
    done = apply(override, p, base, log, undo_entry={"updated": mid, "order": order})
    log(f"Updated {m.name}: {len(m.files)} files now.")
    done["name"] = m.name
    return done


def remove_mod(override: Path, mid: str, base: Path | None = None, log=print) -> dict:
    """Take a mod out of the game and out of the list. Its files wait in library/_removed until
    da2lm empties that (Undo brings it back until then)."""
    override = Path(override)
    lib = Library(base)
    rec = Record.load(override, base)
    before = list(rec.order)
    name = lib.mods()[mid].name
    p = plan(override, [i for i in before if i["id"] != mid], base)
    p.lines.insert(0, f"Removed {name}")
    done = apply(override, p, base, log, undo_entry={"removed": mid, "order": before})
    (lib.base / TRASH).mkdir(parents=True, exist_ok=True)
    dst = lib.base / TRASH / mid
    if dst.exists():
        shutil.rmtree(dst)
    shutil.move(str(lib.base / mid), str(dst))
    done["name"] = name
    return done


def empty_trash(base: Path | None = None, keep: set = frozenset()) -> int:
    d = Library(base).base / TRASH
    n = 0
    if d.is_dir():
        for x in d.iterdir():
            if x.name not in keep:
                shutil.rmtree(x, ignore_errors=True); n += 1
    return n


def refresh(override: Path, base: Path | None = None, log=print) -> dict | None:
    """Place again any library file edited since it was placed (e.g. you edited your own mod via
    'Open mod files'). Nothing else changes; not an undo step."""
    override = Path(override)
    rec = Record.load(override, base)
    if not rec.order:
        return None
    p = plan(override, rec.order, base)
    if not p.add and not p.remove:
        return None
    return apply(override, p, base, log, history=False)


# --------------------------------------------------------------------------- everything installed by hand

SET_ASIDE = "_set aside files"


def _engine_key(name: str) -> str:
    return name.upper()


def hand_installed(override: Path, base: Path | None = None) -> list[dict]:
    """What sits in override outside da2lm's control, in the order the game loads it.

    Each entry: {"kind": "folder" | "erf" | "loose" | "deploy", "name", "on", "files": [(path, path inside mod)]}.
    - a folder is one mod (named as the old numbered layout recorded it, else by its folder name);
    - an .erf at the top level is one mod, named after the file;
    - other top-level files (.uti, .gda, ...) are grouped by where they fall between those, because
      their position decides who they beat;
    - "deploy" marks where da2lm_mods itself sorts, so adopting keeps every winner the same.
    Folders turned off in the old layout (da2lm_disabled) come last, off.
    """
    from .index import _not_a_resource
    from . import loadorder as lo
    override = Path(override)
    names = lo._load_state(override)
    out, run = [], []
    # add-ins installed by hand (Documents\...\addins\<folder> with a manifest): they load before override
    ad = addins_dir(override)
    if ad.is_dir():
        ours = {rel[len(ADDIN_KEY):].split("/", 1)[0].lower()
                for rel in Record.load(override, base).placed if rel.startswith(ADDIN_KEY)}
        for d in sorted((x for x in ad.iterdir() if x.is_dir()), key=lambda x: _engine_key(x.name)):
            if d.name.lower() in ours or not is_addin_dir(d):
                continue
            files = [(f, f.relative_to(d).as_posix()) for f in d.rglob("*") if f.is_file()]
            if files:
                out.append({"kind": "addin", "name": d.name, "on": True, "files": files, "src": d, "folder": d.name})

    def close_run():
        if run:
            out.append({"kind": "loose", "name": "", "on": True, "files": [(f, f.name) for f in run]})
            run.clear()

    entries = [p for p in override.iterdir() if p.name not in lo.RESERVED or p.name == DEPLOY_DIR]
    for p in sorted(entries, key=lambda p: _engine_key(p.name)):
        if p.name == DEPLOY_DIR:
            close_run(); out.append({"kind": "deploy", "name": "", "on": True, "files": []})
            # folders in da2lm_mods that da2lm has no record of (its record was lost or deleted):
            # they still load in the game, so they're listed and can be taken over again, order kept
            known = {rel.split("/")[1] for rel in Record.load(override, base).placed if rel.count("/") >= 2}
            for d in sorted((x for x in p.iterdir() if x.is_dir() and x.name not in known),
                            key=lambda x: _engine_key(x.name)):
                files = [(f, f.relative_to(d).as_posix()) for f in d.rglob("*") if f.is_file()]
                if files:
                    m = PREFIX_RE.match(d.name)
                    out.append({"kind": "folder", "name": m.group(2) if m else d.name, "on": True,
                                "files": files, "src": d, "orphan": True})
        elif p.is_dir():
            files = [(f, f.relative_to(p).as_posix()) for f in p.rglob("*") if f.is_file()]
            if not files:
                continue
            close_run()
            m = PREFIX_RE.match(p.name)
            out.append({"kind": "folder", "name": names.get(p.name) or (m.group(2) if m else p.name),
                        "on": True, "files": files, "src": p})
        elif p.is_file() and p.suffix.lower() == ".erf":
            close_run(); out.append({"kind": "erf", "name": p.stem, "on": True, "files": [(p, p.name)], "src": p})
        elif p.is_file() and not _not_a_resource(p):
            run.append(p)
    close_run()
    loose = [e for e in out if e["kind"] == "loose"]
    for n, e in enumerate(loose, 1):
        e["name"] = "Loose files" + (f" {n}" if len(loose) > 1 else "")
    dd = lo.disabled_dir(override)
    if dd.is_dir():
        for p in sorted(dd.iterdir(), key=lambda p: _engine_key(p.name)):
            if p.is_dir() and p.name != SET_ASIDE:
                files = [(f, f.relative_to(p).as_posix()) for f in p.rglob("*") if f.is_file()]
                if files:
                    out.append({"kind": "folder", "name": names.get(p.name) or p.name, "on": False,
                                "files": files, "src": p})
    return out


def adopt(override: Path, wanted: list, base: Path | None = None, log=print) -> dict:
    """Take everything installed by hand into the library and place it all, in one step.

    wanted = load order, lowest first; each item is {"id", "on"} for a library mod or
    {"hand": <entry from hand_installed>, "on"}. Every file is linked into the library and
    checked BEFORE its original name is removed. Undo gives every file back under its
    original name (and the old numbered layout's record, if there was one).
    """
    override = Path(override)
    lib = Library(base)
    rec = Record.load(override, base)
    before = list(rec.order)
    order, adopted, taken = [], [], []
    for w in wanted:
        if "hand" not in w:
            order.append({"id": w["id"], "on": w["on"]})
            continue
        e = w["hand"]
        try:
            if e["kind"] == "addin":
                m = lib.add_files(e["files"], e["name"], str(e.get("src")), "addin", e["folder"])
            else:
                m = lib.add_files(e["files"], e["name"], str(e.get("src") or override))
        except OSError:
            for a in adopted:                              # nothing was removed yet: drop the half-made library
                shutil.rmtree(lib.base / a["id"], ignore_errors=True)
            raise
        adopted.append({"id": m.id, "back": [[rel, str(f)] for f, rel in e["files"]]})
        order.append({"id": m.id, "on": w["on"]})
        taken.append(e)
    # every file is safely in the library: now the original names can go
    for e in taken:
        for f, _ in e["files"]:
            Path(f).unlink()
        if e["kind"] in ("folder", "addin"):
            _prune_empty(Path(e["src"]))
    legacy = override / ".da2lm"
    kept = None
    if legacy.is_dir():
        kept = (base or app_dir()) / "legacy" / f"{_key(override)}-{len(rec.history)}"
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(kept))
    rec.order = before
    rec.save()
    p = plan(override, order, base)
    n = len(taken)
    p.lines.insert(0, f"Took over {n} mod{'s' if n != 1 else ''} installed by hand (order kept)")
    done = apply(override, p, base, log, undo_entry={"adopted": adopted, "order": before,
                                                       "legacy": str(kept) if kept else None})
    done["taken"] = n
    log(f"Took over {n} mods installed by hand; Undo gives them back under their original names.")
    return done


def _give_back(override: Path, entry: dict, base: Path | None, log):
    """Undo of adopt: files go back under their original names, library copies go away."""
    p = plan(override, entry["order"], base)
    apply(override, p, base, log)
    rec = Record.load(override, base)
    rec.history.pop()
    rec.save()
    lib = Library(base).mods()
    skipped = []
    for a in entry["adopted"]:
        m = lib.get(a["id"])
        if not m:
            continue
        ok = True
        for rel, orig in a["back"]:
            src, dst = m.files_dir / rel, Path(orig)
            if dst.exists():
                skipped.append(orig); ok = False; continue
            place(src, dst)
        if ok:
            shutil.rmtree(m.root)
    if entry.get("legacy") and Path(entry["legacy"]).is_dir() and not (override / ".da2lm").exists():
        shutil.move(entry["legacy"], str(override / ".da2lm"))
    if skipped:
        log(f"{len(skipped)} files already existed and were left alone; their library copy is kept.")
    log(f"Gave back {len(entry['adopted'])} mods under their original names.")


def current_order(override: Path, base: Path | None = None) -> list[dict]:
    """[{'id','name','on'}] for every library mod known to this override, in load order."""
    lib = Library(base).mods()
    rec = Record.load(override, base)
    out = [{"id": i["id"], "name": lib[i["id"]].name, "on": bool(i.get("on")), "kind": lib[i["id"]].kind}
           for i in rec.order if i["id"] in lib]
    # library mods this override has never seen (added by another tool or by hand): listed at the
    # bottom, off, so they show up instead of hiding; Apply records them
    known = {i["id"] for i in rec.order}
    for mid, m in lib.items():
        if mid not in known:
            out.append({"id": mid, "name": m.name, "on": False, "kind": m.kind, "new": True})
    return out


def display_name(rel_parts: tuple) -> str | None:
    """Mod name for a file under override/da2lm_mods/NNN_<name>/..., else None."""
    if len(rel_parts) > 2 and rel_parts[0].lower() == DEPLOY_DIR:
        m = PREFIX_RE.match(rel_parts[1])
        return m.group(2) if m else rel_parts[1]
    return None


# --------------------------------------------------------------------------- resource inventory

INVENTORY = "inventory"


def write_inventory(override: Path, base: Path | None = None) -> int:
    """List what every placed mod ships, archives opened, into <app>/inventory/<mod id>.json.

    Lets other tools see each mod's resources without reading the placed files (which are hard
    links, and some tools refuse to read through links). Only archive headers are read: cheap.
    A mod's file is rewritten only when its contents changed. Returns how many mods were written.
    """
    from .erf import read_erf, ErfError
    override = Path(override)
    rec = Record.load(override, base)
    lib = Library(base).mods()
    by_mod: dict[str, dict] = {}
    for rel, info in sorted(rec.placed.items()):
        mid = info.get("mod")
        f = _abs(override, rel)
        if not f.is_file():
            continue
        entry = {"size": f.stat().st_size}
        if f.suffix.lower() in (".erf", ".rim"):
            try:
                erf = read_erf(f)
                entry["erf"] = {"version": erf.version, "encryption": erf.encryption,
                                "compression": erf.compression,
                                "entries": [[e.name or f"#{e.name_hash:016x}", e.unpacked_size] for e in erf.entries]}
            except (ErfError, OSError, ValueError) as e:
                entry["error"] = str(e)
        by_mod.setdefault(mid, {})[rel] = entry
    out = (base or app_dir()) / INVENTORY
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for mid, files in by_mod.items():
        m = lib.get(mid)
        data = json.dumps({"id": mid, "name": m.name if m else mid, "kind": m.kind if m else "override",
                           "files": files}, indent=1, ensure_ascii=False)
        p = out / f"{mid}.json"
        if not p.is_file() or p.read_text("utf-8") != data:
            p.write_text(data, "utf-8"); n += 1
    for p in out.glob("*.json"):
        if p.stem not in by_mod:
            p.unlink()
    return n
