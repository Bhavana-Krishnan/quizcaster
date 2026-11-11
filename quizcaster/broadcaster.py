"""Broadcaster agent + session orchestration.

The Broadcaster owns everything YouTube-facing and the video pipeline:

  setup -> create_broadcast -> metadata -> thumbnail -> start_encoder -> wait_live
        -> (run Quizmaster, with a health watchdog) -> teardown -> END

It runs on the same state graph as the Quizmaster. The two agents share one
EventBus: the Quizmaster decides what the overlay shows, the Broadcaster decides
whether the stream is healthy and can force a technical break.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from . import config, db, questions as qs, youtube
from .chat import SimulatedChat, YouTubeChat
from .commentary import CommentaryPool
from .events import EventBus
from .graph import END, StateGraph
from .llm import current_config
from .quizmaster import Quizmaster, QuizState, Timing
from .streamer import Streamer
from .thumbnail import make_thumbnail


@dataclass
class GoLiveRequest:
    mode: str                      # 'preview' | 'test' | 'live'
    set_id: int
    theme: str = "dark"
    topic: str = ""
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    privacy: str = "public"        # public | unlisted | private  (test mode forces unlisted)
    max_questions: int = 0         # 0 = all approved
    bots: bool = True              # preview only: simulated viewers
    record: bool = False           # preview only: save an .mp4 locally
    timing_scale: float = 1.0


def default_metadata(topic: str, channel: str = "") -> dict:
    return {
        "title": f"LIVE {topic} Quiz | Answer in chat, fastest correct wins! #shorts",
        "description": (f"Live {topic} quiz! Type A, B, C or D in the chat. The first correct answer wins "
                        f"each question and climbs the leaderboard.\n\nHosted by an AI quizmaster."
                        + (f"\n\n{channel}" if channel else "")),
        "tags": ["quiz", "live quiz", "trivia", topic.lower(), "shorts", "quiz time"],
    }


def estimate_question_cost(timing: Timing) -> int:
    """Worst-case YouTube quota units for one question (window + why window polling)."""
    polls = (timing.window + timing.why_window) / (config.MIN_CHAT_POLL_MS / 1000) + 2
    return int(polls * config.QUOTA_COST["liveChatMessages.list"])


def estimate_capacity(timing: Timing | None = None) -> dict:
    timing = timing or Timing()
    setup = sum(config.QUOTA_COST[k] for k in ("liveBroadcasts.insert", "liveStreams.insert", "liveBroadcasts.bind",
                                               "videos.update", "thumbnails.set", "liveBroadcasts.transition")) + 60
    per_q = estimate_question_cost(timing)
    remaining = db.quota_remaining()
    usable = max(0, remaining - setup - config.QUOTA_SAFETY_MARGIN)
    return {"remaining": remaining, "limit": db.quota_limit(), "per_question": per_q,
            "setup": setup, "questions_possible": usable // per_q if per_q else 0}


@dataclass
class BState:
    req: GoLiveRequest
    broadcast_id: str | None = None
    stream_id: str | None = None
    ingest: str | None = None
    live_chat_id: str | None = None
    video_url: str | None = None
    warnings: list[str] = field(default_factory=list)
    end_reason: str = ""
    stream_row: int | None = None


class Session:
    """One running show. Created by the server for each Go Live / Preview."""

    def __init__(self, bus: EventBus, req: GoLiveRequest, overlay_url: str):
        self.bus = bus
        self.req = req
        self.overlay_url = overlay_url
        self.state = BState(req)
        self.streamer: Streamer | None = None
        self.quizmaster: Quizmaster | None = None
        self.sim_chat: SimulatedChat | None = None
        self.task: asyncio.Task | None = None
        self.stop_requested = False
        self.stop_now = False
        self.graph = StateGraph("broadcaster")
        for n in ("setup", "create_broadcast", "metadata", "start_encoder", "wait_live", "run_show", "teardown"):
            self.graph.add_node(n, getattr(self, f"n_{n}"))
        self.graph.set_entry("setup")
        self.graph.on_transition = lambda a, b: self.bus.set_status(phase=b)

    # ------------------------------------------------------------ public control
    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self.graph.run(self.state)
        except Exception as e:  # last line of defence: never leave a broadcast dangling
            self.bus.info(f"Stopped because of an error: {e}", "error")
            self.state.end_reason = "error"
            await self._teardown_quietly()
        finally:
            self.bus.set_status(state="idle", phase=None, ended_reason=self.state.end_reason or "completed")

    def request_stop(self, now: bool = False) -> None:
        self.stop_requested = True
        self.stop_now = self.stop_now or now
        if self.quizmaster:
            self.quizmaster.stop_requested = True
            self.quizmaster.stop_now = self.quizmaster.stop_now or now
            if not self.state.end_reason and not self.quizmaster.s.end_reason:
                self.quizmaster.s.end_reason = "stopped"

    @property
    def is_youtube(self) -> bool:
        return self.req.mode in ("test", "live")

    # ------------------------------------------------------------ nodes
    async def n_setup(self, s: BState) -> str:
        r = s.req
        self.bus.set_status(state="starting", mode=r.mode, started_at=time.time(), tech_break=False,
                            question=0, total=0, video_url=None, warnings=[])
        self.bus.comments.clear()
        approved = qs.get_questions(r.set_id, approved_only=True)
        if not approved:
            raise RuntimeError("This question set has no approved questions.")
        if r.mode == "test":
            r.privacy = "unlisted"
        meta = default_metadata(r.topic or "Trivia", (db.get_setting("channel") or {}).get("title", ""))
        r.title = r.title.strip() or meta["title"]
        r.description = r.description.strip() or meta["description"]
        r.tags = r.tags or meta["tags"]
        cur = db.execute("INSERT INTO streams(mode,set_id,title,theme,started_at) VALUES(?,?,?,?,?)",
                         (r.mode, r.set_id, r.title, r.theme, db.now_iso()))
        s.stream_row = cur.lastrowid
        self.bus.overlay(scene="idle", theme=r.theme, topic=r.topic, mode="preview" if r.mode == "preview" else "live",
                         leaderboard=[], qnum=0)
        if not self.is_youtube:
            return "start_encoder"
        if not youtube.signed_in():
            raise RuntimeError("Sign in to YouTube first (Setup → YouTube).")
        ok, msg = await asyncio.to_thread(youtube.check_live_enabled)
        if not ok:
            raise RuntimeError(msg)
        cap = estimate_capacity(Timing().scaled(r.timing_scale))
        if cap["questions_possible"] < 1:
            raise RuntimeError("Not enough YouTube API quota left today to run a quiz. It resets at midnight Pacific time.")
        return "create_broadcast"

    async def n_create_broadcast(self, s: BState) -> str:
        r = s.req
        self.bus.info("Creating YouTube broadcast…")
        b = await asyncio.to_thread(youtube.create_broadcast, r.title, r.description, r.privacy)
        s.broadcast_id = b["id"]
        s.live_chat_id = b.get("snippet", {}).get("liveChatId")
        st = await asyncio.to_thread(youtube.create_stream, r.title)
        s.stream_id = st["id"]
        ing = st["cdn"]["ingestionInfo"]
        s.ingest = f"{ing['ingestionAddress'].rstrip('/')}/{ing['streamName']}"
        await asyncio.to_thread(youtube.bind, s.broadcast_id, s.stream_id)
        s.video_url = f"https://www.youtube.com/watch?v={s.broadcast_id}"
        db.execute("UPDATE streams SET broadcast_id=? WHERE id=?", (s.broadcast_id, s.stream_row))
        self.bus.set_status(video_url=s.video_url)
        return "metadata"

    async def n_metadata(self, s: BState) -> str:
        r = s.req
        s.warnings += await asyncio.to_thread(youtube.set_video_metadata, s.broadcast_id, r.title, r.description,
                                              r.tags, r.privacy, True)
        channel = (db.get_setting("channel") or {}).get("title", "")
        png = await asyncio.to_thread(make_thumbnail, r.topic or "Trivia", r.theme, channel)
        (config.THUMB_DIR / f"{s.broadcast_id}.png").write_bytes(png)
        warn = await asyncio.to_thread(youtube.set_thumbnail, s.broadcast_id, png)
        if warn:
            s.warnings.append(warn)
        for w in s.warnings:
            self.bus.info(w, "warn")
        self.bus.set_status(warnings=s.warnings)
        return "start_encoder"

    async def n_start_encoder(self, s: BState) -> str:
        r = s.req
        if self.is_youtube:
            target = s.ingest
        elif r.record:
            out = config.DATA_DIR / "recordings"
            out.mkdir(exist_ok=True)
            target = str(out / f"preview-{time.strftime('%Y%m%d-%H%M%S')}.mp4")
        else:
            return "run_show"         # preview without recording: the dashboard shows the overlay itself
        self.bus.info("Starting the video encoder…")
        self.streamer = Streamer(self.overlay_url, target)
        await self.streamer.start()
        if not self.is_youtube:
            self.bus.info(f"Recording to {target}")
            self.bus.set_status(recording=target)
        return "wait_live" if self.is_youtube else "run_show"

    async def n_wait_live(self, s: BState) -> str:
        """Auto-start turns the broadcast live once YouTube sees our video. Fall back to a manual transition."""
        self.bus.info("Waiting for YouTube to receive the video…")
        deadline = time.time() + 120
        while time.time() < deadline and not self.stop_now:
            if self.streamer and not self.streamer.alive:
                raise RuntimeError(f"Video encoder stopped: {self.streamer.last_error()}")
            status = await asyncio.to_thread(youtube.stream_status, s.stream_id)
            if status == "active":
                break
            await asyncio.sleep(4)
        else:
            if not self.stop_now:
                raise RuntimeError("YouTube never received the video. Check your internet upload speed and firewall (RTMP port 1935).")
        deadline = time.time() + 90
        while time.time() < deadline and not self.stop_now:
            b = await asyncio.to_thread(youtube.broadcast_status, s.broadcast_id)
            life = b.get("status", {}).get("lifeCycleStatus")
            s.live_chat_id = b.get("snippet", {}).get("liveChatId") or s.live_chat_id
            if life == "live":
                break
            if life in ("ready", "testing") and time.time() > deadline - 60:
                try:
                    await asyncio.to_thread(youtube.transition, s.broadcast_id, "live")
                except youtube.YouTubeError as e:
                    self.bus.info(f"Waiting to go live ({e.reason or e})", "warn")
            await asyncio.sleep(5)
        if not s.live_chat_id:
            raise RuntimeError("The broadcast has no live chat. Make sure live chat is enabled for your channel.")
        self.bus.info("You're live!")
        return "run_show"

    async def n_run_show(self, s: BState) -> str:
        r = s.req
        self.bus.set_status(state="live" if self.is_youtube else "preview")
        pool = await CommentaryPool.build(current_config(), r.topic or "general knowledge")
        timing = Timing().scaled(r.timing_scale)
        served: set[int] = set()

        def next_question():
            if r.max_questions and len(served) >= r.max_questions:
                return None
            for q in qs.get_questions(r.set_id, approved_only=True):   # re-read: questions approved mid-stream join in
                if q.id not in served:
                    served.add(q.id)
                    return q
            return None

        def total_hint():
            n = len(qs.get_questions(r.set_id, approved_only=True))
            return min(n, r.max_questions) if r.max_questions else n

        per_q = estimate_question_cost(timing)

        def quota_ok() -> bool:
            return (not self.is_youtube) or db.quota_remaining() > per_q + config.QUOTA_SAFETY_MARGIN + 60

        if self.is_youtube:
            chat = YouTubeChat(s.live_chat_id)
        else:
            chat = self.sim_chat = SimulatedChat(bots=r.bots)
        self.quizmaster = Quizmaster(self.bus, QuizState(topic=r.topic or "Trivia", next_question=next_question,
                                                         total_hint=total_hint, chat=chat, pool=pool,
                                                         timing=timing, quota_ok=quota_ok))
        if self.stop_requested:
            self.request_stop(self.stop_now)
        watchdog = asyncio.create_task(self._watchdog()) if self.streamer else None
        try:
            s.end_reason = await self.quizmaster.run()
        finally:
            if watchdog:
                watchdog.cancel()
        db.execute("UPDATE streams SET questions_played=?, leaderboard=? WHERE id=?",
                   (self.quizmaster.s.qnum, json.dumps(self.quizmaster.leaderboard(10)), s.stream_row))
        return "teardown"

    async def _watchdog(self) -> None:
        """Restart the encoder if it dies; show a technical break meanwhile."""
        failures = 0
        while True:
            await asyncio.sleep(2)
            st = self.streamer
            if st is None or st.alive or self.stop_now:
                continue
            failures += 1
            err = st.last_error()
            self.bus.info(f"Video encoder stopped ({err}). Restarting… [{failures}/5]", "warn")
            if self.quizmaster:
                self.quizmaster.pause("Back in a moment!")
            if failures > 5:
                self.bus.info("The encoder keeps failing — ending the stream.", "error")
                self.state.end_reason = "encoder_failed"
                self.request_stop(now=True)
                return
            await asyncio.sleep(min(30, 3 * failures))
            try:
                await st.restart_encoder()
                await asyncio.sleep(3)
                if st.alive and self.quizmaster:
                    self.quizmaster.resume()
            except Exception as e:
                self.bus.info(f"Restart failed: {e}", "warn")

    async def n_teardown(self, s: BState) -> str:
        await self._teardown_quietly()
        return END

    async def _teardown_quietly(self) -> None:
        s = self.state
        if self.streamer:
            try:
                await asyncio.sleep(2)        # let the last frames of the final scene go out
                await self.streamer.stop()
            except Exception as e:
                self.bus.info(f"Encoder shutdown: {e}", "warn")
            self.streamer = None
        if s.broadcast_id:
            try:
                b = await asyncio.to_thread(youtube.broadcast_status, s.broadcast_id)
                if b.get("status", {}).get("lifeCycleStatus") in ("live", "testing", "liveStarting"):
                    await asyncio.to_thread(youtube.transition, s.broadcast_id, "complete")
            except youtube.YouTubeError as e:
                self.bus.info(f"Couldn't end the broadcast cleanly ({e}). YouTube will end it automatically.", "warn")
        if s.stream_row:
            db.execute("UPDATE streams SET ended_at=?, end_reason=? WHERE id=?",
                       (db.now_iso(), s.end_reason or "completed", s.stream_row))
        self.bus.info(f"Stream finished ({(s.end_reason or 'completed').replace('_', ' ')}).")
