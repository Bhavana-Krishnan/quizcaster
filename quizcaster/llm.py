"""Bring-your-own-key LLM access for Claude (Anthropic) and OpenAI, over plain REST.

No vendor SDKs: fewer dependencies, and the app keeps working when SDK versions move.
Key verification == listing models: one cheap call proves the key and returns the
models the user can pick from.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

import requests

from . import config, db

ANTHROPIC_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1"

PROVIDERS = ("anthropic", "openai")


class LLMError(Exception):
    """Raised with a message that is safe to show the user (never contains the key)."""


@dataclass
class LLMConfig:
    provider: str
    api_key: str
    model: str


def _explain_http_error(resp: requests.Response, provider: str) -> str:
    try:
        body = resp.json()
        msg = (body.get("error") or {}).get("message") or str(body)[:300]
    except ValueError:
        msg = resp.text[:300]
    hint = {
        401: "The key was rejected. Check that you copied the whole key.",
        403: "The key is valid but not allowed to do this.",
        404: "Model or endpoint not found.",
        429: "Rate limited or out of credit. Check your billing / usage limits.",
    }.get(resp.status_code, "")
    return f"{provider} API error {resp.status_code}: {msg} {hint}".strip()


# ---------------------------------------------------------------- model listing
def list_models(provider: str, api_key: str) -> list[dict]:
    """Verify the key and return [{'id':..., 'name':...}] sorted newest-first-ish."""
    if provider not in PROVIDERS:
        raise LLMError(f"Unknown provider {provider!r}")
    try:
        if provider == "anthropic":
            models, after = [], None
            while True:
                params = {"limit": 100}
                if after:
                    params["after_id"] = after
                r = requests.get(f"{ANTHROPIC_URL}/models", params=params, timeout=20,
                                 headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION})
                if r.status_code != 200:
                    raise LLMError(_explain_http_error(r, "Anthropic"))
                body = r.json()
                models += [{"id": m["id"], "name": m.get("display_name") or m["id"]} for m in body.get("data", [])]
                if not body.get("has_more"):
                    break
                after = body.get("last_id")
            return models
        r = requests.get(f"{OPENAI_URL}/models", timeout=20, headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code != 200:
            raise LLMError(_explain_http_error(r, "OpenAI"))
        data = r.json().get("data", [])
        return [{"id": m["id"], "name": m["id"]}
                for m in sorted(data, key=lambda m: m.get("created", 0), reverse=True)
                if _is_openai_chat_model(m["id"])]
    except requests.RequestException as e:
        raise LLMError(f"Could not reach {provider}: {e.__class__.__name__}") from e


_OPENAI_EXCLUDE = re.compile(r"(embed|whisper|tts|dall-e|image|audio|realtime|moderation|transcribe|search|davinci|babbage|sora|computer-use)")


def _is_openai_chat_model(model_id: str) -> bool:
    return bool(re.match(r"^(gpt-|o\d|chatgpt-)", model_id)) and not _OPENAI_EXCLUDE.search(model_id)


# ---------------------------------------------------------------- completion
def complete(cfg: LLMConfig, system: str, user: str, max_tokens: int = 4000) -> str:
    try:
        if cfg.provider == "anthropic":
            r = requests.post(
                f"{ANTHROPIC_URL}/messages", timeout=config.LLM_TIMEOUT_SECONDS,
                headers={"x-api-key": cfg.api_key, "anthropic-version": ANTHROPIC_VERSION,
                         "content-type": "application/json"},
                json={"model": cfg.model, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": user}]})
            if r.status_code != 200:
                raise LLMError(_explain_http_error(r, "Anthropic"))
            return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        r = requests.post(
            f"{OPENAI_URL}/chat/completions", timeout=config.LLM_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
            json={"model": cfg.model, "max_completion_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        if r.status_code != 200:
            raise LLMError(_explain_http_error(r, "OpenAI"))
        return r.json()["choices"][0]["message"]["content"] or ""
    except requests.RequestException as e:
        raise LLMError(f"Could not reach {cfg.provider}: {e.__class__.__name__}") from e


async def acomplete(cfg: LLMConfig, system: str, user: str, max_tokens: int = 4000) -> str:
    return await asyncio.to_thread(complete, cfg, system, user, max_tokens)


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (handles ```json fences)."""
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
    if start == -1:
        raise LLMError("The model did not return JSON.")
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    end = text.rfind(closer)
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"The model returned malformed JSON ({e.msg}).") from e


# ---------------------------------------------------------------- saved config
def save_llm_settings(provider: str, api_key: str | None, model: str | None) -> None:
    db.set_setting("llm_provider", provider)
    if api_key:
        db.set_secret("llm_api_key", api_key)
    if model:
        db.set_setting("llm_model", model)


def current_config() -> LLMConfig | None:
    provider = db.get_setting("llm_provider")
    key = db.get_secret("llm_api_key")
    model = db.get_setting("llm_model")
    if provider and key and model:
        return LLMConfig(provider, key, model)
    return None
