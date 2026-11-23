"""Double-click to open the da2lm window (no console window).

.pyw files run with pythonw, which has no console, so errors would be invisible:
any startup error is written to %LOCALAPPDATA%\\da2lm\\gui_error.log and shown in a box.
"""
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))            # the da2lm package next to this file
os.chdir(HERE)

try:
    from da2lm.gui import main
    sys.exit(main())
except SystemExit:
    raise
except BaseException:
    text = traceback.format_exc()
    log = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "da2lm" / "gui_error.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text, "utf-8")
    except OSError:
        pass
    if "PySide6" in text and "No module named" in text:
        msg = ("PySide6 is missing. Open a command prompt and run:\n\n    py -m pip install PySide6-Essentials\n\n"
               "Then open this file again.")
    else:
        msg = f"The loader couldn't start. Details saved to:\n{log}\n\n{text[-1500:]}"
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "DA2 Loader", 0x10)
    except Exception:
        print(msg)
