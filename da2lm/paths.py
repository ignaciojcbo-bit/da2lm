"""Find the DA2 install and the user's override folder, with a saved fallback.

Order for the game install:
  1. a path saved earlier by the user (config file)
  2. Steam: registry SteamPath -> every library in libraryfolders.vdf -> steamapps/common/Dragon Age II
  3. EA app / Origin: registry keys BioWare\\Dragon Age 2, then the classic default folders
A candidate counts only if it contains packages/core/data/2da.rim.

The override folder lives under the real Documents folder, asked from Windows itself
(known-folder API), so a Documents folder moved by OneDrive is found too.

If nothing is found, the caller (CLI flag or GUI folder picker) supplies the path and
it's saved with save_config() for next time.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

GAME_MARKER = Path("packages") / "core" / "data" / "2da.rim"
OVERRIDE_SUB = Path("BioWare") / "Dragon Age 2" / "packages" / "core" / "override"

CLASSIC_GAME_DIRS = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Dragon Age II",
    r"C:\Program Files\Steam\steamapps\common\Dragon Age II",
    r"C:\Program Files (x86)\Origin Games\Dragon Age 2",
    r"C:\Program Files\Origin Games\Dragon Age 2",
    r"C:\Program Files\EA Games\Dragon Age 2",
    r"C:\Program Files (x86)\EA Games\Dragon Age 2",
]


def config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "da2lm" / "config.json"


def load_config() -> dict:
    p = config_path()
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(**values) -> None:
    cfg = load_config()
    for k, v in values.items():
        if isinstance(v, (list, tuple)):
            cfg[k] = list(v)
        elif v:
            cfg[k] = str(v)
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2), "utf-8")


def is_game_dir(p) -> bool:
    return bool(p) and (Path(p) / GAME_MARKER).is_file()


# --------------------------------------------------------------------------- Windows helpers

def _reg(root_name: str, key: str, value: str):
    if sys.platform != "win32":
        return None
    import winreg
    root = getattr(winreg, root_name)
    for view in (0, getattr(winreg, "KEY_WOW64_32KEY", 0), getattr(winreg, "KEY_WOW64_64KEY", 0)):
        try:
            with winreg.OpenKey(root, key, 0, winreg.KEY_READ | view) as k:
                return winreg.QueryValueEx(k, value)[0]
        except OSError:
            continue
    return None


def documents_dir() -> Path:
    """The real Documents folder (follows OneDrive / moved folders on Windows)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                            ("d", ctypes.c_ubyte * 8)]
            fid = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                return Path(path)
        except Exception:
            pass
    return Path(os.environ.get("USERPROFILE", Path.home())) / "Documents"


def steam_libraries() -> list[Path]:
    steam = _reg("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath") or \
        _reg("HKEY_LOCAL_MACHINE", r"SOFTWARE\Valve\Steam", "InstallPath")
    libs = []
    if steam:
        libs.append(Path(steam))
        vdf = Path(steam) / "steamapps" / "libraryfolders.vdf"
        try:
            libs += [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text("utf-8", "replace"))]
        except OSError:
            pass
    return list(dict.fromkeys(libs))


def game_candidates() -> list[Path]:
    out = [lib / "steamapps" / "common" / "Dragon Age II" for lib in steam_libraries()]
    for key in (r"SOFTWARE\BioWare\Dragon Age 2", r"SOFTWARE\WOW6432Node\BioWare\Dragon Age 2",
                r"SOFTWARE\EA Games\Dragon Age 2"):
        for value in ("Install Dir", "InstallDir", "Path"):
            v = _reg("HKEY_LOCAL_MACHINE", key, value)
            if v:
                out.append(Path(v))
    out += [Path(p) for p in CLASSIC_GAME_DIRS]
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------- public API

def find_game() -> Path | None:
    saved = load_config().get("game")
    if is_game_dir(saved):
        return Path(saved)
    for c in game_candidates():
        if is_game_dir(c):
            return c
    return None


def find_override() -> Path:
    saved = load_config().get("override")
    if saved and Path(saved).is_dir():
        return Path(saved)
    return documents_dir() / OVERRIDE_SUB


def vanilla_dirs(game: Path) -> list[Path]:
    """Game data in load order: base, then the official patch, then DLC."""
    dirs = [game / "packages" / "core" / "data", game / "packages" / "core" / "patch", game / "addins"]
    return [d for d in dirs if d.is_dir()]


def user_addins(override: Path) -> Path:
    """Documents\\BioWare\\Dragon Age 2\\addins, next to packages\\core\\override. Mods installed as
    add-ins (a manifest.xml plus core\\data\\*.erf) live here instead of in override."""
    return Path(override).parent.parent.parent / "addins"


def scan_roots(override: Path) -> list[Path]:
    """Everything da2lm checks, lowest priority first: user add-ins, then override."""
    a = user_addins(override)
    return ([a] if a.is_dir() else []) + [Path(override)]
