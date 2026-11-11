import json
import random

import pytest

from quizcaster import db, llm, moderation, questions as qs, youtube
from quizcaster.commentary import CommentaryPool
from quizcaster.quizmaster import is_why, parse_answer


# ---------------------------------------------------------------- answers
@pytest.mark.parametrize("text,expected", [
    ("B", "B"), ("b", "B"), ("  c ", "C"), ("D!", "D"), ("a.", "A"), ("option b", "B"), ("B?", "B"),
    ("E", None), ("BC", None), ("I think B", None), ("bb", None), ("", None), ("why?", None), ("1", None),
])
def test_parse_answer(text, expected):
    assert parse_answer(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("why?", True), ("Why", True), ("WHY??", True), ("but why?", True), ("why B", True),
    ("why not", False), ("why is the sky blue", False), ("B", False), ("", False),
])
def test_is_why(text, expected):
    assert is_why(text) is expected


# ---------------------------------------------------------------- questions
def test_normalize_shapes():
    q = qs.normalize({"question": "Q?", "options": ["w", "x", "y", "z"], "answer": "2", "explanation": "e"})
    assert q.answer == "B" and q.correct_text() == "x"
    q = qs.normalize({"Question": "Q?", "option_a": "w", "option_b": "x", "option_c": "y", "option_d": "z",
                      "answer": "y", "context": "because"})
    assert q.answer == "C" and q.explanation == "because"
    q = qs.normalize({"question": "Q?", "a": "w", "b": "x", "c": "y", "d": "z", "answer": "Option D", "why": "e"})
    assert q.answer == "D"


@pytest.mark.parametrize("bad", [
    {"question": "", "options": ["a", "b", "c", "d"], "answer": "A", "explanation": "e"},
    {"question": "Q", "options": ["a", "b", "c"], "answer": "A", "explanation": "e"},
    {"question": "Q", "options": ["a", "a", "c", "d"], "answer": "A", "explanation": "e"},
    {"question": "Q", "options": ["a", "b", "c", "d"], "answer": "E", "explanation": "e"},
    {"question": "Q", "options": ["a", "b", "c", "d"], "answer": "A", "explanation": ""},
])
def test_normalize_rejects(bad):
    with pytest.raises(qs.QuestionFormatError):
        qs.normalize(bad)


def test_shuffle_keeps_correct_answer():
    q = qs.normalize({"question": "Q", "options": ["right", "w1", "w2", "w3"], "answer": "A", "explanation": "e"})
    rng = random.Random(1)
    letters = set()
    for _ in range(40):
        s = qs.shuffle_options(q, rng)
        assert s.correct_text() == "right"
        letters.add(s.answer)
    assert letters == {"A", "B", "C", "D"}


def test_upload_csv_json_xlsx():
    good, errs = qs.parse_upload("t.csv", qs.TEMPLATE_CSV.encode() + b"Bad row,,,,,A,\n")
    assert len(good) == 1 and good[0].answer == "B" and len(errs) == 1
    data = {"questions": [{"question": "Q", "options": ["a", "b", "c", "d"], "answer": "a", "explanation": "e"}]}
    good, errs = qs.parse_upload("t.json", json.dumps(data).encode())
    assert len(good) == 1 and not errs
    import io
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["Question", "Option_A", "Option_B", "Option_C", "Option_D", "Answer", "Explanation"])
    ws.append(["Q?", "a", "b", "c", "d", "C", "why"])
    buf = io.BytesIO(); wb.save(buf)
    good, errs = qs.parse_upload("t.xlsx", buf.getvalue())
    assert len(good) == 1 and good[0].answer == "C"
    with pytest.raises(qs.QuestionFormatError):
        qs.parse_upload("t.txt", b"x")


