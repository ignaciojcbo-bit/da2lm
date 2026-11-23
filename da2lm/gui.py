"""da2lm desktop window (PySide6).

Run with:  py -m da2lm.gui      (needs: py -m pip install PySide6-Essentials)

Layout, top to bottom: a status banner that answers "can I play?", then tabs:
  Mods              tick = installed, drag to reorder, one "Apply changes" step
  Things to check   each clash in plain words, who wins, and what you can do about it
  Character creation  the merged chargenmorphcfg.xml
  Details / Log     the full technical report, for modders

The window only calls the engine; nothing here decides who wins. Reporting follows the
project rule: describe the clash and the winner, never assume it's a mistake.
"""
from __future__ import annotations

import os
import subprocess
import sys
import re
import traceback
import zipfile
from pathlib import Path

from PySide6.QtCore import Slot, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSplitter, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QMenu, QHeaderView)

from . import __version__
from . import chargen as cg
from . import loadorder as lo
from . import merge as mg
from . import overview as ovw
from .index import conflicts, connect, replacements, scan, summary
from .paths import find_game, find_override, is_game_dir, load_config, save_config, scan_roots, vanilla_dirs

APP_DIR = Path.home() / "AppData" / "Local" / "da2lm" if sys.platform == "win32" else Path.home() / ".da2lm"
DB_PATH = APP_DIR / "da2lm.db"

# Theme "Kirkwall": near-black stone, bone text, brass trim, DA2's blood red for the one action
# that matters. Every color is a token; the style sheet below is built from them.
T = {
    "bg": "#0f0b0a", "bg2": "#1a1210", "surface": "#171110", "raised": "#211816", "row": "#1c1513",
    "border": "#4a3329", "border_strong": "#7d5a46", "brass": "#b48a57", "brass_dim": "#6e5338",
    "text": "#ebe3d6", "text2": "#a99b8b", "heading": "#d9b77b",
    "accent": "#8b1a1a", "accent_hover": "#a82222", "accent_text": "#e46b5f",
    "ok": "#86b073", "warn": "#e2b456", "info": "#86a9c4", "bad": "#e0705f",
}
DISPLAY = "Cinzel"     # bundled (SIL Open Font License, assets/Cinzel-OFL.txt); falls back to a serif


def build_style(t=T, toggles=("", "")) -> str:
    on, off = toggles
    return f"""
QMainWindow {{ background: qradialgradient(cx:0.5, cy:0.15, radius:1.1, fx:0.5, fy:0.1,
                stop:0 {t['bg2']}, stop:1 {t['bg']}); }}
QWidget {{ color: {t['text']}; font-size: 10pt; }}
QWidget#page, QSplitter, QScrollArea {{ background: transparent; }}
QLabel {{ background: transparent; }}
QPushButton {{ background: {t['raised']}; border: 1px solid {t['border_strong']}; padding: 6px 14px; border-radius: 3px; }}
QPushButton:hover {{ border-color: {t['brass']}; background: #2a1e1b; }}
QPushButton:disabled {{ color: #6f6358; border-color: {t['border']}; background: {t['surface']}; }}
QPushButton#primary {{ background: #3a1f19; border-color: {t['brass']}; color: {t['heading']}; font-weight: bold; }}
QPushButton#primary:hover {{ background: #4a2820; }}
QPushButton#primary:disabled {{ background: {t['surface']}; border-color: {t['border']}; color: #6f6358; font-weight: normal; }}
QPushButton#play {{ background: {t['accent']}; border: 1px solid #c9473c; color: #fff2e6; font-family: "{DISPLAY}";
                   font-weight: bold; font-size: 15pt; padding: 10px 34px; letter-spacing: 2px; }}
QPushButton#play:hover {{ background: {t['accent_hover']}; }}
QPushButton#play:disabled {{ background: #3a1d1b; color: #8a6d65; border-color: #5a2f2a; }}
QPushButton#danger {{ background: transparent; border: 1px solid #6e2a24; color: {t['accent_text']}; }}
QPushButton#danger:hover {{ background: #2a1412; border-color: {t['accent_text']}; }}
QPushButton#link {{ background: transparent; border: none; color: {t['text2']}; text-decoration: underline; padding: 2px; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {t['border']}; top: -1px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {t['text2']}; font-family: "{DISPLAY}"; font-size: 12pt;
               padding: 8px 16px 6px 16px; margin-right: 6px; border: none; border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {t['text']}; }}
QTabBar::tab:selected {{ color: {t['heading']}; border-bottom: 2px solid {t['accent_text']}; }}
QListWidget, QTreeWidget, QPlainTextEdit {{ background: {t['surface']}; border: 1px solid {t['border']};
  alternate-background-color: {t['row']}; selection-background-color: #4a221d; selection-color: {t['text']};
  outline: none; }}
QListWidget::item {{ padding: 7px 6px; border-bottom: 1px solid #241a17; }}
QListWidget::item:selected {{ background: #3d1d19; border-left: 3px solid {t['accent_text']}; }}
QTreeWidget::item {{ padding: 6px 2px; }}
QTreeWidget::item:selected {{ background: #3d1d19; }}
QHeaderView::section {{ background: {t['raised']}; color: {t['text2']}; padding: 6px; border: none;
                       border-bottom: 1px solid {t['border']}; font-family: "{DISPLAY}"; }}
QTreeWidget::indicator, QListWidget::indicator {{ width: 34px; height: 18px; image: url("{off}"); }}
QTreeWidget::indicator:checked, QListWidget::indicator:checked {{ image: url("{on}"); }}
QFrame#choice {{ background: rgba(255, 240, 220, 0.025); border: 1px solid {t['border']}; border-radius: 3px; }}
QFrame#choice:hover {{ border-color: {t['brass']}; background: rgba(180, 138, 87, 0.08); }}
QFrame#choice[best="true"] {{ border-left: 3px solid {t['brass']}; }}
QFrame#choice[now="true"] {{ background: rgba(134, 176, 115, 0.05); }}
QLabel#choiceTitle {{ color: {t['heading']}; font-weight: bold; }}
QLabel#tag {{ color: {t['bg']}; background: {t['brass']}; border-radius: 2px; padding: 0 6px; font-size: 8pt;
              font-weight: bold; }}
QLabel#tag[kind="now"] {{ background: {t['ok']}; }}
QFrame#card {{ background: rgba(33, 24, 22, 0.92); border: 1px solid {t['border']}; border-radius: 2px; }}
QFrame#card[attn="true"] {{ border: 1px solid {t['warn']}; background: rgba(60, 40, 18, 0.92); }}
QLabel#groupLabel {{ color: {t['text2']}; font-family: "{DISPLAY}"; font-size: 9pt; letter-spacing: 1px; }}
QLabel#appTitle {{ font-family: "{DISPLAY}"; font-size: 13pt; color: {t['heading']}; letter-spacing: 3px; }}
QLabel#bannerTitle {{ font-family: "{DISPLAY}"; font-size: 19pt; font-weight: bold; color: {t['heading']}; }}
QLabel#sectionTitle {{ font-family: "{DISPLAY}"; font-size: 12pt; color: {t['heading']}; }}
QLabel#issueTitle {{ font-family: "{DISPLAY}"; font-size: 14pt; font-weight: bold; color: {t['heading']}; }}
QLabel#muted {{ color: {t['text2']}; }}
QLabel#statValue {{ font-family: "{DISPLAY}"; font-size: 17pt; color: {t['text']}; }}
QLabel#statValue[tone="ok"] {{ color: {t['ok']}; }}
QLabel#statValue[tone="warn"] {{ color: {t['warn']}; }}
QLabel#statValue[tone="bad"] {{ color: {t['bad']}; }}
QMessageBox QPushButton {{ min-width: 80px; }}
QLabel#toast {{ background: #1e3320; border: 1px solid #4f8a5b; color: #d8f0d8; padding: 7px 12px;
               border-radius: 2px; font-weight: bold; }}
QLabel#toast[kind="bad"] {{ background: #3c1a17; border-color: {t['accent_hover']}; color: #f6d5cf; }}
QProgressBar {{ border: none; background: {t['surface']}; height: 4px; }}
QProgressBar::chunk {{ background: {t['accent_text']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['border_strong']}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {t['border_strong']}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QDialog, QMessageBox {{ background: {t['raised']}; }}
"""


STYLE = build_style()

LEVEL_COLOR = {"CRITICAL": "#e07a5f", "CONFLICT": "#e07a5f", "WARNING": "#e9c46a", "CHECK": "#e9c46a",
               "NO EFFECT": "#e9c46a", "OVERRIDE": "#8fb3c9",
               "note": "#9fb7a0", "ok": "#9fb7a0", "info": "#b9b0a0"}
SEV_ICON = {ovw.ATTENTION: "⚠", ovw.INFO: "ℹ"}
SEV_COLOR = {ovw.ATTENTION: T["warn"], ovw.INFO: T["info"], "accepted": T["ok"]}


# --------------------------------------------------------------------------- look

