"""Drive the Quizmaster through whole shows with scripted chat and compressed time."""
import asyncio
import time

from quizcaster import youtube
from quizcaster.chat import ChatMessage, ChatSource
from quizcaster.commentary import CommentaryPool
from quizcaster.events import EventBus
from quizcaster.questions import normalize
from quizcaster.quizmaster import Quizmaster, QuizState, Timing

FAST = Timing(intro=0.05, question=0.05, window=1.2, reveal=0.05, explain=0.1, why_window=0.4,
              why_extra=0.1, leaderboard=0.05, final=0.05)


def make_q(i, answer="B"):
    return normalize({"question": f"Q{i}?", "options": ["w", "x", "y", "z"], "answer": answer, "explanation": f"E{i}"})


class Scripted(ChatSource):
    """script: list of (seconds_after_window_opens, author, name, text); `before` is posted before options."""

    def __init__(self, per_question):
        self.per_question = per_question
        self.qi = -1
        self.opened = 0.0
        self.sent = set()
        self.fail_polls = 0
        self.polls = 0

    async def skip_backlog(self):
        self.qi += 1
        self.opened = time.time()
        self.sent = set()

    async def poll(self):
        self.polls += 1
        if self.fail_polls:
            self.fail_polls -= 1
            raise youtube.YouTubeError("boom", "backendError")
        script = self.per_question[self.qi] if self.qi < len(self.per_question) else []
        now = time.time()
        out = []
        for idx, (dt, author, name, text) in enumerate(script):
            if idx not in self.sent and self.opened + dt <= now:
                self.sent.add(idx)
                out.append(ChatMessage(author, name, text, self.opened + dt))
        return out

    def wait_hint(self):
        return 0.05


def run_show(questions, chat, **kw):
    bus = EventBus()
    scenes = []
    orig = bus.overlay

    def spy(**state):
        scenes.append(dict(state))
        orig(**state)
    bus.overlay = spy
    it = iter(questions)
    state = QuizState(topic="Test", next_question=lambda: next(it, None), total_hint=lambda: len(questions),
                      chat=chat, pool=CommentaryPool(), timing=FAST, **kw)
    qm = Quizmaster(bus, state)
    reason = asyncio.run(qm.run())
    return qm, reason, scenes, bus


def test_first_correct_by_timestamp_and_first_answer_only():
    script = [[
        (0.10, "u1", "Ana", "A"),        # wrong
        (0.12, "u1", "Ana", "B"),        # second answer from Ana: ignored
        (0.15, "u2", "Ben", "hello"),    # not an answer
        (0.20, "u3", "Cy", "b"),         # FIRST correct
        (0.21, "u4", "Di", "B!"),        # correct but later
    ]]
    qm, reason, scenes, bus = run_show([make_q(1)], Scripted(script))
    reveal = next(s for s in scenes if s["scene"] == "reveal")
    assert reveal["winner"] == "Cy"
    assert reveal["counts"]["A"] == 1 and reveal["counts"]["B"] == 1   # window closed at first correct
    assert qm.leaderboard() == [{"name": "Cy", "points": 1}]
    assert reason == "out_of_questions"
    assert [s["scene"] for s in scenes][-1] == "final"


def test_comments_before_options_are_ignored():
    chat = Scripted([[(-3.0, "u1", "Early", "B"), (0.3, "u2", "Late", "B")]])
    qm, *_ = run_show([make_q(1)], chat)
    assert qm.leaderboard()[0]["name"] == "Late"


def test_nobody_right_reveals_with_filler():
    qm, _, scenes, _ = run_show([make_q(1)], Scripted([[(0.1, "u1", "Ana", "C")]]))
    reveal = next(s for s in scenes if s["scene"] == "reveal")
    assert reveal["winner"] is None and reveal["line"]
    assert qm.leaderboard() == []


def test_window_closes_on_timeout_without_answers():
    t0 = time.time()
    run_show([make_q(1)], Scripted([[]]))
    assert time.time() - t0 >= FAST.window


