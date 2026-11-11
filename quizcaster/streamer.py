"""Renderer: headless Chromium draws /overlay, frames are piped to FFmpeg, FFmpeg
pushes 9:16 H.264 + silent AAC to YouTube over RTMP (or to an .mp4 for previews).

Frames come from Chrome's screencast (pushed only when pixels change); a pacer
re-sends the latest frame at a fixed rate so the encoder always gets steady input.
"""
from __future__ import annotations

import asyncio
import base64
import shutil
import time
from collections import deque

from . import config


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def build_ffmpeg_cmd(target: str, width: int, height: int) -> list[str]:
    is_rtmp = target.startswith("rtmp")
    cmd = [
        ffmpeg_path() or "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "image2pipe", "-framerate", str(config.CAPTURE_FPS), "-c:v", "mjpeg", "-i", "pipe:0",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-vf", (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"),
        "-r", str(config.OUTPUT_FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-b:v", config.VIDEO_BITRATE, "-maxrate", config.VIDEO_BITRATE,
        "-bufsize", str(int(config.VIDEO_BITRATE.rstrip("k")) * 2) + "k",
        "-g", str(config.OUTPUT_FPS * 2), "-keyint_min", str(config.OUTPUT_FPS * 2), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-shortest",
    ]
    if is_rtmp:
        cmd += ["-f", "flv", target]
    else:
        cmd += ["-movflags", "+faststart", target]
    return cmd


class Streamer:
    def __init__(self, overlay_url: str, target: str, width: int = config.STREAM_WIDTH,
                 height: int = config.STREAM_HEIGHT):
        self.overlay_url = overlay_url
        self.target = target
        self.width, self.height = width, height
        self.frame: bytes | None = None
        self.frames_sent = 0
        self.stderr_tail: deque[str] = deque(maxlen=20)
        self._proc: asyncio.subprocess.Process | None = None
        self._pw = None
        self._browser = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self.started_at = 0.0

    # ------------------------------------------------------------ browser
    async def _start_browser(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        last_error: Exception | None = None
        # Playwright's bundled Chromium first, then whatever Chrome or Edge is installed.
        for channel in (None, "chrome", "msedge"):
            try:
                self._browser = await self._pw.chromium.launch(**({"channel": channel} if channel else {}))
                break
            except Exception as e:
                last_error = e
        if self._browser is None:
            raise RuntimeError("No browser found for the video renderer. Install Google Chrome or Microsoft Edge "
                               f"(or run 'python -m playwright install chromium'). Details: {last_error}")
        scale = self.width / 720
        ctx = await self._browser.new_context(viewport={"width": 720, "height": 1280}, device_scale_factor=scale)
        self.page = await ctx.new_page()
        await self.page.goto(self.overlay_url + ("&" if "?" in self.overlay_url else "?") + "render=1")
        self.cdp = await ctx.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 85,
                                                     "maxWidth": self.width, "maxHeight": self.height})

    def _on_frame(self, params: dict) -> None:
        self.frame = base64.b64decode(params["data"])
        if self._browser is None:
            return
        fut = asyncio.ensure_future(self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())   # swallow errors after shutdown

    async def _screenshot_fallback(self) -> None:
        """If the screencast stays silent (some headless builds), take screenshots instead."""
        await asyncio.sleep(3)
        if self.frame is not None:
            return
        while not self._stopping:
            try:
                self.frame = await self.page.screenshot(type="jpeg", quality=85)
            except Exception:
                pass
            await asyncio.sleep(1 / config.CAPTURE_FPS)

    # ------------------------------------------------------------ encoder
    async def _start_ffmpeg(self) -> None:
        if not ffmpeg_path():
            raise RuntimeError("FFmpeg is not installed. See README → Install.")
        self._proc = await asyncio.create_subprocess_exec(
            *build_ffmpeg_cmd(self.target, self.width, self.height),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)

    async def _pace(self) -> None:
        interval = 1 / config.CAPTURE_FPS
        nxt = time.monotonic()
        while not self._stopping and self._proc and self._proc.returncode is None:
            if self.frame is not None:
                try:
                    self._proc.stdin.write(self.frame)
                    await self._proc.stdin.drain()
                    self.frames_sent += 1
                except (BrokenPipeError, ConnectionResetError):
                    break
            nxt += interval
            await asyncio.sleep(max(0, nxt - time.monotonic()))

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            self.stderr_tail.append(line.decode(errors="replace").rstrip())

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._stopping = False
        if self._browser is None:
            await self._start_browser()
        await self._start_ffmpeg()
        self.started_at = time.time()
        self._tasks = [asyncio.create_task(self._pace()), asyncio.create_task(self._read_stderr()),
                       asyncio.create_task(self._screenshot_fallback())]

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def last_error(self) -> str:
        return " | ".join(list(self.stderr_tail)[-3:]) or "encoder stopped"

    async def restart_encoder(self) -> None:
        """Restart only FFmpeg (the browser and overlay keep running)."""
        await self._stop_ffmpeg()
        self._stopping = False
        await self._start_ffmpeg()
        self._tasks = [asyncio.create_task(self._pace()), asyncio.create_task(self._read_stderr())]

    async def _stop_ffmpeg(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.stdin.close()
                await asyncio.wait_for(self._proc.wait(), timeout=8)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                self._proc.kill()
                await self._proc.wait()

    async def stop(self) -> None:
        await self._stop_ffmpeg()
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._browser = self._pw = None
