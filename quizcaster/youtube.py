"""YouTube access using the creator's OWN Google Cloud OAuth client (bring your own
client ID + secret). Plain REST + OAuth 2.0 with PKCE; every call is charged to the
local quota ledger so the app knows how long it can keep streaming today.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

from . import config, db

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
SCOPES = ["https://www.googleapis.com/auth/youtube"]


class YouTubeError(Exception):
    def __init__(self, message: str, reason: str = "", status: int = 0):
        super().__init__(message)
        self.reason = reason
        self.status = status


class QuotaExhausted(YouTubeError):
    pass


def redirect_uri() -> str:
    return f"http://127.0.0.1:{config.PORT}/oauth/callback"


# ------------------------------------------------------------------ OAuth
_pending: dict[str, str] = {}   # state -> code_verifier


def save_client(client_id: str, client_secret: str) -> None:
    db.set_setting("google_client_id", client_id.strip())
    db.set_secret("google_client_secret", client_secret.strip())


def client_configured() -> bool:
    return bool(db.get_setting("google_client_id")) and db.has_secret("google_client_secret")


def auth_url() -> str:
    if not client_configured():
        raise YouTubeError("Add your Google client ID and secret first.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    _pending[state] = verifier
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": db.get_setting("google_client_id"),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def finish_auth(code: str, state: str) -> None:
    verifier = _pending.pop(state, None)
    if verifier is None:
        raise YouTubeError("Sign-in expired or was started elsewhere. Please try again.")
    r = requests.post(TOKEN_URL, timeout=20, data={
        "code": code, "client_id": db.get_setting("google_client_id"),
        "client_secret": db.get_secret("google_client_secret"),
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code", "code_verifier": verifier})
    if r.status_code != 200:
        raise YouTubeError(f"Google sign-in failed: {r.json().get('error_description') or r.text[:200]}")
    tok = r.json()
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    db.set_secret("google_token", tok)


def signed_in() -> bool:
    return bool(db.get_secret("google_token"))


def sign_out() -> None:
    tok = db.get_secret("google_token")
    if tok:
        try:
            requests.post(REVOKE_URL, params={"token": tok.get("refresh_token") or tok["access_token"]}, timeout=10)
        except requests.RequestException:
            pass
    db.set_secret("google_token", None)
    db.set_setting("channel", None)


def _access_token() -> str:
    tok = db.get_secret("google_token")
    if not tok:
        raise YouTubeError("Not signed in to YouTube.", "notSignedIn")
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise YouTubeError("YouTube sign-in expired. Please sign in again.", "notSignedIn")
    r = requests.post(TOKEN_URL, timeout=20, data={
        "client_id": db.get_setting("google_client_id"), "client_secret": db.get_secret("google_client_secret"),
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise YouTubeError("YouTube sign-in expired. Please sign in again.", "notSignedIn")
    new = r.json()
    tok.update(access_token=new["access_token"], expires_at=time.time() + int(new.get("expires_in", 3600)) - 60)
    db.set_secret("google_token", tok)
    return tok["access_token"]


# ------------------------------------------------------------------ API calls
def _call(method_name: str, http: str, path: str, *, params=None, json=None, data=None,
          headers=None, upload=False) -> dict:
    cost = config.QUOTA_COST.get(method_name, 1)
    if db.quota_remaining() < cost:
        raise QuotaExhausted("Today's YouTube API quota is used up. It resets at midnight Pacific time.",
                             "quotaExceeded")
    url = (UPLOAD_API if upload else API) + path
    h = {"Authorization": f"Bearer {_access_token()}"}
    h.update(headers or {})
    try:
        r = requests.request(http, url, params=params, json=json, data=data, headers=h, timeout=30)
    except requests.RequestException as e:
        raise YouTubeError(f"Network problem talking to YouTube ({e.__class__.__name__}).", "network") from e
    db.add_quota(method_name, cost)
    if r.status_code in (200, 201, 204):
        return r.json() if r.content else {}
    try:
        err = r.json().get("error", {})
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        msg = err.get("message", r.text[:200])
    except ValueError:
        reason, msg = "", r.text[:200]
    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        raise QuotaExhausted(f"YouTube quota/rate limit: {msg}", reason, r.status_code)
    raise YouTubeError(f"YouTube {method_name} failed: {msg}", reason, r.status_code)


def my_channel() -> dict:
    data = _call("channels.list", "GET", "/channels", params={"part": "snippet", "mine": "true"})
    items = data.get("items") or []
    if not items:
        raise YouTubeError("This Google account has no YouTube channel yet. Create one on youtube.com first.",
                           "noChannel")
    ch = {"id": items[0]["id"], "title": items[0]["snippet"]["title"],
          "thumbnail": items[0]["snippet"].get("thumbnails", {}).get("default", {}).get("url")}
    db.set_setting("channel", ch)
    return ch


LIVE_NOT_ENABLED_HELP = (
    "Live streaming isn't enabled on this channel yet. Go to studio.youtube.com → Create → Go live, "
    "verify your phone number when asked, then wait — YouTube can take up to 24 hours to enable it. "
    "Come back and press 'Check again'.")


def check_live_enabled() -> tuple[bool, str]:
    try:
        _call("liveBroadcasts.list", "GET", "/liveBroadcasts",
              params={"part": "id", "mine": "true", "maxResults": 1, "broadcastType": "all"})
        return True, "Live streaming is enabled."
    except YouTubeError as e:
        if e.reason in ("liveStreamingNotEnabled", "insufficientLivePermissions", "livePermissionBlocked"):
            return False, LIVE_NOT_ENABLED_HELP
        raise


def create_broadcast(title: str, description: str, privacy: str) -> dict:
    start = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _call("liveBroadcasts.insert", "POST", "/liveBroadcasts",
                 params={"part": "snippet,status,contentDetails"},
                 json={"snippet": {"title": title[:100], "description": description[:5000],
                                   "scheduledStartTime": start},
                       "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
                       "contentDetails": {"enableAutoStart": True, "enableAutoStop": True,
                                          "latencyPreference": "low", "enableDvr": True,
                                          "monitorStream": {"enableMonitorStream": False}}})


def create_stream(title: str) -> dict:
    return _call("liveStreams.insert", "POST", "/liveStreams", params={"part": "snippet,cdn,contentDetails"},
                 json={"snippet": {"title": title[:100]},
                       "cdn": {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"},
                       "contentDetails": {"isReusable": False}})


def bind(broadcast_id: str, stream_id: str) -> dict:
    return _call("liveBroadcasts.bind", "POST", "/liveBroadcasts/bind",
                 params={"id": broadcast_id, "streamId": stream_id, "part": "id,contentDetails"})


def broadcast_status(broadcast_id: str) -> dict:
    data = _call("liveBroadcasts.list", "GET", "/liveBroadcasts",
                 params={"part": "status,snippet", "id": broadcast_id})
    items = data.get("items") or []
    return items[0] if items else {}


def stream_status(stream_id: str) -> str:
    data = _call("liveStreams.list", "GET", "/liveStreams", params={"part": "status", "id": stream_id})
    items = data.get("items") or []
    return items[0]["status"]["streamStatus"] if items else "unknown"


def transition(broadcast_id: str, status: str) -> dict:
    return _call("liveBroadcasts.transition", "POST", "/liveBroadcasts/transition",
                 params={"broadcastStatus": status, "id": broadcast_id, "part": "status"})


def set_video_metadata(video_id: str, title: str, description: str, tags: list[str],
                       privacy: str, synthetic: bool = True) -> list[str]:
    """Tags, category and the AI-content disclosure live on the video resource.
    Best effort: returns warnings instead of failing the stream."""
    warnings = []
    try:
        _call("videos.update", "PUT", "/videos", params={"part": "snippet,status"}, json={
            "id": video_id,
            "snippet": {"title": title[:100], "description": description[:5000],
                        "tags": tags[:30], "categoryId": "24"},          # 24 = Entertainment
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": synthetic}})
    except YouTubeError as e:
        warnings.append(f"Couldn't set tags / AI disclosure ({e}). You can set them in YouTube Studio.")
    return warnings


def set_thumbnail(video_id: str, png_bytes: bytes) -> str | None:
    try:
        _call("thumbnails.set", "POST", "/thumbnails/set", params={"videoId": video_id},
              data=png_bytes, headers={"Content-Type": "image/png"}, upload=True)
        return None
    except YouTubeError as e:
        if e.status == 403:
            return ("Custom thumbnails need a verified channel (youtube.com/verify). "
                    "The stream continues with YouTube's automatic thumbnail.")
        return f"Thumbnail upload failed: {e}"


def list_chat(live_chat_id: str, page_token: str | None) -> dict:
    params = {"liveChatId": live_chat_id, "part": "snippet,authorDetails", "maxResults": 2000}
    if page_token:
        params["pageToken"] = page_token
    return _call("liveChatMessages.list", "GET", "/liveChat/messages", params=params)


def parse_ts(ts: str) -> float:
    """RFC 3339 -> epoch seconds; tolerates 'Z' and >6 fractional digits."""
    import re
    ts = ts.replace("Z", "+00:00")
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)
    return datetime.fromisoformat(ts).timestamp()
