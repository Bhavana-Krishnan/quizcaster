"""Local web server: dashboard, overlay, mobile remote, JSON API and SSE streams."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
import socket
from dataclasses import fields

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                                 Response, StreamingResponse)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import config, db, llm, questions as qs, updates, youtube
from .broadcaster import GoLiveRequest, Session, default_metadata, estimate_capacity
from .events import bus
from .quizmaster import Timing
from .streamer import ffmpeg_path
from .thumbnail import THEMES, make_thumbnail

SESSION: Session | None = None
GEN_JOBS: dict[str, dict] = {}
REMOTE_TOKENS: set[str] = set()


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def ok(**data) -> JSONResponse:
    return JSONResponse({"ok": True, **data})


def api(fn):
    async def wrapper(request: Request):
        try:
            return await fn(request)
        except ApiError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=e.status)
        except (llm.LLMError, youtube.YouTubeError, qs.QuestionFormatError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    wrapper.__name__ = fn.__name__
    return wrapper


# ------------------------------------------------------------------ security: local vs remote
def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "testclient")


REMOTE_PUBLIC = ("/remote", "/static/", "/overlay", "/events/overlay")
REMOTE_AUTHED = ("/events/control", "/api/remote/")


class LocalOnly(BaseHTTPMiddleware):
    """Everything is local-only, except the mobile remote (PIN-protected, limited actions)."""

    async def dispatch(self, request: Request, call_next):
        if _is_local(request):
            return await call_next(request)
        path = request.url.path
        if not db.get_setting("remote_enabled", False):
            return PlainTextResponse("Remote access is turned off.", status_code=403)
        if path.startswith(REMOTE_PUBLIC):
            return await call_next(request)
        if path.startswith(REMOTE_AUTHED) and request.cookies.get("qc_remote") in REMOTE_TOKENS:
            return await call_next(request)
        return PlainTextResponse("Not allowed from the mobile remote.", status_code=403)


# ------------------------------------------------------------------ pages
def page(name: str):
    async def handler(request: Request):
        return FileResponse(config.STATIC_DIR / name, headers={"Cache-Control": "no-store"})
    return handler


async def oauth_callback(request: Request):
    if request.query_params.get("error"):
        return RedirectResponse("/?yt=denied#setup")
    try:
        await asyncio.to_thread(youtube.finish_auth, request.query_params.get("code", ""),
                                request.query_params.get("state", ""))
        await asyncio.to_thread(youtube.my_channel)
    except youtube.YouTubeError as e:
        return HTMLResponse(f"<p>{e}</p><p><a href='/'>Back to QuizCaster</a></p>", status_code=400)
    return RedirectResponse("/?yt=ok#setup")


# ------------------------------------------------------------------ SSE
async def sse(request: Request):
    channel = request.path_params["channel"]
    if channel not in ("overlay", "control"):
        return PlainTextResponse("unknown channel", status_code=404)
    q = bus.subscribe(channel)

    async def gen():
        try:
            if channel == "overlay":
                yield f"event: scene\ndata: {json.dumps(bus.overlay_state, default=str)}\n\n"
            else:
                yield f"event: status\ndata: {json.dumps(bus.status, default=str)}\n\n"
                for c in list(bus.comments)[-60:]:
                    yield f"event: comment\ndata: {json.dumps(c, default=str)}\n\n"
                for entry in list(bus.log)[-40:]:
                    yield f"event: log\ndata: {json.dumps(entry, default=str)}\n\n"
            while True:
                try:
                    event, payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {event}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(channel, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ state
def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))     # no packets are sent
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _session_active() -> bool:
    return SESSION is not None and SESSION.task is not None and not SESSION.task.done()


@api
async def get_state(request: Request):
    cfg_provider = db.get_setting("llm_provider")
    try:
        cap = estimate_capacity()
    except Exception:
        cap = None
    return ok(
        llm={"provider": cfg_provider, "model": db.get_setting("llm_model"), "has_key": db.has_secret("llm_api_key")},
        youtube={"client_configured": youtube.client_configured(), "client_id": db.get_setting("google_client_id"),
                 "signed_in": youtube.signed_in(), "channel": db.get_setting("channel"),
                 "live_enabled": db.get_setting("live_enabled"), "redirect_uri": youtube.redirect_uri()},
        quota=cap,
        sets=qs.list_sets(),
        themes=list(THEMES),
        session={"active": _session_active(), **bus.status},
        remote={"enabled": db.get_setting("remote_enabled", False), "pin": db.get_setting("remote_pin"),
                "url": f"http://{lan_ip()}:{config.PORT}/remote", "bound_lan": config.HOST != "127.0.0.1"},
        checks={"ffmpeg": bool(ffmpeg_path())},
        update=await asyncio.to_thread(updates.check),
    )


# ------------------------------------------------------------------ LLM
@api
async def llm_verify(request: Request):
    d = await body(request)
    provider, key = d.get("provider", ""), (d.get("api_key") or "").strip()
    if not key:
        if provider == db.get_setting("llm_provider") and db.has_secret("llm_api_key"):
            key = db.get_secret("llm_api_key")
        else:
            raise ApiError("Paste your API key.")
    models = await asyncio.to_thread(llm.list_models, provider, key)
    if not models:
        raise ApiError("The key works, but no chat models are available on it.")
    llm.save_llm_settings(provider, key, None)
    if db.get_setting("llm_model") not in {m["id"] for m in models}:
        db.set_setting("llm_model", None)
    return ok(models=models)


@api
async def llm_model(request: Request):
    d = await body(request)
    if not d.get("model"):
        raise ApiError("Pick a model.")
    db.set_setting("llm_model", d["model"])
    return ok()


@api
async def llm_forget(request: Request):
    db.set_secret("llm_api_key", None)
    db.set_setting("llm_model", None)
    return ok()


# ------------------------------------------------------------------ YouTube
@api
async def yt_client(request: Request):
    d = await body(request)
    cid, secret = (d.get("client_id") or "").strip(), (d.get("client_secret") or "").strip()
    if not cid.endswith(".apps.googleusercontent.com"):
        raise ApiError("That doesn't look like a Google OAuth client ID (it ends in .apps.googleusercontent.com).")
    if not secret:
        raise ApiError("Paste the client secret too.")
    youtube.save_client(cid, secret)
    return ok()


@api
async def yt_auth(request: Request):
    return ok(url=youtube.auth_url())


@api
async def yt_check(request: Request):
    ch = await asyncio.to_thread(youtube.my_channel)
    enabled, msg = await asyncio.to_thread(youtube.check_live_enabled)
    db.set_setting("live_enabled", enabled)
    return ok(channel=ch, live_enabled=enabled, message=msg)


@api
async def yt_signout(request: Request):
    await asyncio.to_thread(youtube.sign_out)
    db.set_setting("live_enabled", None)
    return ok()


@api
async def yt_quota(request: Request):
    d = await body(request)
    limit = int(d.get("daily_limit") or 0)
    if limit < 1000:
        raise ApiError("Daily quota must be at least 1,000 units.")
    db.set_setting("youtube_daily_quota", limit)
    return ok(quota=estimate_capacity())


# ------------------------------------------------------------------ questions
@api
async def sets_list(request: Request):
    return ok(sets=qs.list_sets())


@api
async def set_get(request: Request):
    sid = int(request.path_params["sid"])
    rows = db.query("SELECT * FROM question_sets WHERE id=?", (sid,))
    if not rows:
        raise ApiError("Question set not found.", 404)
    return ok(set=rows[0], questions=[q.to_dict() for q in qs.get_questions(sid)])


async def _gen_job(job_id: str, cfg, topic: str, count: int, difficulty: str, set_id: int | None, name: str):
    job = GEN_JOBS[job_id]

    async def progress(done, total):
        job.update(done=done, total=total)
        bus.control("gen", job)

    try:
        generated, warnings = await qs.generate(cfg, topic, count, difficulty, progress)
        if set_id:
            qs.add_questions(set_id, generated)
        else:
            set_id = qs.create_set(name, topic, "generated", generated)
        job.update(state="done", set_id=set_id, done=len(generated), warnings=warnings)
    except llm.LLMError as e:
        job.update(state="error", error=str(e))
    except Exception as e:  # keep the job record honest
        job.update(state="error", error=f"Unexpected error: {e}")
    bus.control("gen", job)


@api
async def sets_generate(request: Request):
    d = await body(request)
    cfg = llm.current_config()
    if cfg is None:
        raise ApiError("Add your Claude or OpenAI key and pick a model first (Setup).")
    topic = (d.get("topic") or "").strip()
    set_id = int(d["set_id"]) if d.get("set_id") else None
    if set_id and not topic:
        rows = db.query("SELECT topic FROM question_sets WHERE id=?", (set_id,))
        topic = rows[0]["topic"] if rows else ""
    if not topic:
        raise ApiError("Enter a quiz topic.")
    count = max(1, min(200, int(d.get("count") or 20)))
    job_id = secrets.token_hex(6)
    GEN_JOBS[job_id] = {"id": job_id, "state": "running", "done": 0, "total": count, "topic": topic}
    asyncio.create_task(_gen_job(job_id, cfg, topic, count, d.get("difficulty") or "mixed", set_id,
                                 (d.get("name") or topic).strip()[:80]))
    return ok(job=GEN_JOBS[job_id])


@api
async def gen_status(request: Request):
    job = GEN_JOBS.get(request.path_params["jid"])
    if not job:
        raise ApiError("Unknown job", 404)
    return ok(job=job)


@api
async def sets_upload(request: Request):
    form = await request.form()
    f = form.get("file")
    if f is None or not getattr(f, "filename", ""):
        raise ApiError("Choose a file to upload.")
    content = await f.read()
    if len(content) > 5_000_000:
        raise ApiError("File is too large (5 MB max).")
    good, errors = qs.parse_upload(f.filename, content)
    if not good:
        raise ApiError("No valid questions found. " + " ".join(errors[:5]))
    name = (form.get("name") or f.filename.rsplit(".", 1)[0])[:80]
    topic = (form.get("topic") or name)[:80]
    sid = qs.create_set(name, topic, "uploaded", good)
    return ok(set_id=sid, imported=len(good), errors=errors[:50])


@api
async def set_approve(request: Request):
    d = await body(request)
    qs.set_approval(int(request.path_params["sid"]), bool(d.get("approved", True)), d.get("ids"))
    return ok()


@api
async def set_delete(request: Request):
    qs.delete_set(int(request.path_params["sid"]))
    return ok()


@api
async def question_update(request: Request):
    q = qs.update_question(int(request.path_params["qid"]), await body(request))
    return ok(question=q.to_dict())


@api
async def question_delete(request: Request):
    qs.delete_question(int(request.path_params["qid"]))
    return ok()


async def template_csv(request: Request):
    return Response(qs.TEMPLATE_CSV, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=quiz-template.csv"})


# ------------------------------------------------------------------ go live
@api
async def metadata_defaults(request: Request):
    topic = request.query_params.get("topic") or "Trivia"
    return ok(**default_metadata(topic, (db.get_setting("channel") or {}).get("title", "")))


async def thumbnail_preview(request: Request):
    png = await asyncio.to_thread(make_thumbnail, request.query_params.get("topic") or "Trivia",
                                  request.query_params.get("theme") or "dark",
                                  (db.get_setting("channel") or {}).get("title", ""))
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@api
async def capacity(request: Request):
    return ok(quota=estimate_capacity())


def _build_request(d: dict) -> GoLiveRequest:
    allowed = {f.name for f in fields(GoLiveRequest)}
    data = {k: v for k, v in d.items() if k in allowed}
    if data.get("mode") not in ("preview", "test", "live"):
        raise ApiError("Unknown mode.")
    if not data.get("set_id"):
        raise ApiError("Pick a question set.")
    data["set_id"] = int(data["set_id"])
    if data.get("theme") not in THEMES:
        data["theme"] = "dark"
    if data.get("privacy") not in ("public", "unlisted", "private"):
        data["privacy"] = "public"
    if isinstance(data.get("tags"), str):
        data["tags"] = [t.strip() for t in data["tags"].split(",") if t.strip()]
    data["max_questions"] = max(0, int(data.get("max_questions") or 0))
    data["timing_scale"] = min(3.0, max(0.05, float(data.get("timing_scale") or 1.0)))
    if not data.get("topic"):
        rows = db.query("SELECT topic, name FROM question_sets WHERE id=?", (data["set_id"],))
        data["topic"] = (rows[0]["topic"] or rows[0]["name"]) if rows else "Trivia"
    return GoLiveRequest(**data)


async def _start(d: dict) -> Session:
    global SESSION
    if _session_active():
        raise ApiError("A stream is already running. Stop it first.")
    req = _build_request(d)
    if req.mode != "preview" and not ffmpeg_path():
        raise ApiError("FFmpeg isn't installed, so the video can't be sent to YouTube. See the README.")
    if req.mode != "preview" and not youtube.signed_in():
        raise ApiError("Sign in to YouTube first (Setup → YouTube).")
    if not qs.get_questions(req.set_id, approved_only=True):
        raise ApiError("This question set has no approved questions yet.")
    SESSION = Session(bus, req, f"http://127.0.0.1:{config.PORT}/overlay")
    SESSION.start()
    return SESSION


@api
async def golive(request: Request):
    s = await _start(await body(request))
    return ok(mode=s.req.mode)


@api
async def stop(request: Request):
    d = await body(request)
    if not _session_active():
        raise ApiError("Nothing is running.")
    SESSION.request_stop(now=bool(d.get("now")))
    bus.info("Stopping now…" if d.get("now") else "Finishing this question, then showing the final leaderboard…")
    return ok()


@api
async def preview_comment(request: Request):
    d = await body(request)
    if not (_session_active() and SESSION.sim_chat):
        raise ApiError("Fake comments only work during a preview.")
    name, text = (d.get("name") or "You").strip()[:30], (d.get("text") or "").strip()[:200]
    if not text:
        raise ApiError("Type a comment.")
    SESSION.sim_chat.post(name, text)
    return ok()


@api
async def update_setting(request: Request):
    d = await body(request)
    db.set_setting("check_updates", bool(d.get("enabled", True)))
    return ok(update=await asyncio.to_thread(updates.check, True))


@api
async def history(request: Request):
    rows = db.query("SELECT * FROM streams ORDER BY id DESC LIMIT 30")
    for r in rows:
        r["leaderboard"] = json.loads(r["leaderboard"]) if r.get("leaderboard") else []
    return ok(streams=rows)


# ------------------------------------------------------------------ mobile remote
@api
async def remote_settings(request: Request):
    d = await body(request)
    enabled = bool(d.get("enabled"))
    db.set_setting("remote_enabled", enabled)
    if enabled and (d.get("new_pin") or not db.get_setting("remote_pin")):
        db.set_setting("remote_pin", f"{secrets.randbelow(1_000_000):06d}")
        REMOTE_TOKENS.clear()
    if not enabled:
        REMOTE_TOKENS.clear()
    return ok(enabled=enabled, pin=db.get_setting("remote_pin"), url=f"http://{lan_ip()}:{config.PORT}/remote",
              needs_restart=enabled and config.HOST == "127.0.0.1")


_failed_logins = {"count": 0}


async def remote_login(request: Request):
    d = await body(request)
    if _failed_logins["count"] >= 10:
        return JSONResponse({"ok": False, "error": "Too many wrong PINs. Make a new PIN on the desktop."}, 429)
    pin = db.get_setting("remote_pin")
    if pin and secrets.compare_digest(str(d.get("pin", "")), pin):
        token = secrets.token_urlsafe(24)
        REMOTE_TOKENS.add(token)
        _failed_logins["count"] = 0
        resp = JSONResponse({"ok": True})
        resp.set_cookie("qc_remote", token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30)
        return resp
    _failed_logins["count"] += 1
    return JSONResponse({"ok": False, "error": "Wrong PIN."}, 401)


@api
async def remote_state(request: Request):
    sets = [s for s in qs.list_sets() if s["approved"]]
    return ok(session={"active": _session_active(), **bus.status}, sets=sets, themes=list(THEMES),
              leaderboard=bus.overlay_state.get("leaderboard", []), signed_in=youtube.signed_in())


@api
async def remote_golive(request: Request):
    d = await body(request)
    d = {k: d.get(k) for k in ("mode", "set_id", "theme", "privacy", "max_questions")}
    s = await _start(d)
    return ok(mode=s.req.mode)


# ------------------------------------------------------------------ app
routes = [
    Route("/", page("dashboard.html")),
    Route("/overlay", page("overlay.html")),
    Route("/remote", page("remote.html")),
    Route("/oauth/callback", oauth_callback),
    Route("/events/{channel}", sse),
    Route("/api/state", get_state),
    Route("/api/llm/verify", llm_verify, methods=["POST"]),
    Route("/api/llm/model", llm_model, methods=["POST"]),
    Route("/api/llm/forget", llm_forget, methods=["POST"]),
    Route("/api/youtube/client", yt_client, methods=["POST"]),
    Route("/api/youtube/auth", yt_auth),
    Route("/api/youtube/check", yt_check, methods=["POST"]),
    Route("/api/youtube/signout", yt_signout, methods=["POST"]),
    Route("/api/youtube/quota", yt_quota, methods=["POST"]),
    Route("/api/sets", sets_list),
    Route("/api/sets/generate", sets_generate, methods=["POST"]),
    Route("/api/sets/upload", sets_upload, methods=["POST"]),
    Route("/api/jobs/{jid}", gen_status),
    Route("/api/sets/{sid:int}", set_get),
    Route("/api/sets/{sid:int}", set_delete, methods=["DELETE"]),
    Route("/api/sets/{sid:int}/approve", set_approve, methods=["POST"]),
    Route("/api/questions/{qid:int}", question_update, methods=["PUT"]),
    Route("/api/questions/{qid:int}", question_delete, methods=["DELETE"]),
    Route("/api/template.csv", template_csv),
    Route("/api/metadata", metadata_defaults),
    Route("/api/thumbnail", thumbnail_preview),
    Route("/api/capacity", capacity),
    Route("/api/golive", golive, methods=["POST"]),
    Route("/api/stop", stop, methods=["POST"]),
    Route("/api/preview/comment", preview_comment, methods=["POST"]),
    Route("/api/history", history),
    Route("/api/updates", update_setting, methods=["POST"]),
    Route("/api/remote-settings", remote_settings, methods=["POST"]),
    Route("/remote/login", remote_login, methods=["POST"]),
    Route("/api/remote/state", remote_state),
    Route("/api/remote/golive", remote_golive, methods=["POST"]),
    Route("/api/remote/stop", stop, methods=["POST"]),
    Mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static"),
]

app = Starlette(routes=routes, middleware=[Middleware(LocalOnly)])
