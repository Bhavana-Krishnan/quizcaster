"""SQLite storage with encrypted secrets.

Tables
  settings       key/value (plain JSON) for non-secret settings
  secrets        key/value encrypted with Fernet (LLM key, Google client secret, OAuth tokens)
  question_sets  a named set of questions (topic, source)
  questions      individual questions, with approval flag
  streams        stream history
  quota_ledger   YouTube API units used, per Pacific day (YouTube resets quota at midnight PT)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_fernet: Fernet | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secrets  (key TEXT PRIMARY KEY, value BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS question_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    topic TEXT,
    source TEXT NOT NULL,             -- 'generated' | 'uploaded'
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id INTEGER NOT NULL REFERENCES question_sets(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    question TEXT NOT NULL,
    option_a TEXT NOT NULL, option_b TEXT NOT NULL,
    option_c TEXT NOT NULL, option_d TEXT NOT NULL,
    answer TEXT NOT NULL CHECK (answer IN ('A','B','C','D')),
    explanation TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL, set_id INTEGER, broadcast_id TEXT,
    title TEXT, theme TEXT, started_at TEXT, ended_at TEXT,
    questions_played INTEGER DEFAULT 0, end_reason TEXT,
    leaderboard TEXT
);
CREATE TABLE IF NOT EXISTS quota_ledger (
    day TEXT NOT NULL, method TEXT NOT NULL, units INTEGER NOT NULL,
    PRIMARY KEY (day, method)
);
"""


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        path = config.SECRET_KEY_PATH
        if not path.exists():
            path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        _fernet = Fernet(path.read_bytes().strip())
    return _fernet


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA foreign_keys = ON")
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def reset_for_tests(path) -> None:
    """Point the module at a fresh database file (tests only)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        config.DB_PATH = path


def execute(sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
    with _lock:
        cur = conn().execute(sql, params)
        conn().commit()
        return cur


def query(sql: str, params: tuple | list = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, params).fetchall()]


# ---------- settings ----------
def get_setting(key: str, default: Any = None) -> Any:
    rows = query("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(rows[0]["value"]) if rows else default


def set_setting(key: str, value: Any) -> None:
    execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))


# ---------- secrets ----------
def set_secret(key: str, value: str | dict | None) -> None:
    if value is None:
        execute("DELETE FROM secrets WHERE key=?", (key,))
        return
    raw = json.dumps(value).encode()
    execute("INSERT INTO secrets(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _get_fernet().encrypt(raw)))


def get_secret(key: str, default: Any = None) -> Any:
    rows = query("SELECT value FROM secrets WHERE key=?", (key,))
    if not rows:
        return default
    try:
        return json.loads(_get_fernet().decrypt(rows[0]["value"]))
    except InvalidToken:
        # secret.key changed or file tampered: treat as missing
        return default


def has_secret(key: str) -> bool:
    return bool(query("SELECT 1 FROM secrets WHERE key=?", (key,)))


# ---------- quota ----------
def quota_day() -> str:
    """YouTube quota resets at midnight Pacific Time. Approximate PT as UTC-8 (PST)
    / UTC-7 (PDT) using a simple DST rule; a few hours of slop is harmless here."""
    now = datetime.now(timezone.utc)
    year = now.year
    # DST: 2nd Sunday March 10:00 UTC to 1st Sunday Nov 09:00 UTC
    march = datetime(year, 3, 8, 10, tzinfo=timezone.utc)
    dst_start = march + timedelta(days=(6 - march.weekday()) % 7)
    nov = datetime(year, 11, 1, 9, tzinfo=timezone.utc)
    dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    offset = -7 if dst_start <= now < dst_end else -8
    return (now + timedelta(hours=offset)).strftime("%Y-%m-%d")


def add_quota(method: str, units: int) -> None:
    execute("INSERT INTO quota_ledger(day,method,units) VALUES(?,?,?) "
            "ON CONFLICT(day,method) DO UPDATE SET units=units+excluded.units",
            (quota_day(), method, units))


def quota_used_today() -> int:
    rows = query("SELECT COALESCE(SUM(units),0) AS u FROM quota_ledger WHERE day=?", (quota_day(),))
    return int(rows[0]["u"])


def quota_limit() -> int:
    return int(get_setting("youtube_daily_quota", config.DEFAULT_DAILY_QUOTA))


def quota_remaining() -> int:
    return max(0, quota_limit() - quota_used_today())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
