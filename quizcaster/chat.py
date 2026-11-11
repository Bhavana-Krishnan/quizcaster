"""Chat sources. The Quizmaster only ever asks a source for "messages since X" and
only while a window is open, so the YouTube source never polls between questions."""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

from . import config, youtube


@dataclass
class ChatMessage:
    author_id: str
    name: str
    text: str
    ts: float          # epoch seconds, from YouTube's publishedAt (or local clock in preview)


class ChatSource:
    async def poll(self) -> list[ChatMessage]:
        """Return new messages since the previous poll."""
        raise NotImplementedError

    def wait_hint(self) -> float:
        """Seconds to wait before the next poll."""
        return 1.0

    async def skip_backlog(self) -> None:
        """Called when a window opens: drop anything queued before now."""


class YouTubeChat(ChatSource):
    def __init__(self, live_chat_id: str):
        self.live_chat_id = live_chat_id
        self.page_token: str | None = None
        self.interval = config.MIN_CHAT_POLL_MS / 1000

    async def poll(self) -> list[ChatMessage]:
        data = await asyncio.to_thread(youtube.list_chat, self.live_chat_id, self.page_token)
        self.page_token = data.get("nextPageToken") or self.page_token
        self.interval = max(config.MIN_CHAT_POLL_MS, int(data.get("pollingIntervalMillis", 0))) / 1000
        out = []
        for item in data.get("items", []):
            sn, au = item.get("snippet", {}), item.get("authorDetails", {})
            if sn.get("type") not in ("textMessageEvent", None):
                continue
            text = sn.get("displayMessage") or (sn.get("textMessageDetails") or {}).get("messageText", "")
            try:
                ts = youtube.parse_ts(sn["publishedAt"])
            except (KeyError, ValueError):
                ts = time.time()
            out.append(ChatMessage(au.get("channelId", ""), au.get("displayName", "viewer"), text, ts))
        return out

    def wait_hint(self) -> float:
        return self.interval


class SimulatedChat(ChatSource):
    """Preview mode. The dashboard can post comments as fake viewers, and optional
    bots answer automatically so the flow can be watched without anyone typing."""

    BOT_NAMES = ["PriyaQuizzes", "Arjun_K", "TriviaTom", "MeeraM", "QuizWhiz99", "Sam R", "neha.reads"]

    def __init__(self, bots: bool = True, rng: random.Random | None = None):
        self.queue: list[ChatMessage] = []
        self.bots = bots
        self.rng = rng or random.Random()
        self._current_answer: str | None = None
        self._opened_at = 0.0
        self._bot_plan: list[tuple[float, ChatMessage]] = []

    def post(self, name: str, text: str) -> None:
        self.queue.append(ChatMessage(f"sim-{name.lower()}", name, text, time.time()))

    def arm_bots(self, correct_letter: str | None) -> None:
        """Schedule bot answers for the window that just opened."""
        self._bot_plan = []
        if not self.bots or correct_letter is None:
            return
        now = time.time()
        for name in self.rng.sample(self.BOT_NAMES, k=self.rng.randint(2, 5)):
            right = self.rng.random() < 0.55
            letter = correct_letter if right else self.rng.choice([l for l in "ABCD" if l != correct_letter])
            text = self.rng.choice([letter, letter.lower(), f"{letter}!", f"{letter.lower()} ?"]) \
                if self.rng.random() < 0.85 else self.rng.choice(["hmm", "this is hard", "why?", "hi from Pune!"])
            self._bot_plan.append((now + self.rng.uniform(2, 14), ChatMessage(f"bot-{name}", name, text, 0)))

    def arm_why(self) -> None:
        self._bot_plan = []
        if self.bots and self.rng.random() < 0.35:
            name = self.rng.choice(self.BOT_NAMES)
            self._bot_plan.append((time.time() + self.rng.uniform(1, 5), ChatMessage(f"bot-{name}", name, "why?", 0)))

    async def skip_backlog(self) -> None:
        self.queue.clear()

    async def poll(self) -> list[ChatMessage]:
        now = time.time()
        due = [m for t, m in self._bot_plan if t <= now]
        self._bot_plan = [(t, m) for t, m in self._bot_plan if t > now]
        for m in due:
            m.ts = now
        out, self.queue = self.queue + due, []
        return sorted(out, key=lambda m: m.ts)

    def wait_hint(self) -> float:
        return 0.5