def test_why_callout_and_streak_and_leaderboard_order():
    script = [
        [(0.1, "u1", "Ana", "B")],
        [(0.1, "u1", "Ana", "B"), (0.2, "u2", "Ben", "B")],
        [(0.1, "u2", "Ben", "B")],
    ]
    chat = Scripted(script)
    # "why?" arrives during the first explanation: inject it via the next poll after the reveal

    orig_poll = chat.poll
    asked = {"done": False}

    async def poll_with_why():
        msgs = await orig_poll()
        if not asked["done"] and chat.qi == 0 and chat.polls > 3 and not msgs:
            asked["done"] = True
            return [ChatMessage("u9", "Curious", "why?", time.time())]
        return msgs
    chat.poll = poll_with_why
    qm, _, scenes, bus = run_show([make_q(1), make_q(2), make_q(3)], chat)
    assert any(c["tag"] == "why" and c["name"] == "Curious" for c in bus.comments)
    # Ana wins twice in a row -> streak line uses her name
    reveals = [s for s in scenes if s["scene"] == "reveal"]
    assert [r["winner"] for r in reveals] == ["Ana", "Ana", "Ben"]
    assert "Ana" in reveals[1]["line"]
    assert qm.leaderboard() == [{"name": "Ana", "points": 2}, {"name": "Ben", "points": 1}]
    final = scenes[-1]
    assert final["champion"] == "Ana" and "Ana" in final["line"]


def test_offensive_winner_name_is_masked():
    qm, _, scenes, _ = run_show([make_q(1)], Scripted([[(0.1, "u1", "sh1tlord99", "B")]]))
    assert next(s for s in scenes if s["scene"] == "reveal")["winner"] == "A quiz player"


def test_chat_failure_shows_technical_break_then_recovers(monkeypatch):
    from quizcaster import config
    monkeypatch.setattr(config, "TECH_BREAK_RETRY_SECONDS", 0.05)
    chat = Scripted([[(0.1, "u1", "Ana", "B")]])
    chat.fail_polls = 2
    qm, reason, scenes, bus = run_show([make_q(1)], chat)
    names = [s["scene"] for s in scenes]
    assert "break" in names
    assert names.index("break") < names.index("reveal")
    assert qm.leaderboard()[0]["name"] == "Ana"     # the show carried on after the break


def test_quota_guard_ends_gracefully():
    qm, reason, scenes, _ = run_show([make_q(1), make_q(2)], Scripted([[], []]), quota_ok=lambda: False)
    assert reason == "quota"
    assert [s["scene"] for s in scenes] == ["intro", "final"]


def test_graceful_stop_finishes_current_question():
    chat = Scripted([[(0.1, "u1", "Ana", "B")], [], []])
    bus = EventBus()
    qs_ = iter([make_q(1), make_q(2), make_q(3)])
    state = QuizState(topic="T", next_question=lambda: next(qs_, None), total_hint=lambda: 3, chat=chat,
                      pool=CommentaryPool(), timing=FAST)
    qm = Quizmaster(bus, state)

    async def main():
        task = asyncio.create_task(qm.run())
        while state.qnum < 1:
            await asyncio.sleep(0.01)
        qm.stop_requested = True
        state.end_reason = "stopped"
        return await task
    reason = asyncio.run(main())
    assert reason == "stopped" and state.qnum == 1


def test_pause_freezes_the_answer_window():
    chat = Scripted([[(0.1, "u1", "Ana", "C")]])
    bus = EventBus()
    state = QuizState(topic="T", next_question=iter([make_q(1)]).__next__, total_hint=lambda: 1, chat=chat,
                      pool=CommentaryPool(), timing=FAST)
    state.next_question = (lambda it: (lambda: next(it, None)))(iter([make_q(1)]))
    qm = Quizmaster(bus, state)

    async def main():
        task = asyncio.create_task(qm.run())
        while bus.overlay_state.get("scene") != "options":
            await asyncio.sleep(0.01)
        ends = bus.overlay_state["ends_at"]
        qm.pause("encoder")
        assert bus.overlay_state["scene"] == "break"
        await asyncio.sleep(0.5)
        qm.resume()
        assert bus.overlay_state["scene"] == "options"
        assert bus.overlay_state["ends_at"] >= ends + 0.45
        return await task
    t0 = time.time()
    asyncio.run(main())
    assert time.time() - t0 >= FAST.window + 0.45
