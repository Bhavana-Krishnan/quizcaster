"""Paths and tunable constants. Everything that controls timing or cost lives here."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
if getattr(sys, "frozen", False):
    # Packaged app: ROOT is a temporary bundle folder, so keep data in the user's home.
    _default_data = Path.home() / "QuizCaster" / "data"
else:
    _default_data = ROOT.parent / "data"           # running from the source checkout
DATA_DIR = Path(os.environ.get("QUIZCASTER_DATA", _default_data))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "quizcaster.db"
SECRET_KEY_PATH = DATA_DIR / "secret.key"
THUMB_DIR = DATA_DIR / "thumbnails"
THUMB_DIR.mkdir(exist_ok=True)

HOST = os.environ.get("QUIZCASTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("QUIZCASTER_PORT", "8765"))

# ---- Quiz timing (seconds) ----
INTRO_SECONDS = 6
QUESTION_READ_SECONDS = 4          # question alone on screen before options appear
ANSWER_WINDOW_SECONDS = 30         # max comment window; closes earlier on first correct
REVEAL_SECONDS = 6
EXPLAIN_SECONDS = 8
WHY_WINDOW_SECONDS = 10            # listen for "why?" after reveal
WHY_EXTRA_SECONDS = 6              # extra explanation time when someone asks why
LEADERBOARD_SECONDS = 5
FINAL_SECONDS = 15
TECH_BREAK_RETRY_SECONDS = 10

# ---- Rendering ----
STREAM_WIDTH = int(os.environ.get("QUIZCASTER_WIDTH", "720"))
STREAM_HEIGHT = int(os.environ.get("QUIZCASTER_HEIGHT", "1280"))
CAPTURE_FPS = 15                   # frames captured from the overlay page
OUTPUT_FPS = 30
VIDEO_BITRATE = "2500k"

# ---- YouTube quota (units). Costs per Google's published quota table; verify
# against https://developers.google.com/youtube/v3/determine_quota_cost ----
DEFAULT_DAILY_QUOTA = 10_000
QUOTA_COST = {
    "liveChatMessages.list": 5,
    "liveBroadcasts.list": 1,
    "liveBroadcasts.insert": 50,
    "liveBroadcasts.bind": 50,
    "liveBroadcasts.transition": 50,
    "liveStreams.insert": 50,
    "liveStreams.list": 1,
    "videos.update": 50,
    "videos.list": 1,
    "thumbnails.set": 50,
    "channels.list": 1,
}
QUOTA_SAFETY_MARGIN = 300          # end the stream gracefully before this much is left
MIN_CHAT_POLL_MS = 2000            # never poll faster than this (YouTube also sets a minimum)

# ---- LLM ----
QUESTION_BATCH_SIZE = 10
LLM_TIMEOUT_SECONDS = 90
COMMENTARY_POOL_SIZE = 30
