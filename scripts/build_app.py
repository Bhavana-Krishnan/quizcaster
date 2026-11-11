"""Build a standalone app folder with PyInstaller. Run from the repo root:

    python scripts/build_app.py

Output: dist/QuizCaster/  (zip it for release). Works on Windows and Linux.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sep = os.pathsep                                   # ';' on Windows, ':' elsewhere

cmd = [
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
    "--name", "QuizCaster",
    "--console",                                   # keep a console window: shows the address and errors
    "--add-data", f"{ROOT / 'quizcaster' / 'static'}{sep}quizcaster/static",
    "--collect-submodules", "uvicorn",
    "--collect-submodules", "starlette",
    "--collect-data", "certifi",
    str(ROOT / "run.py"),
]
subprocess.run(cmd, cwd=ROOT, check=True)
print("\nBuilt:", ROOT / "dist" / "QuizCaster")
