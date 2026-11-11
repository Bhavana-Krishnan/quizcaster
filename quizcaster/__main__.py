"""Desktop launcher:  python -m quizcaster  [--no-window] [--port 8765]

Starts the local server and opens the dashboard in its own app window
(Chrome/Edge app mode), falling back to the default browser.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser


def _preflight() -> list[str]:
    notes = []
    if not shutil.which("ffmpeg"):
        notes.append("FFmpeg not found: previews work, but going live needs it (see README).")
    try:
        import playwright  # noqa: F401
    except ImportError:
        notes.append("Playwright missing: run  pip install -r requirements.txt  then  playwright install chromium")
    return notes


def _find_app_browser() -> str | None:
    candidates = [
        shutil.which("google-chrome"), shutil.which("chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
        shutil.which("msedge"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    return next((c for c in candidates if c and os.path.exists(c)), None)


def _open_window(url: str) -> None:
    time.sleep(1.2)
    browser = _find_app_browser()
    if browser:
        subprocess.Popen([browser, f"--app={url}", "--window-size=1280,860"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def main() -> None:
    ap = argparse.ArgumentParser(prog="quizcaster")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-window", action="store_true", help="don't open the dashboard window")
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    args = ap.parse_args()
    if args.version:
        from . import __version__
        print(f"QuizCaster {__version__}")
        return
    if args.port:
        os.environ["QUIZCASTER_PORT"] = str(args.port)

    from . import config, db
    db.conn()
    if db.get_setting("remote_enabled", False) and "QUIZCASTER_HOST" not in os.environ:
        config.HOST = "0.0.0.0"          # mobile remote needs the LAN; non-local requests are PIN-gated

    import uvicorn
    from .server import app

    url = f"http://127.0.0.1:{config.PORT}/"
    print(f"\n  QuizCaster is running at {url}")
    for n in _preflight():
        print(f"  ! {n}")
    print("  Press Ctrl+C to quit.\n")
    if not args.no_window:
        threading.Thread(target=_open_window, args=(url,), daemon=True).start()
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