def test_set_persistence_and_approval():
    q = qs.normalize({"question": "Q", "options": ["a", "b", "c", "d"], "answer": "A", "explanation": "e"})
    sid = qs.create_set("Set", "topic", "uploaded", [q, q])
    assert qs.get_questions(sid, approved_only=True) == []
    first = qs.get_questions(sid)[0]
    qs.set_approval(sid, True, [first.id])
    assert [x.id for x in qs.get_questions(sid, approved_only=True)] == [first.id]
    qs.set_approval(sid, True)
    assert len(qs.get_questions(sid, approved_only=True)) == 2
    updated = qs.update_question(first.id, {"question": "Q2", "options": ["a", "b", "c", "d"], "answer": "D",
                                            "explanation": "e", "approved": False})
    assert updated.answer == "D" and not updated.approved
    assert qs.list_sets()[0]["approved"] == 1


# ---------------------------------------------------------------- db / secrets / quota
def test_secrets_are_encrypted_at_rest():
    db.set_secret("llm_api_key", "sk-very-secret")
    raw = db.query("SELECT value FROM secrets WHERE key='llm_api_key'")[0]["value"]
    assert b"sk-very-secret" not in raw
    assert db.get_secret("llm_api_key") == "sk-very-secret"
    db.set_secret("llm_api_key", None)
    assert not db.has_secret("llm_api_key")


def test_quota_ledger():
    assert db.quota_remaining() == 10_000
    db.add_quota("liveChatMessages.list", 5)
    db.add_quota("liveChatMessages.list", 5)
    assert db.quota_used_today() == 10
    db.set_setting("youtube_daily_quota", 20)
    assert db.quota_remaining() == 10


# ---------------------------------------------------------------- moderation
@pytest.mark.parametrize("name,safe", [
    ("Priya", True), ("Scunthorpe United", True), ("Dick Grayson", False), ("f.u.c.k", False),
    ("Sh1tLord", False), ("visit spam.com now", False), ("", False), ("Assassin Creed fan", True),
])
def test_safe_display_name(name, safe):
    shown = moderation.safe_display_name(name)
    assert (shown != moderation.SAFE_NAME) is safe


def test_long_names_truncated():
    assert len(moderation.safe_display_name("x" * 60)) == moderation.MAX_NAME


# ---------------------------------------------------------------- commentary
def test_commentary_pool_validates_and_avoids_repeats():
    pool = CommentaryPool({"winner": ["no placeholder here", "{name} rocks", "{name} wins"]}, rng=random.Random(0))
    assert all("{name}" in s for s in pool.lines["winner"])
    picks = [pool.pick("winner", "Ana") for _ in range(10)]
    assert all("Ana" in p for p in picks)
    assert all(a != b for a, b in zip(picks, picks[1:]))
    assert pool.lines["nobody"]  # fallback kept


# ---------------------------------------------------------------- llm helpers
def test_extract_json_handles_fences_and_noise():
    assert llm.extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('here: [1,2,3] done') == [1, 2, 3]
    with pytest.raises(llm.LLMError):
        llm.extract_json("no json at all")


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._p


def test_list_models_openai_filters(monkeypatch):
    payload = {"data": [{"id": "gpt-4o", "created": 2}, {"id": "text-embedding-3-small", "created": 3},
                        {"id": "whisper-1", "created": 1}, {"id": "o3-mini", "created": 4}, {"id": "gpt-4o-mini-tts", "created": 5}]}
    monkeypatch.setattr(llm.requests, "get", lambda *a, **k: FakeResp(200, payload))
    assert [m["id"] for m in llm.list_models("openai", "k")] == ["o3-mini", "gpt-4o"]


def test_list_models_anthropic_pagination(monkeypatch):
    pages = [{"data": [{"id": "m1", "display_name": "M1"}], "has_more": True, "last_id": "m1"},
             {"data": [{"id": "m2", "display_name": "M2"}], "has_more": False}]
    monkeypatch.setattr(llm.requests, "get", lambda *a, **k: FakeResp(200, pages.pop(0)))
    assert [m["id"] for m in llm.list_models("anthropic", "k")] == ["m1", "m2"]


