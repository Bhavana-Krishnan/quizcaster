"""Quizmaster agent: runs the show.

    intro -> question -> options -> reveal -> explain -> leaderboard -> (question | final) -> END

No LLM calls happen in here: questions and the commentary pool are prepared before
going live, which keeps the stream cheap and immune to LLM outages.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import config
from .chat import ChatMessage, ChatSource, SimulatedChat
from .commentary import CommentaryPool
from .events import EventBus
from .graph import END, StateGraph
from .moderation import safe_comment, safe_display_name
from .questions import LETTERS, Question
from .youtube import QuotaExhausted, YouTubeError

_ANSWER_RE = re.compile(r"^\s*(?:option\s*)?([abcd])\s*[!.?)]*\s*$", re.I)
_WHY_RE = re.compile(r"^\s*why\b[\s?!.]*$|^\s*but\s+why\b[\s?!.]*$|^\s*why\s+[abcd]\b[\s?!.]*$", re.I)


def parse_answer(text: str) -> str | None:
    """'B', 'b', 'B!', 'b.' -> 'B'. Anything longer is not an answer."""
    m = _ANSWER_RE.match(text or "")
    return m.group(1).upper() if m else None


def is_why(text: str) -> bool:
    return bool(_WHY_RE.match(text or ""))


@dataclass
class Timing:
    intro: float = config.INTRO_SECONDS
    question: float = config.QUESTION_READ_SECONDS
    window: float = config.ANSWER_WINDOW_SECONDS
    reveal: float = config.REVEAL_SECONDS
    explain: float = config.EXPLAIN_SECONDS
    why_window: float = config.WHY_WINDOW_SECONDS
    why_extra: float = config.WHY_EXTRA_SECONDS
    leaderboard: float = config.LEADERBOARD_SECONDS
    final: float = config.FINAL_SECONDS

    def scaled(self, f: float) -> "Timing":
        return Timing(**{k: v * f for k, v in self.__dict__.items()})


@dataclass
class Player:
    name: str
    points: int = 0


@dataclass
class QuizState:
    topic: str
    next_question: Callable[[], Question | None]
    total_hint: Callable[[], int]
    chat: ChatSource
    pool: CommentaryPool
    timing: Timing = field(default_factory=Timing)
    quota_ok: Callable[[], bool] = lambda: True
    # runtime
    q: Question | None = None
    qnum: int = 0
    players: dict[str, Player] = field(default_factory=dict)
    last_winner: str | None = None
    streak: int = 0
    winner: ChatMessage | None = None
    answer_counts: dict[str, int] = field(default_factory=dict)
    end_reason: str = ""
    history: list[dict] = field(default_factory=list)


class Quizmaster:
    def __init__(self, bus: EventBus, state: QuizState):
        self.bus = bus
        self.s = state
        self.stop_requested = False          # graceful: finish current question, then final
        self.stop_now = False                # immediate
        self._running = asyncio.Event()
        self._running.set()
        self._paused_total = 0.0
        self.graph = StateGraph("quizmaster")
        for name in ("intro", "question", "options", "reveal", "explain", "leaderboard", "final"):
            self.graph.add_node(name, getattr(self, f"n_{name}"))
        self.graph.set_entry("intro")
        self.graph.on_transition = lambda a, b: self.bus.set_status(scene=b)

    # ------------------------------------------------------------ control
    def pause(self, reason: str) -> None:
        """Technical break: freeze timers and show the break screen."""
        if not self._running.is_set():
            return
        self._saved_scene = dict(self.bus.overlay_state)
        self._paused_at = time.time()
        self._running.clear()
        self.bus.overlay(scene="break", message=reason)
        self.bus.set_status(tech_break=True)
        self.bus.info(f"Technical break: {reason}", "warn")

    def resume(self) -> None:
        if self._running.is_set():
            return
        paused = time.time() - self._paused_at
        self._paused_total += paused
        scene = self._saved_scene
        if "ends_at" in scene:                       # keep the timer honest
            scene["ends_at"] = scene["ends_at"] + paused
        self.bus.overlay(**{k: v for k, v in scene.items() if k != "t"})
        self._running.set()
        self.bus.set_status(tech_break=False)
        self.bus.info("Back from technical break.")

    async def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        seen = self._paused_total
        while not self.stop_now:
            await self._running.wait()
            end += self._paused_total - seen
            seen = self._paused_total
            left = end - time.time()
            if left <= 0:
                return
            await asyncio.sleep(min(left, 0.25))

    async def _poll(self) -> list[ChatMessage]:
        """Poll chat; on API trouble take a technical break and retry with backoff."""
        delay = config.TECH_BREAK_RETRY_SECONDS
        attempt = 0
        while not self.stop_now:
            try:
                msgs = await self.s.chat.poll()
                if attempt:
                    self.resume()
                return msgs
            except QuotaExhausted:
                self.s.end_reason = "quota"
                self.stop_requested = True
                if attempt:
                    self.resume()
                return []
            except (YouTubeError, OSError) as e:
                attempt += 1
                if attempt == 1:
                    self.pause("We'll be right back!")
                self.bus.info(f"Chat read failed ({e}); retrying in {delay}s", "warn")
                if attempt >= 8:
                    self.s.end_reason = "chat_unavailable"
                    self.stop_requested = True
                    self.resume()
                    return []
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        return []

    def leaderboard(self, n: int = 5) -> list[dict]:
        ranked = sorted(self.s.players.values(), key=lambda p: -p.points)[:n]
        return [{"name": p.name, "points": p.points} for p in ranked]

    def _show_comment(self, m: ChatMessage, tag: str = "") -> None:
        text = safe_comment(m.text)
        self.bus.comment({"ts": m.ts, "name": safe_display_name(m.name), "text": text if text else "(hidden)",
                          "tag": tag, "q": self.s.qnum})

    async def run(self) -> str:
        await self.graph.run(self.s)
        return self.s.end_reason or "completed"

    # ------------------------------------------------------------ nodes
    async def n_intro(self, s: QuizState) -> str:
        self.bus.overlay(scene="intro", topic=s.topic, line=s.pool.pick("intro"),
                         leaderboard=[], qnum=0, qtotal=s.total_hint())
        await self._sleep(s.timing.intro)
        return "question"

    async def n_question(self, s: QuizState) -> str:
        if self.stop_now:
            return END
        if self.stop_requested:
            return "final"
        if not s.quota_ok():
            s.end_reason = "quota"
            self.bus.info("YouTube quota is nearly used up for today — wrapping up.", "warn")
            return "final"
        q = s.next_question()
        if q is None:
            s.end_reason = s.end_reason or "out_of_questions"
            return "final"
        s.q, s.qnum = q, s.qnum + 1
        s.winner, s.answer_counts = None, {l: 0 for l in LETTERS}
        self.bus.set_status(question=s.qnum, total=s.total_hint())
        self.bus.overlay(scene="question", question=q.question, qnum=s.qnum, qtotal=s.total_hint())
        await self._sleep(s.timing.question)
        return "options"

    async def n_options(self, s: QuizState) -> str:
        q = s.q
        assert q is not None
        await s.chat.skip_backlog()
        if isinstance(s.chat, SimulatedChat):
            s.chat.arm_bots(q.answer)
        opened = time.time()
        deadline = opened + s.timing.window
        self.bus.overlay(scene="options", question=q.question, options=q.options,
                         opened_at=opened, ends_at=deadline, window=s.timing.window, answers=0)
        answered: set[str] = set()
        seen = self._paused_total
        while not self.stop_now and s.winner is None:
            await self._running.wait()
            deadline += self._paused_total - seen
            seen = self._paused_total
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(s.chat.wait_hint(), remaining))
            msgs = await self._poll()
            for m in sorted(msgs, key=lambda m: m.ts):
                if m.ts < opened - 1.0:          # posted before options appeared
                    continue
                letter = parse_answer(m.text)
                self._show_comment(m, "answer" if letter else "")
                if letter is None or m.author_id in answered:
                    continue
                answered.add(m.author_id)        # first answer only, right or wrong
                s.answer_counts[letter] += 1
                if letter == q.answer and s.winner is None:
                    s.winner = m
            self.bus.overlay_patch(answers=len(answered))
            if self.stop_requested and s.end_reason == "quota":
                break
        return "reveal"

    async def n_reveal(self, s: QuizState) -> str:
        q = s.q
        assert q is not None
        if s.winner:
            m = s.winner
            p = s.players.setdefault(m.author_id, Player(safe_display_name(m.name)))
            p.name = safe_display_name(m.name)
            p.points += 1
            s.streak = s.streak + 1 if s.last_winner == m.author_id else 1
            s.last_winner = m.author_id
            kind = "streak" if s.streak >= 2 else "winner"
            line, winner_name = s.pool.pick(kind, p.name), p.name
        else:
            s.last_winner, s.streak = None, 0
            line, winner_name = s.pool.pick("nobody"), None
        s.history.append({"q": s.qnum, "question": q.question, "answer": q.answer,
                          "winner": winner_name, "answers": sum(s.answer_counts.values())})
        self.bus.overlay(scene="reveal", question=q.question, options=q.options, answer=q.answer,
                         winner=winner_name, line=line, leaderboard=self.leaderboard(),
                         counts=s.answer_counts)
        self.bus.control("winner", {"q": s.qnum, "winner": winner_name, "leaderboard": self.leaderboard()})
        self._why_opened = time.time()
        await self._sleep(s.timing.reveal)
        return "explain"

    async def n_explain(self, s: QuizState) -> str:
        q = s.q
        assert q is not None
        self.bus.overlay(scene="explain", question=q.question, answer=q.answer,
                         answer_text=q.correct_text(), explanation=q.explanation, asker=None)
        if isinstance(s.chat, SimulatedChat):
            s.chat.arm_why()
        show_until = time.time() + s.timing.explain
        why_until = self._why_opened + s.timing.why_window
        asker = None
        seen = self._paused_total
        while not self.stop_now:
            await self._running.wait()
            show_until += self._paused_total - seen
            why_until += self._paused_total - seen
            seen = self._paused_total
            now = time.time()
            if now >= show_until and (asker or now >= why_until):
                break
            if asker is None and now < why_until:
                await asyncio.sleep(min(s.chat.wait_hint(), max(0.1, why_until - now)))
                for m in await self._poll():
                    if m.ts < self._why_opened - 1.0:
                        continue
                    if is_why(m.text):
                        self._show_comment(m, "why")
                        asker = safe_display_name(m.name)
                        self.bus.overlay_patch(asker=asker, why_line=s.pool.pick("why", asker))
                        show_until = max(show_until, time.time() + s.timing.why_extra)
                        break
            else:
                await asyncio.sleep(min(0.25, max(0.0, show_until - now)))
        return "leaderboard"

    async def n_leaderboard(self, s: QuizState) -> str:
        if self.leaderboard():
            self.bus.overlay(scene="leaderboard", leaderboard=self.leaderboard())
            await self._sleep(s.timing.leaderboard)
        return "question"

    async def n_final(self, s: QuizState) -> str:
        board = self.leaderboard()
        champ = board[0]["name"] if board else None
        line = s.pool.pick("outro", champ) if champ else "Thanks for playing! See you at the next quiz!"
        self.bus.overlay(scene="final", leaderboard=board, champion=champ, line=line, reason=s.end_reason)
        await self._sleep(s.timing.final)
        return END
