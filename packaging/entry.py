"""Entry point for the Windows .exe (PyInstaller). Same behaviour as 'Open DA2 Loader.pyw':
a startup error is written to %LOCALAPPDATA%\\da2lm\\gui_error.log and shown in a box."""
import os
import sys
import traceback
from pathlib import Path

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
    msg = f"The loader couldn't start. Details saved to:\n{log}\n\n{text[-1500:]}"
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "DA2 Loader", 0x10)
    except Exception:
        print(msg)
