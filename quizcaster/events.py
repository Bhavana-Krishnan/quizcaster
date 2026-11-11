"""In-process pub/sub fanned out to Server-Sent Events clients (dashboard, overlay, remote).

Two channels:
  overlay  — what the stream shows (the overlay page renders only from this)
  control  — status, comments, logs for the dashboard and mobile remote
The latest overlay state is kept so a page that (re)connects renders immediately.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque


class EventBus:
    def __init__(self):
        self._subs: dict[str, set[asyncio.Queue]] = {"overlay": set(), "control": set()}
        self.overlay_state: dict = {"scene": "idle"}
        self.status: dict = {"state": "idle"}
        self.log: deque = deque(maxlen=200)
        self.comments: deque = deque(maxlen=200)

    def subscribe(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs[channel].add(q)
        return q

    def unsubscribe(self, channel: str, q: asyncio.Queue) -> None:
        self._subs[channel].discard(q)

    def _publish(self, channel: str, event: str, data: dict) -> None:
        payload = json.dumps(data, default=str)
        for q in list(self._subs[channel]):
            try:
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                pass

    # -- overlay
    def overlay(self, **state) -> None:
        """Replace the scene. Persistent bits (theme, leaderboard, topic) are merged in."""
        keep = {k: self.overlay_state[k] for k in ("theme", "leaderboard", "topic", "qnum", "qtotal", "mode")
                if k in self.overlay_state and k not in state}
        self.overlay_state = {**keep, **state, "t": time.time()}
        self._publish("overlay", "scene", self.overlay_state)

    def overlay_patch(self, **patch) -> None:
        self.overlay_state.update(patch, t=time.time())
        self._publish("overlay", "scene", self.overlay_state)

    # -- control
    def set_status(self, **status) -> None:
        self.status.update(status)
        self._publish("control", "status", self.status)

    def info(self, message: str, level: str = "info") -> None:
        entry = {"ts": time.time(), "level": level, "message": message}
        self.log.append(entry)
        self._publish("control", "log", entry)

    def comment(self, entry: dict) -> None:
        self.comments.append(entry)
        self._publish("control", "comment", entry)

    def control(self, event: str, data: dict) -> None:
        self._publish("control", event, data)


bus = EventBus()
