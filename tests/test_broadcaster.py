"""Full YouTube-mode run with the YouTube API faked out and RTMP swapped for an .mp4.

Exercises the Broadcaster graph end to end: live check, broadcast + stream creation,
metadata, thumbnail, encoder start, waiting for live, chat reading, teardown.
Skipped when FFmpeg or Playwright's browser aren't available.
"""
import asyncio
import shutil
import threading
import time

import pytest

from quizcaster import broadcaster, config, db, questions as qs, youtube
from quizcaster.events import EventBus

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


class FakeYouTube:
    def __init__(self):
        self.calls = []
        self.t0 = None
        self.chat_polls = 0

    def install(self, mp, out_path):
        mp.setattr(youtube, "signed_in", lambda: True)
        mp.setattr(youtube, "check_live_enabled", lambda: (self._c("check"), True)[1:] + ("ok",))
        mp.setattr(youtube, "create_broadcast", lambda t, d, p: (self._c("broadcast", p),
                   {"id": "vid123", "snippet": {"liveChatId": "chat1"}})[1])
        mp.setattr(youtube, "create_stream", lambda t: (self._c("stream"),
                   {"id": "st1", "cdn": {"ingestionInfo": {"ingestionAddress": "rtmp://x/live2", "streamName": "k"}}})[1])
        mp.setattr(youtube, "bind", lambda b, s: self._c("bind"))
        mp.setattr(youtube, "set_video_metadata", lambda *a: (self._c("metadata", a[-1]), [])[1])
        mp.setattr(youtube, "set_thumbnail", lambda v, png: (self._c("thumbnail", len(png)), None)[1])
        mp.setattr(youtube, "stream_status", lambda s: "active")
        mp.setattr(youtube, "broadcast_status", lambda b: {"status": {"lifeCycleStatus": "live"},
                                                           "snippet": {"liveChatId": "chat1"}})
        mp.setattr(youtube, "transition", lambda b, s: self._c("transition", s))
        mp.setattr(youtube, "list_chat", self.list_chat)
        # RTMP -> local file
        orig = broadcaster.Streamer.__init__

        def init(this, url, target, *a, **k):
            orig(this, url, str(out_path), *a, **k)
        mp.setattr(broadcaster.Streamer, "__init__", init)

    def _c(self, *a):
        self.calls.append(a)

    def list_chat(self, chat_id, token):
        self.chat_polls += 1
        db.add_quota("liveChatMessages.list", 5)
        now = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1e6):06d}Z"
        items = []
        if self.chat_polls % 3 == 0:
            items.append({"snippet": {"type": "textMessageEvent", "displayMessage": "b", "publishedAt": iso},
                          "authorDetails": {"channelId": "UCviewer", "displayName": "Real Viewer"}})
        return {"items": items, "nextPageToken": f"p{self.chat_polls}", "pollingIntervalMillis": 100}


def test_youtube_mode_end_to_end(monkeypatch, tmp_path):
    pytest.importorskip("playwright")
    # serve the real overlay so the renderer has something to draw
    import uvicorn
    from quizcaster import server
    port = 8799
    monkeypatch.setattr(config, "PORT", port)
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)

    monkeypatch.setattr(config, "MIN_CHAT_POLL_MS", 100)
    fake = FakeYouTube()
    out = tmp_path / "live.mp4"
    fake.install(monkeypatch, out)
    q = qs.normalize({"question": "Pick B?", "options": ["a", "b", "c", "d"], "answer": "B", "explanation": "e"})
    sid = qs.create_set("S", "Testing", "uploaded", [q, q])
    qs.set_approval(sid, True)

    bus = EventBus()
    req = broadcaster.GoLiveRequest(mode="test", set_id=sid, theme="neon", privacy="public", timing_scale=0.1)
    sess = broadcaster.Session(bus, req, f"http://127.0.0.1:{port}/overlay")

    async def main():
        try:
            sess.start()
            await sess.task
        except Exception:
            pass

    try:
        asyncio.run(main())
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("playwright browser not installed")
        raise
    finally:
        srv.should_exit = True

    names = [c[0] for c in fake.calls]
    assert names[:5] == ["check", "broadcast", "stream", "bind", "metadata"]
    assert ("broadcast", "unlisted") in fake.calls          # test runs are forced unlisted
    assert ("metadata", True) in fake.calls                 # AI-content disclosure set
    assert ("transition", "complete") in fake.calls         # broadcast ended cleanly
    assert fake.chat_polls > 0
    row = db.query("SELECT * FROM streams")[0]
    assert row["end_reason"] == "out_of_questions" and row["questions_played"] == 2
    assert "Real Viewer" in row["leaderboard"]
    assert out.exists() and out.stat().st_size > 10_000
    assert (config.THUMB_DIR / "vid123.png").exists()