def test_list_models_bad_key_message(monkeypatch):
    monkeypatch.setattr(llm.requests, "get",
                        lambda *a, **k: FakeResp(401, {"error": {"message": "invalid x-api-key"}}))
    with pytest.raises(llm.LLMError) as e:
        llm.list_models("anthropic", "sk-secret-value")
    assert "rejected" in str(e.value) and "sk-secret-value" not in str(e.value)


def test_generate_batches_dedupes_and_shuffles(monkeypatch):
    calls = []

    def fake_complete(cfg, system, user, max_tokens=4000):
        calls.append(user)
        n = int(user.split("Write ")[1].split(" ")[0])
        items = [{"question": f"Q{len(calls)}-{i}?", "options": ["right", "w1", "w2", "w3"], "answer": "A",
                  "explanation": "because"} for i in range(n)]
        items.append(items[0])  # duplicate is dropped
        return json.dumps({"questions": items})

    monkeypatch.setattr(qs, "acomplete", lambda *a, **k: _aw(fake_complete(*a, **k)))
    import asyncio
    out, warnings = asyncio.run(qs.generate(llm.LLMConfig("anthropic", "k", "m"), "space", 23))
    assert len(out) == 23 and len(calls) == 3
    assert all(q.correct_text() == "right" for q in out)
    assert len({q.answer for q in out}) > 1


async def _aw(v):
    return v


# ---------------------------------------------------------------- youtube helpers
def test_parse_ts_variants():
    a = youtube.parse_ts("2026-11-05T10:00:00.123456789Z")
    b = youtube.parse_ts("2026-11-05T10:00:00+00:00")
    assert 0.12 < a - b < 0.13


def test_youtube_call_charges_quota_and_maps_errors(monkeypatch):
    db.set_secret("google_token", {"access_token": "t", "expires_at": 9e12})
    monkeypatch.setattr(youtube.requests, "request",
                        lambda *a, **k: FakeResp(403, {"error": {"message": "nope", "errors": [{"reason": "liveStreamingNotEnabled"}]}}))
    ok, msg = youtube.check_live_enabled()
    assert ok is False and "24 hours" in msg
    assert db.quota_used_today() == 1
    monkeypatch.setattr(youtube.requests, "request",
                        lambda *a, **k: FakeResp(403, {"error": {"message": "q", "errors": [{"reason": "quotaExceeded"}]}}))
    with pytest.raises(youtube.QuotaExhausted):
        youtube.list_chat("c", None)
    db.set_setting("youtube_daily_quota", db.quota_used_today())  # nothing left
    with pytest.raises(youtube.QuotaExhausted):
        youtube.list_chat("c", None)


def test_auth_url_uses_pkce_and_loopback():
    youtube.save_client("abc.apps.googleusercontent.com", "s3cret")
    url = youtube.auth_url()
    assert "code_challenge_method=S256" in url and "127.0.0.1" in url and "access_type=offline" in url


# ---------------------------------------------------------------- updates
def test_version_comparison(monkeypatch):
    from quizcaster import updates
    assert updates._parse("v0.10.0".lstrip("v")) > updates._parse("0.9.9")
    assert updates._parse("1.2") == (1, 2)


def test_update_check_reports_newer_release(monkeypatch):
    from quizcaster import updates
    updates._cache.update(at=0.0, data=None)

    class R:
        status_code = 200
        def json(self):
            return {"tag_name": "v9.9.0", "html_url": "https://example/x", "body": "notes"}
    monkeypatch.setattr(updates.requests, "get", lambda *a, **k: R())
    res = updates.check(force=True)
    assert res["update_available"] is True and res["latest"] == "9.9.0"


def test_update_check_offline_is_silent(monkeypatch):
    from quizcaster import updates
    import requests as rq
    updates._cache.update(at=0.0, data=None)

    def boom(*a, **k):
        raise rq.ConnectionError("offline")
    monkeypatch.setattr(updates.requests, "get", boom)
    res = updates.check(force=True)
    assert res["update_available"] is False and res["latest"] is None


def test_update_check_can_be_disabled():
    from quizcaster import updates
    db.set_setting("check_updates", False)
    assert updates.check(force=True)["enabled"] is False
