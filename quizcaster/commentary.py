"""Host one-liners. One LLM call per stream builds the whole pool; a built-in pool
is the fallback, so the show never stalls on the LLM."""
from __future__ import annotations

import random

from .llm import LLMConfig, LLMError, acomplete, extract_json

KINDS = ("winner", "nobody", "intro", "why", "streak", "outro")

FALLBACK: dict[str, list[str]] = {
    "winner": [
        "{name} was FAST! Who's beating that next round?",
        "Lightning fingers, {name}! The rest of you, wake up!",
        "{name} takes it! Think you can beat {name}? Prove it!",
        "First and correct — {name}, you're on fire!",
        "{name} said 'too easy'. Next one's yours if you're quicker!",
        "Boom! {name} grabs the point. Keep those fingers ready!",
        "{name} strikes first! Can anyone catch up?",
        "Speed AND brains — well played, {name}!",
        "{name} owns this one. Who's stealing the next?",
        "That's how it's done, {name}! Your move, everyone else.",
    ],
    "nobody": [
        "Nobody got it! This one's going in the hall of tough ones.",
        "Silence in chat… this one stumped everybody!",
        "Zero correct — don't worry, the next one's yours.",
        "Tricky one! Shake it off, next question incoming.",
        "No winner this round. The point goes back in the jar!",
    ],
    "intro": [
        "Welcome to the live quiz! Type A, B, C or D — fastest correct answer wins!",
        "Quiz time! First correct answer in chat takes the point. Ready?",
        "Get your fingers ready — first right answer wins each round!",
    ],
    "why": [
        "Great question, {name}! Here's why…",
        "{name} wants the story — here it is!",
        "Curious mind, {name}! The reason:",
    ],
    "streak": [
        "{name} is on a streak! Somebody stop them!",
        "{name} again?! Is anyone else playing?",
        "Hat-trick watch: {name} is unstoppable!",
    ],
    "outro": [
        "That's a wrap! Massive congrats to {name}, today's quiz champion!",
        "Show's over — {name} takes the crown! See you next time!",
    ],
}

SYSTEM = """You write short host lines for a live YouTube quiz. Tone: friendly and competitive,
never mean, family-safe. Each line under 80 characters. Use {name} where a viewer's name goes.
Reply ONLY with JSON: {"winner":[...],"nobody":[...],"intro":[...],"why":[...],"streak":[...],"outro":[...]}"""


class CommentaryPool:
    def __init__(self, lines: dict[str, list[str]] | None = None, rng: random.Random | None = None):
        self.lines = {k: list(FALLBACK[k]) for k in KINDS}
        if lines:
            for k in KINDS:
                good = [s.strip() for s in lines.get(k, []) if isinstance(s, str) and 0 < len(s.strip()) <= 120]
                if k in ("winner", "why", "streak", "outro"):
                    good = [s for s in good if "{name}" in s]
                if len(good) >= 2:
                    self.lines[k] = good
        self.rng = rng or random.Random()
        self._last: dict[str, str] = {}

    def pick(self, kind: str, name: str = "") -> str:
        options = self.lines[kind]
        choices = [s for s in options if s != self._last.get(kind)] or options
        line = self.rng.choice(choices)
        self._last[kind] = line
        return line.replace("{name}", name)

    @classmethod
    async def build(cls, cfg: LLMConfig | None, topic: str) -> "CommentaryPool":
        if cfg is None:
            return cls()
        try:
            data = extract_json(await acomplete(
                cfg, SYSTEM,
                f"Quiz topic: {topic}. Write 25 winner lines, 8 nobody, 4 intro, 6 why, 6 streak, 3 outro.",
                max_tokens=2500))
            return cls(data if isinstance(data, dict) else None)
        except LLMError:
            return cls()
