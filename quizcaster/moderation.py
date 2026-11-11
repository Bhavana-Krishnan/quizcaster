"""Display-name and comment filtering for anything shown on stream.

A compact built-in list plus leetspeak normalisation. Users can add words in
data/blocked_words.txt (one per line). Unsafe names are replaced, never shown.
"""
from __future__ import annotations

import re

from . import config

_BASE = """
fuck shit bitch cunt dick cock pussy asshole bastard slut whore porn nazi hitler
nigger nigga faggot fag retard rape rapist kys sex boob tits penis vagina wank
chutiya madarchod behenchod bhenchod bhosdi gandu lund randi harami
""".split()

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
_URL = re.compile(r"(https?://|www\.|\.(com|net|org|io|xyz|ly)\b)", re.I)
MAX_NAME = 24
SAFE_NAME = "A quiz player"


def _words() -> set[str]:
    words = set(_BASE)
    extra = config.DATA_DIR / "blocked_words.txt"
    if extra.exists():
        words |= {w.strip().lower() for w in extra.read_text(encoding="utf-8").splitlines() if w.strip()}
    return words


_BLOCKED = _words()
_SUBSTRING_OK = {"fuck", "shit", "bitch", "slut", "whore", "porn", "nigger", "nigga", "faggot", "retard", "rapist",
                 "chutiya", "madarchod", "behenchod", "bhenchod", "bhosdi", "gandu", "wank", "pussy", "asshole",
                 "motherf"} | {w for w in _BLOCKED if len(w) >= 7}


def _squash(text: str) -> str:
    t = text.lower().translate(_LEET)
    t = re.sub(r"[^a-z]", "", t)
    return re.sub(r"(.)\1{2,}", r"\1\1", t)  # fuuuuck -> fuuck


def is_offensive(text: str) -> bool:
    if not text:
        return False
    tokens = re.findall(r"[a-z0-9@$!]+", text.lower())
    for tok in tokens:
        sq = _squash(tok)
        if sq in _BLOCKED or re.sub(r"(.)\1+", r"\1", sq) in _BLOCKED:
            return True
    squashed = _squash(text)
    if squashed in _BLOCKED:                       # f.u.c.k, s h i t
        return True
    # substring check only for words that don't hide inside innocent ones ('Scunthorpe', 'grape')
    return any(w in squashed for w in _SUBSTRING_OK)


def safe_display_name(name: str) -> str:
    name = (name or "").strip().lstrip("@")
    name = re.sub(r"[​-‏‪-‮]", "", name)  # zero-width / bidi tricks
    if not name or is_offensive(name) or _URL.search(name):
        return SAFE_NAME
    return name if len(name) <= MAX_NAME else name[:MAX_NAME - 1] + "…"


def safe_comment(text: str) -> str | None:
    """Comment text for the dashboard feed; None if it should be hidden."""
    if is_offensive(text) or _URL.search(text or ""):
        return None
    return text[:200]
