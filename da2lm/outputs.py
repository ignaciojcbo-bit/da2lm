"""Fingerprint of everything da2lm's merged files are built from.

The merged tables, combined rows and character creation file are snapshots: if a source
mod changes and nobody rebuilds them, they keep an old decision without anyone noticing.
After every build da2lm stores a hash of all its inputs (every mod .gda and every
chargenmorphcfg.xml: which mod, which file, content hash). Any later difference means
the merged files are out of date, and the window rebuilds them before playing.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .loadorder import MERGE_DIR

STAMP = "da2lm_sources.json"


def fingerprint(con) -> tuple[str, int]:
    rows = con.execute("""SELECT m.name, r.name, r.file, IFNULL(r.inner_name, ''), IFNULL(r.sha1, '') FROM resources r
                          JOIN mods m ON m.id = r.mod_id
                          WHERE m.name != ? AND (r.ext = 'gda' OR r.name = 'chargenmorphcfg.xml')
                          ORDER BY 1, 2, 3, 4""", (MERGE_DIR,)).fetchall()
    h = hashlib.sha1()
    for r in rows:
        h.update("\x1f".join(map(str, r)).encode("utf-8", "replace") + b"\x1e")
    return h.hexdigest(), len(rows)


def has_outputs(root: Path) -> bool:
    d = Path(root) / MERGE_DIR
    return d.is_dir() and any(p.suffix.lower() in (".gda", ".xml") for p in d.iterdir())


def stamp(root: Path, con) -> None:
    d = Path(root) / MERGE_DIR
    if not has_outputs(root):
        if (d / STAMP).exists():
            (d / STAMP).unlink()
        return
    fp, n = fingerprint(con)
    (d / STAMP).write_text(json.dumps({"fingerprint": fp, "sources": n}), "utf-8")


def is_stale(root: Path, con) -> bool | None:
    """True if the merged files were built from different sources; None if there's nothing to compare."""
    p = Path(root) / MERGE_DIR / STAMP
    if not has_outputs(root):
        return None
    if not p.exists():
        return True                                  # built by an older version, or by hand
    try:
        old = json.loads(p.read_text("utf-8")).get("fingerprint")
    except (OSError, ValueError):
        return True
    return old != fingerprint(con)[0]


def tidy(root: Path) -> None:
    """After removing merged files: drop the stamp and the folder if nothing else is left."""
    d = Path(root) / MERGE_DIR
    if not d.is_dir():
        return
    if not has_outputs(root) and (d / STAMP).exists():
        (d / STAMP).unlink()
    if not any(d.iterdir()):
        d.rmdir()
