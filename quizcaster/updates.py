"""Check GitHub Releases for a newer version. Never installs anything: it only tells
the user, so an update can't land in the middle of a live show.

The check sends no personal data, only a generic User-Agent, and is cached for a
week. It can be turned off in Setup, and it fails silently when offline.
"""
from __future__ import annotations

import re
import time

import requests

from . import __version__, db

REPO = "Bhavana-Krishnan/quizcaster"          # the public repository that publishes releases
API = f"https://api.github.com/repos/{REPO}/releases/latest"
CACHE_SECONDS = 7 * 24 * 3600          # check at most once a week
_cache: dict = {"at": 0.0, "data": None}


def _parse(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) or (0,)


def check(force: bool = False) -> dict:
    if not db.get_setting("check_updates", True):
        return {"enabled": False, "current": __version__}
    now = time.time()
    if not force and _cache["data"] and now - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    result = {"enabled": True, "current": __version__, "latest": None, "update_available": False,
              "url": None, "notes": None}
    try:
        r = requests.get(API, timeout=6, headers={"User-Agent": "QuizCaster-update-check",
                                                  "Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            data = r.json()
            latest = data.get("tag_name", "").lstrip("v")
            result.update(latest=latest, url=data.get("html_url"),
                          notes=(data.get("body") or "")[:600],
                          update_available=_parse(latest) > _parse(__version__))
    except requests.RequestException:
        pass                                    # offline or GitHub is down: stay quiet
    _cache.update(at=now, data=result)
    return result
