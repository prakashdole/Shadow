"""Bounded OpenAI-compatible transport. No retries, fallback or raw error logs."""

from __future__ import annotations

import asyncio
import json
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from shadow.decisions import ModelResponse


class ProviderError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authorization and chat content must never follow a provider redirect.
        return None


def _failure(result, http_status=None):
    base = result.get("base_resp", {}) if type(result) is dict else {}
    status = base.get("status_code") if type(base) is dict else None
    error = result.get("error") if type(result) is dict else None
    # Parse only known numeric codes from bounded provider errors; retain no prose.
    if type(error) is dict:
        status = error.get("code", status)
        if "(2056)" in str(error.get("message", "")):
            status = 2056
        elif "(1008)" in str(error.get("message", "")):
            status = 1008
    if status in (2056, "2056"):
        return "provider_token_limit"
    if status in (1008, "1008"):
        return "provider_balance_exhausted"
    if status in (1002, "1002") or http_status == 429:
        return "provider_rate_limited"
    if status in (1004, 2049) or http_status in (401, 403):
        return "provider_auth_failed"
    return "provider_rejected" if error or status not in (None, 0) else "provider_http_error"


class OpenAITransport:
    def __init__(self, *, base_url, api_key, models, max_tokens=1024, context_chars=32000):
        url = urlsplit(base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("A credential-free HTTPS provider base URL is required")
        if type(api_key) is not str or not api_key.strip() or any(c.isspace() for c in api_key):
            raise ValueError("An API key must be supplied privately")
        if (set(models) != {"risk", "reply", "extraction"}
                or any(type(m) is not str or not m.strip() for m in models.values())):
            raise ValueError("An explicit model ID is required for every role")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
            raise ValueError("Invalid provider output token budget")
        if type(context_chars) is not int or not 1000 <= context_chars <= 1000000:
            raise ValueError("Invalid provider input budget")
        self.base_url, self._key = base_url.rstrip("/"), api_key
        self.models, self.max_tokens, self.context_chars = dict(models), max_tokens, context_chars
        self._opener = build_opener(_NoRedirect())

    def configuration(self, role):
        return {"provider": "openai_compatible", "base_url": self.base_url,
                "model": self.models[role], "max_tokens": self.max_tokens,
                "context_chars": self.context_chars, "socket_timeout_seconds": 20,
                "transport_attempts": 1, "structured_output": "strict_json_validation"}

    def _request(self, role, system, input_text):
        body = {"model": self.models[role], "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": input_text}],
            "max_tokens": self.max_tokens, "stream": False}
        if urlsplit(self.base_url).hostname == "api.minimax.io":
            body["reasoning_split"] = True
        request = Request(self.base_url + "/chat/completions",
                          data=json.dumps(body, ensure_ascii=False).encode(),
                          headers={"Authorization": "Bearer " + self._key,
                                   "Content-Type": "application/json"}, method="POST")
        try:
            with self._opener.open(request, timeout=20) as response:
                payload = response.read(262145)
        except HTTPError as exc:
            status = exc.code
            try:
                result = json.loads(exc.read(262144))
            except (ValueError, OSError):
                result = {}
            finally:
                exc.close()
            raise ProviderError(_failure(result, status)) from None
        except (TimeoutError, socket.timeout):
            raise TimeoutError("Provider timed out") from None
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise TimeoutError("Provider timed out") from None
            raise ProviderError("provider_network_error") from None
        if len(payload) > 262144:
            raise ProviderError("provider_response_too_large")
        try:
            result = json.loads(payload)
            if type(result) is not dict:
                raise ValueError
            base = result.get("base_resp", {})
            if type(base) is not dict:
                raise ValueError
            status = base.get("status_code", 0)
            if status != 0 or result.get("error"):
                raise ProviderError(_failure(result))
            choices = result["choices"]
            if type(choices) is not list or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            message = choice["message"]
            if (message.get("role") != "assistant" or type(message.get("content")) is not str
                    or message.get("tool_calls") or message.get("function_call")):
                raise ValueError
            usage = result.get("usage", {})
            if type(usage) is not dict:
                raise ValueError
            model = result.get("model", self.models[role])
            if type(model) is not str or not model:
                raise ValueError
            finish = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}
            return ModelResponse(message["content"], usage, model,
                                 finish.get(choice.get("finish_reason"), "invalid_content"))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError("provider_invalid_response") from None

    async def generate(self, role, system, input_text):
        if role not in self.models or len(system) + len(input_text) > self.context_chars:
            raise ValueError("Unknown role or context exceeds declared provider budget")
        return await asyncio.to_thread(self._request, role, system, input_text)


def configured_transport():
    """One provider selection shared by observe and private evaluation."""
    import os

    models = {role: os.environ.get(f"SHADOW_{role.upper()}_MODEL", "").strip()
              for role in ("risk", "reply", "extraction")}
    provider = os.environ.get("SHADOW_MODEL_PROVIDER", "bedrock").strip()
    if provider == "openai":
        return OpenAITransport(base_url=os.environ.get("SHADOW_OPENAI_BASE_URL", ""),
                               api_key=os.environ.get("SHADOW_OPENAI_API_KEY", ""), models=models,
                               max_tokens=int(os.environ.get("SHADOW_MODEL_MAX_TOKENS", "1024")))
    if provider == "bedrock":
        from shadow.model_bedrock import BedrockTransport
        return BedrockTransport(region=os.environ.get("SHADOW_AWS_REGION", ""), models=models)
    raise ValueError("SHADOW_MODEL_PROVIDER must be bedrock or openai; no fallback")