def _short_join(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


class ChoiceCard(QFrame):
    """One option of an issue: what you pick, a tag (suggested / in use) and what it means. Click to do it."""
    def __init__(self, c, on_pick, parent=None):
        super().__init__(parent)
        self.setObjectName("choice")
        self.setProperty("best", bool(c.get("best"))); self.setProperty("now", bool(c.get("now")))
        self.c, self.on_pick = c, on_pick
        self.active = c.get("do") not in (None, "none") and not (c.get("now") and c.get("do") == "last")
        v = QVBoxLayout(self); v.setContentsMargins(14, 9, 14, 10); v.setSpacing(3)
        h = QHBoxLayout(); h.setSpacing(8)
        t = QLabel(c["label"]); t.setObjectName("choiceTitle"); h.addWidget(t)
        for on, word, kind in ((c.get("best"), "SUGGESTED", "best"), (c.get("now"), "IN USE", "now")):
            if on:
                tag = QLabel(word); tag.setObjectName("tag"); tag.setProperty("kind", kind); h.addWidget(tag)
        h.addStretch(1)
        if self.active:
            go = QLabel("Choose \u203a"); go.setObjectName("muted"); h.addWidget(go)
        v.addLayout(h)
        body = QLabel(c.get("text", "")); body.setWordWrap(True); body.setObjectName("muted")
        v.addWidget(body)
        if self.active:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("" if self.active else "This is what the game uses now.")

    def mouseReleaseEvent(self, e):
        if self.active and self.rect().contains(e.position().toPoint()):
            self.on_pick(self.c)
        super().mouseReleaseEvent(e)


class Card(QFrame):
    """A panel with brass corner brackets; an optional colored bar on the left shows a state."""
    def __init__(self, parent=None, bar=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.bar = bar

    def set_bar(self, color):
        self.bar = color
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        r = self.rect().adjusted(2, 2, -3, -3)
        p.setPen(QPen(QColor(T["brass"]), 2))
        n = 12
        for x, y, dx, dy in ((r.left(), r.top(), 1, 1), (r.right(), r.top(), -1, 1),
                             (r.left(), r.bottom(), 1, -1), (r.right(), r.bottom(), -1, -1)):
            p.drawLine(x, y, x + dx * n, y)
            p.drawLine(x, y, x, y + dy * n)
        if self.bar:
            p.fillRect(r.left() + 6, r.top() + 14, 4, r.height() - 28, QColor(self.bar))
        p.end()


class Backdrop(QWidget):
    """Window background: a warm vignette with a faint grain, painted once per size."""
    def __init__(self):
        super().__init__()
        self._cache = None
        self.setObjectName("page")

    def paintEvent(self, e):
        if self._cache is None or self._cache.size() != self.size():
            from PySide6.QtGui import QImage, QRadialGradient
            import random
            pm = QPixmap(self.size())
            p = QPainter(pm)
            g = QRadialGradient(self.width() * 0.5, self.height() * 0.05, max(self.width(), self.height()) * 0.95)
            g.setColorAt(0, QColor(T["bg2"])); g.setColorAt(1, QColor(T["bg"]))
            p.fillRect(pm.rect(), g)
            grain = QImage(160, 160, QImage.Format.Format_ARGB32)
            rnd = random.Random(7)
            for y in range(160):
                for x in range(160):
                    a = rnd.randint(0, 14)
                    grain.setPixelColor(x, y, QColor(255, 235, 210, a) if rnd.random() < 0.5 else QColor(0, 0, 0, a))
            p.drawTiledPixmap(pm.rect(), QPixmap.fromImage(grain))
            p.end()
            self._cache = pm
        QPainter(self).drawPixmap(0, 0, self._cache)


class Ornament(QWidget):
    """A thin brass rule that fades at both ends, with a small diamond in the middle."""
    def __init__(self, height=14):
        super().__init__()
        self.setFixedHeight(height)

    def paintEvent(self, e):
        from PySide6.QtGui import QLinearGradient, QPolygonF
        from PySide6.QtCore import QPointF
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        y, w = self.height() / 2, self.width()
        g = QLinearGradient(0, 0, w, 0)
        c0, c1 = QColor(T["brass"]), QColor(T["brass"])
        c0.setAlpha(0)
        g.setColorAt(0, c0); g.setColorAt(0.5, c1); g.setColorAt(1, c0)
        p.setPen(QPen(g, 1))
        p.drawLine(0, int(y), w, int(y))
        d = 4
        p.setBrush(QColor(T["accent_text"])); p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygonF([QPointF(w / 2, y - d), QPointF(w / 2 + d, y), QPointF(w / 2, y + d), QPointF(w / 2 - d, y)]))
        p.end()


def _short_change(what: str, keep: int = 3) -> str:
    """'Turn on: A, B, C, D, E' -> 'Turn on: A, B, C and 2 more' (the full text goes in the tooltip)."""
    out = []
    for part in what.split("; "):
        head, sep, rest = part.partition(": ")
        tail = ""
        if sep and head in ("Turn on", "Turn off", "Add-ins turned off"):
            if rest.endswith(" (files stay in da2lm's library)"):
                rest, tail = rest[:-len(" (files stay in da2lm's library)")], " (files stay in da2lm's library)"
            names = [n.strip() for n in rest.split(", ") if n.strip()]
            if len(names) > keep:
                rest = ", ".join(names[:keep]) + f" and {len(names) - keep} more"
        out.append(f"{head}{sep}{rest}{tail}" if sep else part)
    return "<br>".join(out)


def stat_card(title: str) -> tuple:
    """A Home card: returns (card, value label, detail label, button row)."""
    c = Card()
    v = QVBoxLayout(c)
    v.setContentsMargins(22, 16, 20, 16)
    t = QLabel(title); t.setObjectName("sectionTitle")
    val = QLabel(); val.setObjectName("statValue"); val.setWordWrap(True)
    det = QLabel(); det.setObjectName("muted"); det.setWordWrap(True); det.setTextFormat(Qt.TextFormat.RichText)
    row = QHBoxLayout()
    v.addWidget(t); v.addWidget(val); v.addSpacing(4); v.addWidget(det); v.addStretch(1); v.addLayout(row)
    return c, val, det, row


def _toggle_images(folder: Path) -> tuple[str, str]:
    """Draw the on/off switch used for mod ticks (Qt style sheets need image files)."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for on in (True, False):
        pm = QPixmap(34, 18)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(T["brass"] if on else T["border_strong"]), 1))
        p.setBrush(QColor("#5c1717" if on else "#191211"))
        p.drawRoundedRect(1, 1, 32, 16, 8, 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T["heading"] if on else "#6f6358"))
        p.drawEllipse(19 if on else 4, 4, 11, 10)
        p.end()
        f = folder / f"toggle_{'on' if on else 'off'}.png"
        pm.save(str(f))
        out.append(f.as_posix())
    return out[0], out[1]


def load_look(app) -> None:
    font = Path(__file__).parent / "assets" / "Cinzel.ttf"
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
    app.setStyleSheet(build_style(T, _toggle_images(APP_DIR / "ui")))
    icon = Path(__file__).parent / "assets" / "da2lm.ico"
    if icon.is_file():
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon)))


# --------------------------------------------------------------------------- background work

class Worker(QObject):
    log = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(self.log.emit))
        except Exception:                                      # shown to the user, never swallowed
            self.failed.emit(traceback.format_exc())


class Relay(QObject):
    """Lives in the window's thread, so a finished job's callback (often a lambda) runs there, not in the worker."""

    def __init__(self, fn, parent):
        super().__init__(parent)
        self.fn = fn

    @Slot(object)
    def deliver(self, value):
        self.fn(value)
        self.deleteLater()


def collect_report(con) -> dict:
    """Everything the Conflicts tab shows, as plain data (built in the worker thread)."""
    from .cli import TABLE_LEVEL, _classify, _describe, _not_chargen, _redundancy, _row_reports, _within_mod

    out = {"tables": [], "rows": [], "files": [], "within": [], "summary": summary(con)}
    rreps = _row_reports(con) or []
    for r in _not_chargen(replacements(con)):
        if r.risk != "critical":
            continue
        d, src = _classify(con, r)
        verdict = d.verdict if d else "unverified"
        level = TABLE_LEVEL[verdict]
        who = ", ".join(r.mods) + ("  [merged by da2lm]" if r.merged else "")
        out["tables"].append((level, r.key, who, _describe(d, src) + _redundancy(rreps, r, d)))
    from .rows import lines_for
    for rep in rreps:
        mods = ", ".join(f"{p.name} ({p.source})" for p in rep.parts if p.is_mod)
        out["rows"].append((rep.level, f"{rep.prefix}*", mods, lines_for(rep, limit=50)))
    for c in _not_chargen(conflicts(con)):
        if c.verdict == "identical":
            continue
        lines = [f"{cp.mod}: {Path(cp.file).name}{' :: ' + cp.inner_name if cp.inner_name else ' (loose)'}"
                 f"{'   <- active' if cp is c.winner else ''}" for cp in c.copies]
        level = c.risk.upper() if c.verdict == "real" else c.verdict
        out["files"].append((level, c.key, f"{', '.join(c.mods)}   ->  active: {c.winner.mod}", lines))
    for (mod, chain), n in sorted(_within_mod(con).items()):
        out["within"].append(("info", mod, f"{n} files", [chain]))
    return out


def refresh_outputs(override: Path, game: Path | None, con, log) -> list[str]:
    """Keep the files da2lm owns up to date, but only the ones the player already asked for."""
    done = []
    out = override / lo.MERGE_DIR
    vdirs = vanilla_dirs(game) if game else []
    from . import rowmerge as rm
    if vdirs and out.is_dir() and any(not rm.is_rowmerge_file(f.name) for f in out.glob("*.gda")):
        mg.run(override, vdirs, DB_PATH, apply=True, log=log, extra_roots=scan_roots(override)[:-1])
        done.append("merged tables")
    if vdirs and out.is_dir() and any(rm.is_rowmerge_file(f.name) for f in out.glob("*.gda")):
        before = {f.name: f.read_bytes() for f in out.glob("*.gda") if rm.is_rowmerge_file(f.name)}
        rm.run(override, vdirs, DB_PATH, apply=True, log=log, extra_roots=scan_roots(override)[:-1])
        after = {f.name: f.read_bytes() for f in out.glob("*.gda") if rm.is_rowmerge_file(f.name)} if out.is_dir() else {}
        if before != after:
            done.append("combined table rows")
    target = out / cg.FILE_NAME
    if target.exists() and cg.mod_copies(con):
        res = cg.build(con, vdirs, set(load_config().get("chargen_excluded", [])))
        if cg.render(res.config) != target.read_text("utf-8"):
            cg.write(override, res)
            done.append("character creation file")
    return done


# --------------------------------------------------------------------------- window

class FoldersDialog(QDialog):
    def __init__(self, parent, game, override):
        super().__init__(parent)
        self.setWindowTitle("Game and mod folders")
        self.game, self.override = game, override
        g = QGridLayout(self)
        g.addWidget(QLabel("<b>Dragon Age II install</b>"), 0, 0)
        self.g_lbl = QLabel(); self.g_lbl.setWordWrap(True)
        g.addWidget(self.g_lbl, 1, 0)
        b = QPushButton("Change..."); b.clicked.connect(self.pick_game)
        g.addWidget(b, 1, 1)
        g.addWidget(QLabel("<b>Override folder</b> (where mods go)"), 2, 0)
        self.o_lbl = QLabel(); self.o_lbl.setWordWrap(True)
        g.addWidget(self.o_lbl, 3, 0)
        b = QPushButton("Change..."); b.clicked.connect(self.pick_override)
        g.addWidget(b, 3, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.accept)
        g.addWidget(bb, 4, 0, 1, 2)
        self.resize(640, 220)
        self.refresh()

    def refresh(self):
        self.g_lbl.setText(str(self.game) if self.game else "Not found. Pick the folder that contains 'bin_ship' and 'packages'.")
        self.o_lbl.setText(str(self.override) + ("" if self.override.is_dir() else "   (doesn't exist yet)"))

    def pick_game(self):
        d = QFileDialog.getExistingDirectory(self, "Pick the Dragon Age II install folder", str(self.game or ""))
        if not d:
            return
        if not is_game_dir(d):
            QMessageBox.warning(self, "Not a Dragon Age II install",
                                "That folder has no packages\\core\\data\\2da.rim.\n"
                                "Pick the folder that contains 'packages' and 'bin_ship'.")
            return
        self.game = Path(d); save_config(game=d); self.refresh()

    def pick_override(self):
        d = QFileDialog.getExistingDirectory(self, "Pick your override folder", str(self.override))
        if d:
            self.override = Path(d); save_config(override=d); self.refresh()


class ModList(QTreeWidget):
    """Top-level-only drag and drop (no dropping a mod inside another)."""
    changed = Signal()

    def dropEvent(self, e):
        super().dropEvent(e)
        self.changed.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Dragon Age II mod loader  -  da2lm {__version__}")
        self.resize(1150, 760)
        self.game = find_game()
        self.override = find_override()
        self.thread = None
        self.busy = False
        self.issues: list = []
        self.snapshot: list = []
        self.cg_outcome = None

        root = Backdrop()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(16, 10, 16, 8)

        head = QHBoxLayout()
        title = QLabel("DRAGON AGE II  \u00b7  MOD LOADER"); title.setObjectName("appTitle")
        head.addWidget(title)
        head.addStretch(1)
        ver = QLabel(f"da2lm {__version__}"); ver.setObjectName("muted")
        head.addWidget(ver)
        v.addLayout(head)
        v.addWidget(Ornament())

        # banner: the answer to "can I play?"
        self.banner = Card()
        b = QHBoxLayout(self.banner)
        b.setContentsMargins(26, 14, 18, 14)
        txt = QVBoxLayout()
        self.b_title = QLabel(); self.b_title.setObjectName("bannerTitle")
        self.b_sub = QLabel(); self.b_sub.setObjectName("muted"); self.b_sub.setWordWrap(True)
        txt.addWidget(self.b_title); txt.addWidget(self.b_sub)
        b.addLayout(txt, 1)
        self.b_action = QPushButton(); self.b_action.setObjectName("primary")
        self.b_action.clicked.connect(self._banner_action)
        b.addWidget(self.b_action)
        self.play_btn = QPushButton("Play"); self.play_btn.setObjectName("play")
        self.play_btn.clicked.connect(self.play)
        b.addWidget(self.play_btn)
        v.addWidget(self.banner)
        self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6); self.progress.setVisible(False)
        v.addWidget(self.progress)
        self.toast = QLabel(); self.toast.setObjectName("toast"); self.toast.setWordWrap(True); self.toast.setVisible(False)
        v.addWidget(self.toast)
        self._toast_timer = QTimer(self); self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self.toast.setVisible(False))

        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)
        self.home_index = self.tabs.addTab(self._build_home_tab(), "Home")
        self.mods_index = self.tabs.addTab(self._build_mods_tab(), "Mods")
        self.issues_index = self.tabs.addTab(self._build_issues_tab(), "Things to check")
        self.cg_index = self.tabs.addTab(self._build_chargen_tab(), "Character creation")
        self.conf_tree = QTreeWidget()
        self.conf_tree.setHeaderLabels(["Level", "What", "Who"])
        self.conf_tree.setAlternatingRowColors(True)
        self.conf_tree.setColumnWidth(0, 120); self.conf_tree.setColumnWidth(1, 380)
        self.tabs.addTab(self._carded(self.conf_tree, "Technical report",
                                      "Everything the checks found, in modding terms. The other tabs say the same in plain words."),
                         "Details")
        self.log_box = QPlainTextEdit(readOnly=True)
        self.log_box.setFont(QFont("Consolas", 9))
        self.tabs.addTab(self._carded(self.log_box, "Log", "What da2lm did, step by step."), "Log")

        foot = QHBoxLayout()
        self.paths_lbl = QLabel(); self.paths_lbl.setObjectName("muted")
        foot.addWidget(self.paths_lbl, 1)
        fb = QPushButton("Game and mod folders..."); fb.setObjectName("link"); fb.clicked.connect(self.edit_folders)
        foot.addWidget(fb)
        v.addLayout(foot)

        self.refresh_paths()
        self.load_mods()
        QTimer.singleShot(0, self.run_scan)          # check right away, no button to remember

    # ------------------------------------------------------------------ feedback
    def notify(self, text, bad=False, seconds=12):
        """A clear 'this happened' line under the banner, so nobody has to guess whether a click worked."""
        self.toast.setProperty("kind", "bad" if bad else "ok")
        self.toast.style().unpolish(self.toast); self.toast.style().polish(self.toast)
        self.toast.setText(("\u2717  " if bad else "\u2713  ") + text)
        self.toast.setVisible(True)
        self._toast_timer.start(seconds * 1000)
        self.log(text)

    def _set_busy_ui(self, on):
        self.progress.setVisible(on)
        for b in (getattr(self, n, None) for n in ("i_do", "i_ok", "b_action", "cg_build_btn", "cg_clear_btn",
                                                    "apply_btn", "discard_btn")):
            if b is not None:
                b.setEnabled(not on)
        if on:
            QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        else:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    # ------------------------------------------------------------------ banner
    def set_banner(self, state, title, sub="", action=None):
        self.banner.set_bar({"ok": T["ok"], "look": T["warn"], "busy": T["info"], "setup": T["bad"]}.get(state))
        self.b_title.setText(title); self.b_sub.setText(sub)
        self._banner_kind = action
        labels = {"setup": "Pick folders...", "look": "Show me", "apply": "Apply changes", "rescan": "Check again"}
        self.b_action.setVisible(action is not None)
        if action:
            self.b_action.setText(labels[action])

    def _banner_action(self):
        k = self._banner_kind
        if k == "setup":
            self.edit_folders()
        elif k == "look":
            self.tabs.setCurrentIndex(self.issues_index)
        elif k == "apply":
            self.apply_changes()
        elif k == "rescan":
            self.run_scan()

    def update_banner(self):
        if not self.game or not self.override.is_dir():
            what = "the game install" if not self.game else "your override folder"
            self.set_banner("setup", "Set up first", f"da2lm couldn't find {what}. Pick it once; it's remembered.", "setup")
            return
        if self.busy:
            self.set_banner("busy", "Checking your mods...", "Large texture packs take a while the first time; later checks are quick.")
            return
        if self.pending():
            self.set_banner("look", "Changes not applied yet",
                            self._pending_summary() + ". Apply them to update your override folder.", "apply")
            return
        look = ovw.needs_look(self.issues)
        if look:
            self.set_banner("look", f"{len(look)} thing{'s' if len(look) > 1 else ''} to check",
                            "Mods overlap in ways that may matter. You can play anyway; nothing is broken by this tool.",
                            "look")
        else:
            n = self.mods_list.topLevelItemCount()
            self.set_banner("ok", "Ready to play", f"{n} mods checked. No overlaps that need your attention.")

    # ------------------------------------------------------------------ folders
    def refresh_paths(self):
        self.paths_lbl.setText(f"Game: {self.game or 'not found'}     Override: {self.override}")
        self.play_btn.setEnabled(self._exe() is not None)
        self.update_banner()

    def edit_folders(self):
        d = FoldersDialog(self, self.game, self.override)
        d.exec()
        changed = (d.game, d.override) != (self.game, self.override)
        self.game, self.override = d.game, d.override
        self.refresh_paths()
        if changed:
            self.load_mods()
            self.run_scan()

    def _carded(self, widget, title, hint=""):
        w = QWidget(); w.setObjectName("page")
        outer = QVBoxLayout(w); outer.setContentsMargins(4, 12, 4, 4)
        c = Card(); v = QVBoxLayout(c); v.setContentsMargins(20, 14, 20, 18)
        t = QLabel(title); t.setObjectName("sectionTitle"); v.addWidget(t)
        if hint:
            h = QLabel(hint); h.setObjectName("muted"); v.addWidget(h)
        v.addWidget(widget, 1)
        outer.addWidget(c, 1)
        return w

    # ------------------------------------------------------------------ home
    def _build_home_tab(self):
        w = QWidget(); w.setObjectName("page")
        g = QGridLayout(w)
        g.setContentsMargins(4, 12, 4, 4); g.setSpacing(14)
        self.h_mods, self.h_mods_v, self.h_mods_d, r = stat_card("Your mods")
        b = QPushButton("Open the mod list"); b.clicked.connect(lambda: self.tabs.setCurrentIndex(self.mods_index)); r.addWidget(b)
        self.h_adopt = QPushButton("Let da2lm manage them"); self.h_adopt.setObjectName("primary")
        self.h_adopt.clicked.connect(self.adopt_all); r.addWidget(self.h_adopt)
        r.addStretch(1)
        self.h_check, self.h_check_v, self.h_check_d, r = stat_card("Things to check")
        b = QPushButton("Show me"); b.setObjectName("primary")
        b.clicked.connect(lambda: self.tabs.setCurrentIndex(self.issues_index)); r.addWidget(b); r.addStretch(1)
        self.h_comb, self.h_comb_v, self.h_comb_d, r = stat_card("Combined by da2lm")
        b = QPushButton("Character creation"); b.clicked.connect(lambda: self.tabs.setCurrentIndex(self.cg_index))
        r.addWidget(b); r.addStretch(1)
        self.h_last, self.h_last_v, self.h_last_d, r = stat_card("Last change")
        self.h_undo = QPushButton("Undo"); self.h_undo.clicked.connect(self.undo_order); r.addWidget(self.h_undo)
        r.addStretch(1)
        g.addWidget(self.h_mods, 0, 0); g.addWidget(self.h_check, 0, 1)
        g.addWidget(self.h_comb, 1, 0); g.addWidget(self.h_last, 1, 1)
        g.setRowStretch(2, 1)                              # cards keep their content height; space goes below
        g.setColumnStretch(0, 1); g.setColumnStretch(1, 1)
        return w

    def _tone(self, label, tone):
        label.setProperty("tone", tone)
        label.style().unpolish(label); label.style().polish(label)

    def refresh_home(self):
        w = self.wanted() if hasattr(self, "mods_list") else []
        on = sum(1 for _, e in w if e); off = len(w) - on
        by_hand = sum(1 for k, _ in w if k and k[0] == "hand")
        self.h_mods_v.setText(f"{on} on" + (f"  \u00b7  {off} off" if off else ""))
        self._tone(self.h_mods_v, "ok")
        self.h_adopt.setVisible(bool(by_hand)); self.h_adopt.setEnabled(not self.busy)
        self.h_mods_d.setText((f"{by_hand} installed by hand. da2lm can take them over keeping the order the game "
                               "uses now; after that you reorder and switch them here." if by_hand
                               else "All managed by da2lm: order them freely.")
                              + ("<br>Changes not applied yet." if self.pending() else ""))
        look = ovw.needs_look(self.issues)
        fyi = sum(1 for i in self.issues if i.severity == ovw.INFO and not i.accepted)
        ok = sum(1 for i in self.issues if i.accepted)
        self.h_check_v.setText(f"{len(look)} need a look" if look else "All clear")
        self._tone(self.h_check_v, "warn" if look else "ok")
        self.h_check.setProperty("attn", bool(look))
        self.h_check.style().unpolish(self.h_check); self.h_check.style().polish(self.h_check)
        self.h_check_d.setText("<br>".join(["\u26a0 " + i.title for i in look[:4]] +
                                           ([f"\u2026 and {len(look) - 4} more"] if len(look) > 4 else []) +
                                           [f"{fyi} for your information, {ok} marked OK by you."]))
        d = self.override / lo.MERGE_DIR
        files = [f.name for f in d.rglob("*") if f.is_file()] if d.is_dir() else []
        tables = [f for f in files if f.endswith(".gda") and "_da2lm_rows" not in f]
        rows = [f for f in files if "_da2lm_rows" in f]
        pref = [f for f in (d / "preferred").rglob("*") if f.is_file()] if (d / "preferred").is_dir() else []
        bits = []
        if tables:
            bits.append(f"\u2713 {len(tables)} whole table{'s' if len(tables) > 1 else ''} merged")
        if rows:
            bits.append(f"\u2713 shared entries combined in {len(rows)} table{'s' if len(rows) > 1 else ''} "
                        f"({', '.join(r.split('_da2lm_rows')[0].rstrip('_') for r in rows)})")
        if "chargenmorphcfg.xml" in files:
            n = sum(r.n_added for r in self.cg_outcome.results) if self.cg_outcome else 0
            bits.append(f"\u2713 character creation: {len(self.cg_outcome.results) if self.cg_outcome else '?'} mods, "
                        f"{n} options added")
        if pref:
            bits.append(f"\u2713 {len(pref)} files use the version you chose")
        self.h_comb_v.setText(f"{len(bits)} in use" if bits else "Nothing needed")
        self._tone(self.h_comb_v, "ok")
        self.h_comb_d.setText("<br>".join(bits) + ("<br>Kept up to date automatically when your mods change."
                                                   if bits else ""))
        from . import manager as mm
        last = mm.Record.load(self.override, APP_DIR).last
        if last:
            self.h_last_v.setText(last["when"].replace("T", "  "))
            self.h_last_d.setText(_short_change(last["what"]))
            self.h_last_d.setToolTip(last["what"].replace("; ", "\n"))
            self.h_undo.setEnabled(True)
        else:
            self.h_last_v.setText("None yet")
            self.h_last_d.setText("Changes made in the mod list show up here, each one with Undo.")
            self.h_undo.setEnabled(False)
        self._tone(self.h_last_v, "")

    # ------------------------------------------------------------------ mods tab
    def _build_mods_tab(self):
        w = QWidget()
        h = QHBoxLayout(w)
        self.mods_list = ModList()
        self.mods_list.setHeaderLabels(["Mod", "Notes"])
        hdr = self.mods_list.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)        # the mod name gets the room
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.mods_list.setRootIsDecorated(False)
        self.mods_list.setAlternatingRowColors(True)
        self.mods_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.mods_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.mods_list.itemChanged.connect(lambda *_: self.mods_changed())
        self.mods_list.changed.connect(self.mods_changed)
        card = Card()
        cl = QVBoxLayout(card); cl.setContentsMargins(18, 14, 18, 18)
        t = QLabel("Your mods"); t.setObjectName("sectionTitle")
        cl.addWidget(t)
        cl.addWidget(self.mods_list, 1)
        h.addWidget(card, 1)
        side = QVBoxLayout()
        help_ = QLabel("<b>Switch</b> a mod on or off. <b>Drag</b> to reorder: "
                       "<b>lower in the list wins</b> when two mods change the same thing.")
        help_.setWordWrap(True); help_.setFixedWidth(260); help_.setMinimumHeight(help_.heightForWidth(260))
        side.addWidget(help_)
        self.apply_btn = QPushButton("Apply changes"); self.apply_btn.setObjectName("primary")
        self.apply_btn.clicked.connect(self.apply_changes)
        self.discard_btn = QPushButton("Discard"); self.discard_btn.clicked.connect(self.load_mods)
        up = QPushButton("Move up"); up.clicked.connect(lambda: self.move(-1))
        down = QPushButton("Move down"); down.clicked.connect(lambda: self.move(1))
        undo = QPushButton("Undo")
        undo.setToolTip("Undo the last change da2lm made (apply, take over, update, remove)."); undo.clicked.connect(self.undo_order)
        self.sel_btn = QPushButton("Selected mod")             # Qt draws the menu arrow itself
        self.sel_menu = QMenu(self)
        self.sel_btn.setMenu(self.sel_menu)
        self.sel_menu.aboutToShow.connect(lambda: self._fill_mod_menu(self.sel_menu))
        self.mods_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mods_list.customContextMenuRequested.connect(self._mods_context)
        add_a = QPushButton("Archive...")
        add_a.setToolTip("Add a mod from a .zip, .7z or .rar as downloaded (.7z and .rar need 7-Zip)."); add_a.clicked.connect(self.add_archive)
        add_f = QPushButton("Folder...")
        add_f.setToolTip("Add a mod from a folder you already unpacked."); add_f.clicked.connect(self.add_folder)
        opn = QPushButton("Open override folder"); opn.clicked.connect(lambda: self._open(self.override))
        alloff = QPushButton("Turn all mods off"); alloff.setObjectName("danger")
        alloff.setToolTip("Switches off every mod da2lm manages, so the game runs as it ships "
                          "(handy before reporting a bug). Press Apply changes to do it; Undo brings them all back.")
        alloff.clicked.connect(self.all_off)
        self.adopt_btn = QPushButton("Let da2lm manage them"); self.adopt_btn.setObjectName("primary")
        self.adopt_btn.setToolTip("Takes over everything installed by hand, keeping the order the game uses now.")
        self.adopt_btn.clicked.connect(self.adopt_all)
        self.loose_lbl = QLabel(); self.loose_lbl.setWordWrap(True); self.loose_lbl.setObjectName("muted")
        self.loose_lbl.setFixedWidth(260)
        side.addWidget(self.loose_lbl)
        side.addWidget(self.adopt_btn)
        def group(title, *rows):
            side.addSpacing(6)
            lab = QLabel(title.upper()); lab.setObjectName("groupLabel"); side.addWidget(lab)
            for row in rows:
                if isinstance(row, tuple):                 # two buttons side by side
                    hb = QHBoxLayout(); hb.setSpacing(6)
                    for x in row:
                        hb.addWidget(x)
                    side.addLayout(hb)
                else:
                    side.addWidget(row)
        group("Order", (up, down), self.sel_btn)
        group("Changes", self.apply_btn, (self.discard_btn, undo))
        group("Add a mod", (add_a, add_f))
        side.addStretch(1)
        side.addWidget(opn)
        side.addSpacing(6)
        side.addWidget(alloff)
        h.addLayout(side)
        return w

    def all_off(self):
        ml = self.mods_list
        ml.blockSignals(True)
        n = 0
        for i in range(ml.topLevelItemCount()):
            it = ml.topLevelItem(i)
            key = it.data(0, Qt.ItemDataRole.UserRole + 1)
            if key and key[0] == "lib" and it.checkState(0) == Qt.CheckState.Checked:
                it.setCheckState(0, Qt.CheckState.Unchecked); n += 1
        ml.blockSignals(False)
        self.mods_changed()
        hand = len(self.by_hand())
        self.notify(f"{n} mods switched off. Press Apply changes to do it (Undo brings them back)."
                    + (f" {hand} mods installed by hand are still on: let da2lm manage them to switch those too."
                       if hand else ""))

    def load_mods(self):
        """Read the mods as they are on disk, in the order the game loads them (drops unapplied edits)."""
        ml = self.mods_list
        ml.blockSignals(True)
        ml.clear()
        self._hand = []
        if self.override.is_dir():
            from . import manager as mm
            lib = mm.current_order(self.override, APP_DIR)
            self._libinfo = {m["id"]: m for m in lib}
            if not getattr(self, "_trash_done", False):
                self._trash_done = True
                keep = {h["removed"] for h in mm.Record.load(self.override, APP_DIR).history
                        if isinstance(h, dict) and "removed" in h}
                try:
                    mm.empty_trash(APP_DIR, keep)
                except OSError:
                    pass
            self._hand = mm.hand_installed(self.override, APP_DIR)
            placed = False
            for i, e in enumerate(self._hand):
                if e["kind"] == "deploy":
                    for m in lib:
                        self._add_mod_item(m["name"], m["on"], ("lib", m["id"]))
                    placed = True
                else:
                    self._add_mod_item(e["name"], e["on"], ("hand", i))
            if not placed:
                for m in lib:
                    self._add_mod_item(m["name"], m["on"], ("lib", m["id"]))
        self.loose_lbl.setText("")
        ml.blockSignals(False)
        self.snapshot = self.wanted()
        self.decorate_mods()
        self.mods_changed()

    def _hand_note(self, key):
        e = self._hand[key[1]]
        if e.get("orphan"):
            return "in da2lm_mods, but da2lm lost its record of it"
        return {"folder": "installed by hand", "erf": "installed by hand (.erf in override)",
                "addin": "add-in installed by hand",
                "loose": f"{len(e['files'])} files loose in override"}[e["kind"]]

    def _add_mod_item(self, name, enabled, key):
        it = QTreeWidgetItem([name, ""])
        it.setData(0, Qt.ItemDataRole.UserRole, name)
        it.setData(0, Qt.ItemDataRole.UserRole + 1, key)
        it.setFlags((it.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
                    & ~Qt.ItemFlag.ItemIsDropEnabled)
        it.setCheckState(0, Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        self.mods_list.addTopLevelItem(it)

    def wanted(self):
        ml = self.mods_list
        return [(tuple(ml.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole + 1)),
                 ml.topLevelItem(i).checkState(0) == Qt.CheckState.Checked) for i in range(ml.topLevelItemCount())]

    def pending(self):
        return [] if self.wanted() == self.snapshot else ["changes"]

    def _pending_summary(self):
        before = dict(self.snapshot)
        on = [n for n, en in self.wanted() if en and not before.get(n)]
        off = [n for n, en in self.wanted() if not en and before.get(n)]
        moved = [n for n, en in self.wanted() if en] != [n for n, en in self.snapshot if en] and not (on or off)
        bits = []
        if on:
            bits.append(f"{len(on)} mod{'s' if len(on) > 1 else ''} to turn on")
        if off:
            bits.append(f"{len(off)} mod{'s' if len(off) > 1 else ''} to turn off")
        if moved or (not bits):
            bits.append("a new load order")
        return ", ".join(bits).capitalize()

    def mods_changed(self):
        if hasattr(self, "h_mods_v"):
            self.refresh_home()
        p = self.wanted() != self.snapshot
        self.apply_btn.setEnabled(p and not self.busy)
        self.discard_btn.setEnabled(p)
        self.apply_btn.setText("Apply changes" + (" *" if p else ""))
        hand = self.by_hand()
        self.adopt_btn.setVisible(bool(hand))
        self.adopt_btn.setEnabled(not self.busy)
        self.loose_lbl.setText(f"{len(hand)} mod{'s are' if len(hand) != 1 else ' is'} installed by hand. da2lm can take them over without changing "
                               "who wins anything, so you can reorder and switch them freely." if hand else "")
        self.loose_lbl.setMinimumHeight(self.loose_lbl.heightForWidth(260) if hand else 0)
        self.decorate_mods()
        self.update_banner()

    def decorate_mods(self):
        badges = ovw.mod_badges(self.issues)
        chargen = {m for m, _, _ in self._chargen_mods}
        ml = self.mods_list
        ml.blockSignals(True)
        for i in range(ml.topLevelItemCount()):
            it = ml.topLevelItem(i)
            name = it.data(0, Qt.ItemDataRole.UserRole)
            on = it.checkState(0) == Qt.CheckState.Checked
            notes = []
            key = it.data(0, Qt.ItemDataRole.UserRole + 1)
            if key and key[0] == "lib":
                info = getattr(self, "_libinfo", {}).get(key[1], {})
                if info.get("kind") == "addin":
                    notes.append("add-in")
                if info.get("new"):
                    notes.append("new in the library")
            if key and key[0] == "hand":
                notes.append(self._hand_note(key))
                if self._hand[key[1]]["kind"] == "loose":
                    it.setToolTip(0, "\n".join(rel for _, rel in self._hand[key[1]]["files"][:40]))
            if not on:
                notes.append("off")
            if on and badges.get(name):
                notes.append(f"⚠ {badges[name]} to check")
            if name in chargen:
                notes.append("character creation")
            it.setText(1, "   ".join(notes))
            it.setForeground(0, QColor("#e6e1d6" if on else "#7d756a"))
            it.setForeground(1, QColor("#e9c46a" if on and badges.get(name) else "#9a9184"))
        ml.blockSignals(False)

    _chargen_mods: list = []

    def move(self, step):
        ml = self.mods_list
        it = ml.currentItem()
        if not it:
            return
        row = ml.indexOfTopLevelItem(it)
        new = row + step
        if not 0 <= new < ml.topLevelItemCount():
            return
        ml.blockSignals(True)
        ml.takeTopLevelItem(row)
        ml.insertTopLevelItem(new, it)
        ml.blockSignals(False)
        ml.setCurrentItem(it)
        self.mods_changed()

    def _lib_base(self):
        return APP_DIR

    def confirm(self, title, paras, details=None, yes="Yes"):
        """A confirmation that always fits on screen: the gist on top, long lists in a scroll box."""
        d = QDialog(self); d.setWindowTitle(title); d.setMinimumWidth(560)
        v = QVBoxLayout(d); v.setContentsMargins(20, 18, 20, 16); v.setSpacing(10)
        t = QLabel(title); t.setObjectName("sectionTitle"); v.addWidget(t)
        for p in paras:
            l = QLabel(p); l.setWordWrap(True); l.setTextFormat(Qt.TextFormat.RichText); v.addWidget(l)
        if details:
            more = QPushButton("Show the full load order"); more.setCheckable(True)
            box = QPlainTextEdit("\n".join(details)); box.setReadOnly(True); box.setVisible(False)
            box.setMinimumHeight(220)
            more.toggled.connect(box.setVisible)
            more.toggled.connect(lambda on: more.setText("Hide the full load order" if on else "Show the full load order"))
            more.toggled.connect(lambda on: d.adjustSize())
            v.addWidget(more, 0, Qt.AlignmentFlag.AlignLeft); v.addWidget(box, 1)
        bb = QDialogButtonBox()
        ok = bb.addButton(yes, QDialogButtonBox.ButtonRole.AcceptRole); ok.setObjectName("primary"); ok.setDefault(True)
        bb.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(d.accept); bb.rejected.connect(d.reject)
        v.addWidget(bb)
        scr = self.screen().availableGeometry() if self.screen() else None
        if scr:
            d.setMaximumHeight(int(scr.height() * 0.85))
        return d.exec() == QDialog.DialogCode.Accepted

    def by_hand(self):
        return [k for k, _ in self.wanted() if k and k[0] == "hand"]

    def adopt_all(self):
        self.apply_changes(adopting=True)

    def apply_changes(self, adopting=False, ask=True):
        from . import manager as mm
        wanted = self.wanted()
        names = {tuple(self.mods_list.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole + 1)):
                 self.mods_list.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                 for i in range(self.mods_list.topLevelItemCount())}
        hand = [k for k, _ in wanted if k[0] == "hand"]
        before = dict(self.snapshot)
        on_now = [names[k] for k, en in wanted if en and not before.get(k)]
        off_now = [names[k] for k, en in wanted if not en and before.get(k)]
        old_order = [k for k, en in self.snapshot if en]
        new_order = [k for k, en in wanted if en]
        common = [k for k in new_order if k in old_order]
        moved = [names[k] for i, k in enumerate(common) if [x for x in old_order if x in common].index(k) != i]
        summary = []
        if on_now:
            summary.append("<b>Turn on:</b> " + ", ".join(on_now))
        if off_now:
            summary.append("<b>Turn off:</b> " + ", ".join(off_now) + " (its files stay in da2lm's library)")
        if moved:
            summary.append("<b>New position:</b> " + ", ".join(moved[:8]) + (f" and {len(moved) - 8} more" if len(moved) > 8 else ""))
        if hand:
            hn = [names[k] for k in hand]
            summary.append(f"<b>Taken over:</b> {', '.join(hn[:6])}" + (f" and {len(hn) - 6} more" if len(hn) > 6 else "")
                           + f" {'was' if len(hn) == 1 else 'were'} installed by hand. da2lm links "
                           f"{'it' if len(hn) == 1 else 'them'} into its library (no extra space), checks every file "
                           "before removing the old name, and keeps the order, so the game picks the same winners.")
        summary.append("'Undo' puts everything back.")
        full = [f"{n}. {names[k]}" for n, k in enumerate(new_order, 1)]
        offs = [names[k] for k, en in wanted if not en]
        if offs:
            full += ["", "Off: " + ", ".join(offs)]
        if ask and not self.confirm("Apply these changes?" if not (hand and not self.pending()) else
                                    "Let da2lm manage your mods?",
                                    summary, full, yes="Apply"):
            return
        base = self._lib_base()
        hand_items = self._hand
        order = [{"id": k[1], "on": on} if k[0] == "lib" else {"hand": hand_items[k[1]], "on": on} for k, on in wanted]

        def job(log):
            if hand:
                return mm.adopt(self.override, order, base, log=log)
            p = mm.plan(self.override, order, base)
            return mm.apply(self.override, p, base, log=log)

        def after(done):
            self.busy = False
            self._set_busy_ui(False)
            parts = []
            if done.get("taken"):
                parts.append(f"da2lm now manages {done['taken']} more mods, in the same order")
            parts.append(f"{done['linked'] + done['copied']} files placed ({done['linked']} as links, "
                         f"{done['copied']} copied)")
            if done["removed"]:
                parts.append(f"{done['removed']} removed")
            if done["blocked"]:
                parts.append(f"{done['blocked']} changed by hand and left alone")
            self._after_scan_note = ("Applied: " if not done.get("taken") else "") + ", ".join(parts) + "."
            self.load_mods()
            self.run_scan()

        self._start(job, after, busy="Taking over your mods..." if hand else "Applying...")

    def undo_order(self):
        from . import manager as mm
        r = mm.undo(self.override, self._lib_base(), log=self.log)
        if r == "update_undone":
            self._after_scan_note = "Undid the update: the previous version is back."
        elif r == "remove_undone":
            self._after_scan_note = "Undid the removal: the mod is back in its place."
        elif r == "gave_back":
            self._after_scan_note = "Undid the take-over: every mod is back in override under its original name."
        elif r:
            self._after_scan_note = "Undid the last change: the previous order is back."
        elif (self.override / lo.STATE_DIR).is_dir() and not self.by_hand_folders_gone() and lo.undo(self.override):
            self._after_scan_note = "Undid the last reorder: folder names are back."
        else:
            self.notify("Nothing to undo.")
            return
        self.load_mods()
        self.run_scan()

    def by_hand_folders_gone(self):
        """The old numbered layout's journal points at folders that no longer exist (taken over)."""
        import json as _j
        js = sorted((self.override / lo.STATE_DIR).glob("journal-*.json"))
        if not js:
            return True
        return any(not (self.override / new).exists() for _, new in _j.loads(js[-1].read_text("utf-8"))["renames"])

    # ------------------------------------------------------------------ one mod: update / files / remove
    def _selected_key(self):
        it = self.mods_list.currentItem()
        return (tuple(it.data(0, Qt.ItemDataRole.UserRole + 1)), it.data(0, Qt.ItemDataRole.UserRole)) if it else (None, None)

    def _mods_context(self, pos):
        it = self.mods_list.itemAt(pos)
        if not it:
            return
        self.mods_list.setCurrentItem(it)
        m = QMenu(self)
        self._fill_mod_menu(m)
        m.exec(self.mods_list.viewport().mapToGlobal(pos))

    def _fill_mod_menu(self, m):
        m.clear()
        key, name = self._selected_key()
        if not key:
            a = m.addAction("Select a mod in the list first"); a.setEnabled(False)
            return
        if key[0] != "lib":
            a = m.addAction(f"{name} is installed by hand"); a.setEnabled(False)
            m.addAction("Let da2lm manage it first...", self.adopt_all)
            return
        busy = self.busy or bool(self.pending())
        for label, fn in ((f"Update {name} from an archive...", lambda: self.update_mod(key[1], name, True)),
                          (f"Update {name} from a folder...", lambda: self.update_mod(key[1], name, False)),
                          (None, None),
                          ("Open its files", lambda: self.open_mod_files(key[1])),
                          (None, None),
                          (f"Remove {name} from da2lm...", lambda: self.remove_mod(key[1], name))):
            if label is None:
                m.addSeparator(); continue
            a = m.addAction(label, fn)
            if busy and not label.startswith("Open"):
                a.setEnabled(False); a.setToolTip("Apply or discard your pending changes first.")

    def update_mod(self, mid, name, archive):
        from . import manager as mm
        if archive:
            src, _ = QFileDialog.getOpenFileName(self, f"New version of {name}", str(Path.home() / "Downloads"),
                                                 "Mod archives (*.zip *.7z)")
        else:
            src = QFileDialog.getExistingDirectory(self, f"Folder with the new version of {name}")
        if not src:
            return
        if not self.confirm(f"Update {name}?",
                            [f"{name}'s files are replaced by the ones in <b>{Path(src).name}</b>. It keeps its place in "
                             "the list and stays on or off as it is.",
                             "Files the old version had and the new one doesn't are taken out of the game.",
                             "'Undo' brings the previous version back."], yes="Update"):
            return
        base, ov = self._lib_base(), self.override

        def job(log):
            return mm.update_mod(ov, mid, Path(src), base, log=log)

        def after(done):
            self.busy = False; self._set_busy_ui(False)
            self._after_scan_note = f"Updated {done['name']}: same place in the list, new files in the game."
            self.load_mods(); self.run_scan()

        self._start(job, after, busy=f"Updating {name}...")

    def open_mod_files(self, mid):
        from . import manager as mm
        m = mm.Library(self._lib_base()).mods().get(mid)
        if not m:
            return
        self._open(m.files_dir)
        self.notify("These are the mod's files in da2lm's library. Edit them here: da2lm places the changes in "
                    "the game the next time it checks (on start, before Play, or after any change).", seconds=14)

    def remove_mod(self, mid, name):
        from . import manager as mm
        if not self.confirm(f"Remove {name}?",
                            [f"{name} is taken out of the game and out of the list.",
                             "Its files wait in da2lm's bin until da2lm empties it (some changes later), so "
                             "'Undo' can bring it back until then. Your downloads aren't touched."],
                            yes="Remove"):
            return
        base, ov = self._lib_base(), self.override

        def job(log):
            return mm.remove_mod(ov, mid, base, log=log)

        def after(done):
            self.busy = False; self._set_busy_ui(False)
            self._after_scan_note = f"Removed {done['name']}. 'Undo' brings it back."
            self.load_mods(); self.run_scan()

        self._start(job, after, busy=f"Removing {name}...")

    def _add_to_library(self, fn, path):
        try:
            got = fn(path)
        except (OSError, RuntimeError, zipfile.BadZipFile) as e:
            self.notify(f"Couldn't add it: {e}", bad=True, seconds=20)
            return
        mods = got if isinstance(got, list) else [got]
        info = getattr(self, "_libinfo", None)
        if info is None:
            info = self._libinfo = {}
        for m in mods:
            info[m.id] = {"id": m.id, "name": m.name, "kind": m.kind}
            self._add_mod_item(m.name, True, ("lib", m.id))
        self.decorate_mods(); self.mods_changed()
        names = ", ".join(f"{m.name}{' (add-in)' if m.kind == 'addin' else ''}" for m in mods)
        self.notify(f"Added {names} to the library. New mods are at the bottom of the list: "
                    "drag them where you want and press Apply changes."
                    + (" Add-ins go to the add-ins folder, so their place in the list doesn't matter."
                       if any(m.kind == "addin" for m in mods) else ""))

    def add_archive(self):
        from . import manager as mm
        f, _ = QFileDialog.getOpenFileName(self, "Add a mod from an archive", str(Path.home() / "Downloads"),
                                           "Mod archives (*.zip *.7z *.rar)")
        if f:
            self._add_to_library(mm.Library(self._lib_base()).add_any, Path(f))

    def add_folder(self):
        from . import manager as mm
        d = QFileDialog.getExistingDirectory(self, "Add a mod from a folder (it's copied into da2lm's library)")
        if d:
            self._add_to_library(mm.Library(self._lib_base()).add_any, Path(d))

    def show_mods(self, names):
        self.tabs.setCurrentIndex(self.mods_index)
        ml = self.mods_list
        ml.clearSelection()
        for i in range(ml.topLevelItemCount()):
            it = ml.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) in names:
                it.setSelected(True)
                ml.scrollToItem(it)
                ml.setCurrentItem(it)

    # ------------------------------------------------------------------ things to check
    def _build_issues_tab(self):
        sp = QSplitter()
        sp.setHandleWidth(12)
        self.issue_list = QListWidget()
        self.issue_list.setWordWrap(True)
        self.issue_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.issue_list.currentRowChanged.connect(self.show_issue)
        lc = Card(); ll = QVBoxLayout(lc); ll.setContentsMargins(16, 14, 16, 16)
        ll.addWidget(self.issue_list)
        sp.addWidget(lc)
        right = Card()
        r = QVBoxLayout(right)
        r.setContentsMargins(22, 16, 18, 16)
        self.i_title = QLabel(); self.i_title.setObjectName("issueTitle"); self.i_title.setWordWrap(True)
        self.i_text = QLabel(); self.i_text.setWordWrap(True); self.i_text.setTextFormat(Qt.TextFormat.RichText)
        self.i_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        r.addWidget(self.i_title)
        body = QWidget(); body.setObjectName("page")
        bl = QVBoxLayout(body); bl.setContentsMargins(0, 0, 6, 0); bl.setSpacing(8)
        bl.addWidget(self.i_text)
        self.i_choices_title = QLabel("What you can do"); self.i_choices_title.setObjectName("sectionTitle")
        bl.addWidget(self.i_choices_title)
        self.i_choices = QVBoxLayout(); self.i_choices.setSpacing(8)
        bl.addLayout(self.i_choices)
        bl.addStretch(1)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        r.addWidget(scroll, 3)
        btns = QHBoxLayout()
        self.i_do = QPushButton(); self.i_do.setObjectName("primary"); self.i_do.clicked.connect(self.issue_action)
        self.i_show = QPushButton("Show in mod list"); self.i_show.clicked.connect(lambda: self.show_mods(self._issue.mods))
        self.i_ok = QPushButton(); self.i_ok.clicked.connect(self.toggle_accepted)
        self.i_more = QPushButton("Technical details"); self.i_more.setCheckable(True)
        self.i_more.toggled.connect(lambda on: self.i_details.setVisible(on))
        for x in (self.i_do, self.i_show, self.i_ok, self.i_more):
            btns.addWidget(x)
        btns.addStretch(1)
        r.addLayout(btns)
        self.i_details = QPlainTextEdit(readOnly=True); self.i_details.setFont(QFont("Consolas", 9))
        self.i_details.setVisible(False)
        r.addWidget(self.i_details, 2)
        sp.addWidget(right)
        sp.setSizes([430, 700])
        self._issue = None
        self._show_issue_widgets(False)
        return sp

    def _show_issue_widgets(self, on):
        for x in (self.i_do, self.i_show, self.i_ok, self.i_more):
            x.setVisible(on)
        if not on:
            self.i_details.setVisible(False)
            while self.i_choices.count():
                w = self.i_choices.takeAt(0).widget()
                if w:
                    w.deleteLater()
            self.i_choices_title.setVisible(False)

    def fill_issues(self):
        il = self.issue_list
        il.blockSignals(True)
        il.clear()
        groups = [("NEEDS A LOOK", [i for i in self.issues if i.severity == ovw.ATTENTION and not i.accepted]),
                  ("FOR YOUR INFORMATION", [i for i in self.issues if i.severity == ovw.INFO and not i.accepted]),
                  ("MARKED OK BY YOU", [i for i in self.issues if i.accepted])]
        for title, items in groups:
            if not items:
                continue
            h = QListWidgetItem(f"{title.capitalize()}  \u00b7  {len(items)}")
            h.setFlags(Qt.ItemFlag.NoItemFlags)
            h.setForeground(QColor(T["heading"]))
            hf = QFont(DISPLAY, 10); h.setFont(hf)
            il.addItem(h)
            for i in items:
                it = QListWidgetItem(f"{SEV_ICON[i.severity]}  {i.title}")
                it.setData(Qt.ItemDataRole.UserRole, i)
                it.setForeground(QColor(SEV_COLOR["accepted" if i.accepted else i.severity]))
                if i.severity == ovw.ATTENTION and not i.accepted:
                    it.setBackground(QColor(226, 180, 86, 18))
                it.setToolTip(i.title)
                il.addItem(it)
        il.blockSignals(False)
        n = len(ovw.needs_look(self.issues))
        self.tabs.setTabText(self.issues_index, f"Things to check ({n})" if n else "Things to check")
        for row in range(il.count()):
            if il.item(row).data(Qt.ItemDataRole.UserRole):
                il.setCurrentRow(row)
                return
        self.i_title.setText("Nothing to check")
        self.i_text.setText("No mods overlap. Have fun.")
        self._show_issue_widgets(False)

    def show_issue(self, row):
        it = self.issue_list.item(row) if row >= 0 else None
        i = it.data(Qt.ItemDataRole.UserRole) if it else None
        self._issue = i
        if not i:
            return
        self._show_issue_widgets(True)
        self.i_title.setText(i.title)
        paras = "".join(f"<p>{t}</p>" for t in i.text)
        if i.winner and i.winner != "merged":
            paras += f"<p><b>Used in game:</b> {i.winner}</p>"
        self.i_text.setText(paras)
        if i.action == "prefer" and i.files:
            self.i_do.setProperty("best", i.files[0])
        acts = {"merge": "Merge them", "order": "Change the order...", "chargen": "Open character creation",
                "setaside": "Set the files aside" if len(i.files) > 1 else "Set the file aside",
                "prefer": "Use the suggested version"}
        self.i_do.setVisible(i.action in acts)
        if i.action in acts:
            self.i_do.setText(acts[i.action] if i.action != "prefer" else f"Use {i.files[0]}'s version")
        self.i_show.setVisible(i.action != "order" and bool(i.mods))
        self.i_ok.setVisible(i.severity == ovw.ATTENTION)
        self.i_ok.setText("Show it as a problem again" if i.accepted else "It's intended - mark OK")
        self.i_more.setVisible(bool(i.details))
        self.i_details.setPlainText("\n".join(i.details))
        while self.i_choices.count():
            w = self.i_choices.takeAt(0).widget()
            if w:
                w.deleteLater()
        ch = list(i.choices)
        ch.sort(key=lambda c: (not c.get("best"), not c.get("now")))
        for c in ch:
            self.i_choices.addWidget(ChoiceCard(c, self.pick_choice))
        self.i_choices_title.setVisible(bool(ch))
        if ch and any(c["do"] == i.action for c in ch):
            self.i_do.setVisible(False)               # the choices above already offer it

    def pick_choice(self, c):
        if self.busy:
            return
        if c["do"] == "merge":
            self.merge_tables()
        elif c["do"] == "prefer":
            self._prefer(c["mod"], c["files"])
        elif c["do"] == "last":
            self._make_last(c["mod"], self._issue.mods if self._issue else [])
        elif c["do"] == "prefer_game":
            self._prefer_game(c["mod"], c["files"][0])
        elif c["do"] == "unprefer":
            self._unprefer(c["mod"], c["files"])

    def _prefer_game(self, mod, key):
        from .merge import vanilla_table_source
        if not self.game:
            self.notify("Pick the game folder first (link at the bottom): the original table is read from it.", bad=True)
            return
        if QMessageBox.question(self, "Use the game's table?",
                                f"da2lm puts a copy of the game's original {key} in {lo.MERGE_DIR}\\preferred\\"
                                f"{ovw.GAME_PICK}, which loads after every mod, so {mod}'s copy stops applying.\n\n"
                                f"Other files of {mod} stay in use. Your mod folders aren't touched, and the same issue "
                                "offers to go back.") != QMessageBox.StandardButton.Yes:
            return
        try:
            data, _ = vanilla_table_source([Path(d) for d in vanilla_dirs(self.game)], key)
            if data is None:
                raise OSError(f"{key} wasn't found in the game's files")
            d = self.override / lo.MERGE_DIR / "preferred" / ovw.GAME_PICK
            d.mkdir(parents=True, exist_ok=True)
            (d / key).write_bytes(data)
        except Exception as e:
            self.notify(f"Couldn't do it: {e}", bad=True)
            return
        self._after_scan_note = f"Now using the game's {key} instead of {mod}'s."
        self.run_scan()

    def _unprefer(self, mod, files):
        root = (self.override / lo.MERGE_DIR / "preferred").resolve()
        mine = [Path(f) for f in files if root in Path(f).resolve().parents]   # only da2lm's own copies
        if not mine:
            self.notify("Nothing to remove.")
            return
        if QMessageBox.question(self, "Go back?", f"Removes da2lm's {len(mine)} cop{'y' if len(mine) == 1 else 'ies'} "
                                f"in {lo.MERGE_DIR}\\preferred. Your mod files aren't touched.") \
                != QMessageBox.StandardButton.Yes:
            return
        for f in mine:
            if f.exists():
                f.unlink()
        from .outputs import tidy
        for d in sorted({f.parent for f in mine}, key=lambda d: -len(d.parts)):
            for x in (d, d.parent):
                if x.is_dir() and x != self.override and not any(x.iterdir()):
                    x.rmdir()
        tidy(self.override)
        self._after_scan_note = f"Back to the normal order: {mod}'s version applies again."
        self.run_scan()

    def _prefer(self, best, files):
        from .merge import _read_resource
        d = self.override / lo.MERGE_DIR / "preferred" / re.sub(r'[<>:"/\\|?*]+', "_", best)
        if QMessageBox.question(self, f"Use {best}'s version?",
                                f"da2lm puts loose copies of {best}'s {len(files)} file{'s' if len(files) > 1 else ''} in "
                                f"{lo.MERGE_DIR}\\preferred, which loads after every mod, so the game uses them.\n\n"
                                "Your mod folders aren't touched. Delete that folder to go back.") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            d.mkdir(parents=True, exist_ok=True)
            for f, inner, key in files:
                (d / key).write_bytes(_read_resource(f, inner))
        except OSError as e:
            self.notify(f"Couldn't write them: {e}", bad=True)
            return
        self._after_scan_note = (f"Now using {best}'s version of {len(files)} files (loose copies in "
                                 f"{lo.MERGE_DIR}\\preferred, which loads last).")
        self.run_scan()

    def _make_last(self, mod, involved):
        """Move `mod` just below the other mods of this issue (nothing else moves), then apply."""
        ml = self.mods_list
        rows = {ml.topLevelItem(n).data(0, Qt.ItemDataRole.UserRole): n for n in range(ml.topLevelItemCount())}
        if mod not in rows:
            self.notify(f"{mod} isn't in the mod list.", bad=True)
            return
        others = [rows[m] for m in involved if m != mod and m in rows]
        target = max(others, default=rows[mod])
        if target <= rows[mod]:
            self.notify(f"{mod} already loads after the others.")
            return
        names = [m for m in involved if m != mod and m in rows]
        msg = (f"{mod} moves just below {_short_join(names)}, so it wins wherever they overlap "
               "(not only here: everywhere these mods touch the same things). Nothing else moves.")
        if self.by_hand():
            msg += ("\n\nda2lm also takes over the mods you installed by hand, keeping their order, so it can move "
                    "this one without renaming folders. 'Undo' puts everything back.")
        else:
            msg += "\n\n'Undo' puts it back."
        if QMessageBox.question(self, f"Let {mod} win?", msg) != QMessageBox.StandardButton.Yes:
            return
        ml.blockSignals(True)
        it = ml.takeTopLevelItem(rows[mod])
        ml.insertTopLevelItem(target, it)          # target index shifts down by one after the take: lands right below
        ml.blockSignals(False)
        self.mods_changed()
        self.apply_changes(ask=False)

    def issue_action(self):
        i = self._issue
        if not i:
            return
        if i.action == "order":
            self.show_mods(i.mods)
        elif i.action == "chargen":
            self.tabs.setCurrentIndex(self.cg_index)
        elif i.action == "merge":
            self.merge_tables()
        elif i.action == "prefer":
            self._prefer(i.files[0], i.files[1:])
        elif i.action == "setaside":
            from .known import set_aside
            names = "\n".join(Path(f).name for f in i.files[:12]) + (f"\n... and {len(i.files) - 12} more" if len(i.files) > 12 else "")
            if QMessageBox.question(self, "Set aside?", f"Move out of override (not deleted):\n\n{names}\n\n"
                                    "They go to da2lm_disabled\\_set aside files, next to override.") \
                    != QMessageBox.StandardButton.Yes:
                return
            try:
                moved = [set_aside(self.override, f) for f in i.files]
            except OSError as e:
                self.notify(f"Couldn't move it: {e}", bad=True)
                return
            self._after_scan_note = f"Set aside {len(moved)} file{'s' if len(moved) > 1 else ''}: {names.replace(chr(10), ', ')}."
            self.run_scan()

    def toggle_accepted(self):
        i = self._issue
        acc = set(load_config().get("accepted", []))
        acc.symmetric_difference_update({i.id})
        save_config(accepted=sorted(acc))
        i.accepted = i.id in acc
        self.fill_issues()
        self.decorate_mods()
        self.update_banner()
        self.notify("Marked OK: moved to 'Marked OK by you' at the bottom of the list." if i.accepted
                    else "Back in 'Needs a look'.")

    def merge_tables(self):
        if not self.game:
            QMessageBox.information(self, "Merge", "Pick the game folder first: merging compares with the game's tables.")
            return
        ov, game = self.override, self.game

        def job(log):
            from . import rowmerge as rm
            res = mg.run(ov, vanilla_dirs(game), DB_PATH, apply=True, log=log, extra_roots=scan_roots(ov)[:-1])
            for r in res:
                log(f"merge {r.table}: " + (r.error or f"{r.mods_merged} mods merged"))
            rows = rm.run(ov, vanilla_dirs(game), DB_PATH, apply=True, log=log, extra_roots=scan_roots(ov)[:-1])
            for line in rm.report_lines(rows):
                log(line)
            n_tab = sum(1 for r in res if r.data and r.mods_merged >= 2)
            return n_tab, [f for f in rows if f.data]

        def after(result):
            self.busy = False
            self._set_busy_ui(False)
            n_tab, fams = result
            bits = []
            if n_tab:
                bits.append(f"{n_tab} whole table{'s' if n_tab > 1 else ''} merged")
            if fams:
                bits.append(f"{sum(len(f.rows) for f in fams)} shared entries combined in {len(fams)} table"
                            f"{'s' if len(fams) > 1 else ''}")
            self._after_scan_note = ("Done: " + ", ".join(bits) + "." if bits else "Nothing needed combining.")
            self.run_scan()

        self._start(job, after, busy="Combining mods...")

    # ------------------------------------------------------------------ scan
    def log(self, text):
        self.log_box.appendPlainText(text)

    def run_scan(self, then=None):
        if self.busy or not self.override.is_dir():
            self.update_banner()
            return
        game, ov = self.game, self.override
        accepted = load_config().get("accepted", [])

        def job(log):
            from . import outputs
            from . import manager as mm
            APP_DIR.mkdir(parents=True, exist_ok=True)
            try:
                if mm.refresh(ov, APP_DIR, log=log):
                    log("Placed again the mod files you edited in da2lm's library.")
            except OSError as e:
                log(f"Couldn't refresh edited mod files: {e}")
            try:
                mm.write_inventory(ov, APP_DIR)
            except OSError as e:
                log(f"Couldn't write the mod inventory: {e}")
            vd = vanilla_dirs(game) if game else []
            con = scan(DB_PATH, scan_roots(ov), vanilla=vd, log=log)
            stale = outputs.is_stale(ov, con)
            redone = refresh_outputs(ov, game, con, log)
            if redone:
                log("Updated " + " and ".join(redone) + " to match your mods.")
                con.close()
                con = scan(DB_PATH, scan_roots(ov), vanilla=vd, log=log)
            outputs.stamp(ov, con)
            rep = collect_report(con)
            rep["issues"] = ovw.build(con, accepted)
            try:
                lo = ovw.leftover_issue(mm.deploy_leftovers(ov, APP_DIR))
            except OSError as e:
                lo = None
                log(f"Couldn't look for leftover files: {e}")
            if lo:
                lo.accepted = lo.id in accepted
                rep["issues"].insert(0, lo)
            rep["stale_rebuilt"] = bool(stale and redone)
            rep["stale_unfixed"] = bool(outputs.is_stale(ov, con))
            return rep

        self._start(job, lambda rep: self.scan_done(rep, then))

    def _start(self, fn, on_done, busy=None):
        self.busy = True
        self._set_busy_ui(True)
        self._old_threads = [(t, wk) for t, wk in getattr(self, "_old_threads", []) if t.isRunning()]
        if self.thread is not None:
            self._old_threads.append((self.thread, self.worker))   # keep references until it has really stopped
        self.thread = QThread()
        self.worker = Worker(fn)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log)
        relay = Relay(on_done, self)
        self.worker.done.connect(relay.deliver)
        self.worker.failed.connect(self.scan_failed)
        for sig in (self.worker.done, self.worker.failed):
            sig.connect(self.thread.quit)
        self.thread.finished.connect(self._thread_finished)
        self.mods_changed()
        self.update_banner()
        if busy:
            self.b_title.setText(busy)
        self.thread.start()

    def _thread_finished(self):
        self.mods_changed()

    def scan_failed(self, tb):
        self.log(tb)
        if hasattr(self, "mods_list"):
            self.load_mods()
        self.busy = False
        self._set_busy_ui(False)
        self.notify("That didn't work. Details are in the Log tab; your files weren't changed.", bad=True, seconds=30)
        self.set_banner("setup", "Something went wrong", "Details are in the Log tab. Your files weren't changed.", "rescan")

    def scan_done(self, rep, then=None):
        self.busy = False
        self._set_busy_ui(False)
        if rep.get("stale_unfixed"):
            rep["issues"].insert(0, ovw.Issue(
                "stale", ovw.ATTENTION, [], "Merged files are out of date and couldn't be rebuilt",
                ["Some mod that the merged files were built from has changed, and da2lm couldn't rebuild them "
                 "(usually because the game folder isn't set). Until they're rebuilt they keep old decisions.",
                 "Set the game folder (link at the bottom), then check again."]))
        if rep.get("stale_rebuilt") and not getattr(self, "_after_scan_note", None):
            self._after_scan_note = ("Your mods changed since the merged files were last built, so da2lm rebuilt them "
                                     "(tables, combined rows, character creation).")
        self.issues = rep["issues"]
        self.fill_conflicts(rep)
        self.refresh_chargen()
        self.fill_issues()
        self.decorate_mods()
        self.mods_changed()
        s = rep["summary"]
        self.log(f"Checked {s['mods']} mods, {s['resources']} files.")
        if getattr(self, "_after_scan_note", None):
            n = len(ovw.needs_look(self.issues))
            self.notify(self._after_scan_note + (" Checked again: nothing left to look at." if not n else
                        f" Checked again: {n} thing{'s' if n > 1 else ''} left to look at."))
            self._after_scan_note = None
        if then:
            then()

    def fill_conflicts(self, rep):
        t = self.conf_tree
        t.clear()
        sections = [("Whole tables replaced", rep["tables"]), ("Row overlaps in multi-part tables", rep["rows"]),
                    ("Files shipped by more than one mod", rep["files"]),
                    ("Overrides inside a single mod folder", rep["within"])]
        for title, items in sections:
            top = QTreeWidgetItem(["", f"{title}  \u00b7  {len(items)}", ""])
            f = top.font(1); f.setBold(True); top.setFont(1, f)
            t.addTopLevelItem(top)
            for level, what, who, lines in items:
                node = QTreeWidgetItem([level, what, who])
                node.setForeground(0, QColor(LEVEL_COLOR.get(level, "#e6e1d6")))
                top.addChild(node)
                for line in lines:
                    child = QTreeWidgetItem(node, ["      " + line])
                    child.setFirstColumnSpanned(True)
                    child.setToolTip(0, line)
            top.setExpanded(bool(items))


    # ---- character creation
    def _build_chargen_tab(self):
        w = QWidget(); w.setObjectName("page")
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 12, 4, 4); v.setSpacing(12)
        intro = QLabel("The game reads a single list of character creation options (heads, hairs, beards, colours, "
                       "tattoos). da2lm combines every mod's list into one: the game's options first, then what each "
                       "mod adds, in load order. Nothing is removed, and your mod folders aren't touched.")
        intro.setWordWrap(True); intro.setObjectName("muted")
        v.addWidget(intro)
        h = QHBoxLayout(); h.setSpacing(14)
        lc = Card(); left = QVBoxLayout(lc); left.setContentsMargins(20, 14, 20, 18)
        t = QLabel("Mods with options"); t.setObjectName("sectionTitle"); left.addWidget(t)
        hint = QLabel("Switch one off to leave its options out."); hint.setObjectName("muted"); left.addWidget(hint)
        self.cg_list = QListWidget(); self.cg_list.setWordWrap(True)
        self.cg_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cg_list.itemChanged.connect(self._chargen_toggled)
        left.addWidget(self.cg_list, 1)
        h.addWidget(lc, 2)
        rc = Card(); right = QVBoxLayout(rc); right.setContentsMargins(20, 14, 20, 18)
        t = QLabel("What each one adds"); t.setObjectName("sectionTitle"); right.addWidget(t)
        self.cg_tree = QTreeWidget()
        self.cg_tree.setHeaderLabels(["Mod / section", "Options"])
        self.cg_tree.setColumnWidth(0, 360)
        self.cg_tree.setAlternatingRowColors(True)
        right.addWidget(self.cg_tree, 1)
        h.addWidget(rc, 3)
        v.addLayout(h, 1)
        bar = QHBoxLayout()
        self.cg_status = QLabel("Checking your mods...")
        self.cg_status.setWordWrap(True)
        bar.addWidget(self.cg_status, 1)
        self.cg_build_btn = QPushButton("Build combined list"); self.cg_build_btn.setObjectName("primary")
        self.cg_build_btn.clicked.connect(self.build_chargen)
        self.cg_clear_btn = QPushButton("Remove it"); self.cg_clear_btn.clicked.connect(self.clear_chargen)
        bar.addWidget(self.cg_build_btn); bar.addWidget(self.cg_clear_btn)
        v.addLayout(bar)
        self.cg_outcome = None
        return w

    def _chargen_excluded(self):
        return set(load_config().get("chargen_excluded", []))

    def refresh_chargen(self):
        if not DB_PATH.exists() or not self.override.is_dir():
            return
        con = connect(DB_PATH)
        excluded = self._chargen_excluded()
        copies = cg.mod_copies(con)
        self._chargen_mods = copies
        self.cg_list.blockSignals(True)
        self.cg_list.clear()
        for mod, file, inner in copies:
            item = QListWidgetItem(f"{mod}    ({Path(file).name}{' :: ' + inner if inner else ', loose'})")
            item.setData(Qt.ItemDataRole.UserRole, mod)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if mod in excluded else Qt.CheckState.Checked)
            if mod in excluded:
                item.setText(item.text() + "    - left out")
                item.setForeground(QColor("#7d756a"))
            self.cg_list.addItem(item)
        self.cg_list.blockSignals(False)
        self.cg_tree.clear()
        if not copies:
            self.cg_outcome = None
            self.cg_status.setText("")
            empty = QListWidgetItem("None of your mods adds character creation options.")
            empty.setFlags(Qt.ItemFlag.NoItemFlags); empty.setForeground(QColor(T["text2"]))
            self.cg_list.addItem(empty)
            self.cg_build_btn.setEnabled(False); self.cg_clear_btn.setEnabled(False)
            self.tabs.setTabText(self.cg_index, "Character creation")
            self._sync_chargen_issue("")
            return
        try:
            out = cg.build(con, vanilla_dirs(self.game) if self.game else [], excluded)
        except Exception:
            self.log(traceback.format_exc())
            self.cg_status.setText("Could not read the character-creation files - see the Log tab.")
            return
        self.cg_outcome = out
        self.cg_build_btn.setEnabled(not self.busy); self.cg_clear_btn.setEnabled(not self.busy)
        for r in out.results:
            node = QTreeWidgetItem([r.mod, f"+{r.n_added}"])
            f = node.font(0); f.setBold(True); node.setFont(0, f)
            self.cg_tree.addTopLevelItem(node)
            for sec, entries in r.added.items():
                sn = QTreeWidgetItem(node, [sec + ("   (new section)" if sec in r.new_sections else ""), str(len(entries))])
                for e in entries:
                    QTreeWidgetItem(sn, [e.name or "(empty)", " ".join(f'{k}={v}' for k, v in e.attrs if k.lower() != "name")])
            notes = []
            if r.already:
                notes.append(f"{r.already} entries already offered by the game or an earlier mod")
            if r.missing_from_game:
                notes.append(f"its copy doesn't list {r.missing_from_game} of the game's entries; they stay in")
            notes += r.errors
            for n in notes:
                c = QTreeWidgetItem(node, ["note: " + n])
                c.setFirstColumnSpanned(True)
                c.setToolTip(0, n)
                c.setForeground(0, QColor(LEVEL_COLOR["note"]))
            node.setExpanded(True)
        target = self.override / lo.MERGE_DIR / cg.FILE_NAME
        fresh = cg.render(out.config)
        current = target.read_text("utf-8") if target.exists() else None
        n_mods = len(out.results)
        if current is None:
            state = "Not built yet - until you build it, the game uses just one mod's copy."
            mark = " (!)" if n_mods > 1 else ""
        elif current == fresh:
            state = "Merged file is up to date."
            mark = ""
        else:
            state = "Merged file is out of date (mods or ticks changed) - build again."
            mark = " (!)"
        total = sum(r.n_added for r in out.results)
        self.cg_status.setText(f"{n_mods} mods included, {total} entries added to the game's list. {state}")
        self.tabs.setTabText(self.cg_index, "Character creation" + mark)
        self._sync_chargen_issue(mark, n_mods, current is None)

    def _sync_chargen_issue(self, mark, n_mods=0, missing=True):
        self.issues = [i for i in self.issues if i.id != "chargen"]
        if mark:
            mods = [r.mod for r in self.cg_outcome.results] if self.cg_outcome else []
            self.issues.insert(0, ovw.Issue(
                "chargen", ovw.ATTENTION, mods,
                "Character creation options from several mods aren't combined" if missing
                else "The combined character creation file is out of date",
                [f"{n_mods} mods bring their own list of character creation options (hairs, faces, colours...). "
                 "The game reads only one list, so without combining them you only see one mod's options.",
                 "The Character creation tab shows what each mod adds and builds the combined list."],
                action="chargen"))
        if hasattr(self, "issue_list"):
            self.fill_issues()
            self.decorate_mods()
            self.update_banner()

    def _chargen_toggled(self, item):
        mod = item.data(Qt.ItemDataRole.UserRole)
        excluded = self._chargen_excluded()
        if item.checkState() == Qt.CheckState.Checked:
            excluded.discard(mod)
        else:
            excluded.add(mod)
        save_config(chargen_excluded=sorted(excluded))
        self.refresh_chargen()

    def build_chargen(self):
        if not self.cg_outcome:
            QMessageBox.information(self, "Character creation", "Scan mods first.")
            return
        p = cg.write(self.override, self.cg_outcome)
        n = sum(r.n_added for r in self.cg_outcome.results)
        self._after_scan_note = (f"Character creation file built: {n} options from {len(self.cg_outcome.results)} mods "
                                 "added to the game's list.")
        self.log(f"Wrote {p}")
        self.run_scan()

    def clear_chargen(self):
        self._after_scan_note = ("Removed the combined character creation file." if cg.clear(self.override)
                                 else "There was no combined character creation file to remove.")
        self.run_scan()

    def closeEvent(self, e):
        for t in [self.thread] + [t for t, _ in getattr(self, "_old_threads", [])]:
            if t is not None and t.isRunning():
                t.quit(); t.wait(15000)
        super().closeEvent(e)

    # ------------------------------------------------------------------ play
    def _exe(self):
        if not self.game:
            return None
        exe = self.game / "bin_ship" / "DragonAge2.exe"
        return exe if exe.is_file() else None

    def _open(self, path):
        if sys.platform == "win32" and Path(path).exists():
            os.startfile(str(path))

    def play(self):
        exe = self._exe()
        if not exe:
            return
        if self.pending():
            r = QMessageBox.question(self, "Unapplied changes",
                                     "The mod list has changes that aren't applied, so the game won't see them.\n\n"
                                     "Apply them first?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                     | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel:
                return
            if r == QMessageBox.StandardButton.Yes:
                self.apply_changes()
                return
        if self.busy:
            self.notify("Wait a moment: da2lm is still working.", bad=True, seconds=5)
            return
        # re-check right before launching, so merged files built from old sources can't slip through
        self.b_title.setText("Checking before launch...")
        self.run_scan(then=lambda: self._launch(exe))

    def _launch(self, exe):
        try:
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
            self.notify("Game started. Merged files are up to date with your mods.")
        except OSError as e:
            QMessageBox.warning(self, "Couldn't start the game", str(e))


def main():
    app = QApplication(sys.argv)
    load_look(app)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
