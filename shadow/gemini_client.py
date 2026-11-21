"""Gemini calls for transcription, reel summaries and the reply fallback.

Backup keys are used only when a key is rejected. A 429 stays on that key.
A daily quota stops the turn and leaves it pending. Nothing here logs prompts,
images or key material.
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ProviderFailure(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def gemini_keys(environ=None):
    env = os.environ if environ is None else environ
    found = []
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        value = env.get(name, "")
        if type(value) is str and value.strip():
            found.append(value.strip())
    return found


def _class_of(status, body):
    text = body[:800].decode("utf-8", "replace").lower()
    if status in (401, 403) or "api_key_invalid" in text or "permission_denied" in text:
        return "auth"
    if status == 429 or "resource_exhausted" in text:
        if any(token in text for token in ("perday", "per_day", "daily", "per day")):
            return "quota"
        return "rate_limited"
    if status == 408:
        return "timeout"
    return "provider"


def _post(model, parts, key, thinking, timeout):
    config = {"temperature": 0, "responseMimeType": "application/json"}
    if thinking:
        config["thinkingConfig"] = {"thinkingLevel": thinking}
    payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": config}
    request = Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(1_000_001)
            status = response.status
    except HTTPError as exc:
        try:
            body = exc.read(800)
        except OSError:
            body = b""
        finally:
            exc.close()
        raise ProviderFailure(_class_of(exc.code, body)) from None
    except TimeoutError:
        raise ProviderFailure("timeout") from None
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ProviderFailure("timeout") from None
        raise ProviderFailure("provider") from None
    if len(body) > 1_000_000:
        raise ProviderFailure("provider")
    return status, body


def generate(*, model, parts, thinking=None, timeout=60, keys=None, call=None, sleep=None, attempts=3):
    """Call Gemini. `call(key) -> (status, body)` replaces HTTP in tests."""
    import time
    keys = gemini_keys() if keys is None else list(keys)
    if not keys:
        raise ProviderFailure("auth")
    pause = time.sleep if sleep is None else sleep
    last = ProviderFailure("auth")
    for index, key in enumerate(keys):
        for attempt in range(attempts):
            try:
                status, body = (call(key) if call else _post(model, parts, key, thinking, timeout))
            except ProviderFailure as exc:
                last = exc
                if exc.code == "quota":
                    raise
                if exc.code == "auth":
                    break
                if exc.code in ("rate_limited", "timeout") and attempt + 1 < attempts:
                    pause(0.5 * (2 ** attempt))
                    continue
                raise
            if status != 200:
                last = ProviderFailure(_class_of(status, body))
                if last.code == "quota":
                    raise last
                if last.code == "auth":
                    break
                if last.code in ("rate_limited", "timeout") and attempt + 1 < attempts:
                    pause(0.5 * (2 ** attempt))
                    continue
                raise last
            return _parse(body, model)
        if last.code == "auth" and index + 1 < len(keys):
            continue
        raise last
    raise last


def _parse(body, model):
    try:
        payload = json.loads(body)
        parts = payload["candidates"][0]["content"]["parts"]
        texts = [part["text"] for part in parts if part.get("text") and not part.get("thought")]
        if not texts:
            raise ValueError
        usage = payload.get("usageMetadata") or {}
        return {
            "text": "\n".join(texts),
            "model": payload.get("modelVersion") or model,
            "usage": {"prompt_tokens": usage.get("promptTokenCount"),
                      "output_tokens": usage.get("candidatesTokenCount"),
                      "total_tokens": usage.get("totalTokenCount")},
        }
    except (ValueError, KeyError, TypeError, IndexError):
        raise ProviderFailure("provider") from None
