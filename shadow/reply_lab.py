"""Local reply comparison. Frozen screenshot prompts, no DM delivery.

Each turn freezes one system prompt and one transcript. Four models each
return four independent replies. Votes stay in this database.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener

from shadow.config import ROOT
from shadow.context_memory import STYLE_RULES
from shadow.gemini_client import ProviderFailure, gemini_keys, generate as gemini_generate
from shadow.model_bedrock import BedrockTransport
from shadow.model_openai import OpenAITransport, ProviderError, _NoRedirect, openai_api_key
from shadow.screenshot_loop import coerce_reply, evidence_problem, parse_transcript, reply_system
from shadow.decisions import parse_role

# Same reply bounds the screenshot draft path passes to parse_role.
REPLY_PARSE = {"max_items": 1, "max_chars": 180, "max_total": 180}
SHARED_TEMPERATURE = 0.7
SHARED_MAX_TOKENS = 200
CONCURRENCY = 4
OPENAI_CODES = {
    "provider_rate_limited": "rate_limited",
    "provider_auth_failed": "auth",
    "provider_token_limit": "truncated",
    "provider_balance_exhausted": "quota",
}
BASELINE = {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "base_url": "https://api.openai.com/v1",
    "region": "",
}
RETRYABLE = {"failure", "invalid", "pending"}
CASE_ID = re.compile(r"^[a-z0-9-]{1,40}$")
RUN_ID = re.compile(r"^[a-f0-9]{16}$")
HISTORICAL_CASES = ROOT / "data/import/instagram-2026-11-21/reply_lab_cases.json"
PROVIDERS = ("openai", "gemini", "bedrock", "fake", "sarvam", "kiro")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    contact TEXT NOT NULL,
    language TEXT NOT NULL,
    tone TEXT NOT NULL,
    silence_ok INTEGER NOT NULL,
    bubbles_json TEXT NOT NULL,
    media_json TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    reference_text TEXT NOT NULL,
    authorship TEXT NOT NULL,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    seq INTEGER,
    checked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lineup (
    position INTEGER PRIMARY KEY,
    spec_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    system_text TEXT NOT NULL,
    user_text TEXT NOT NULL,
    digest TEXT NOT NULL,
    lineup_json TEXT NOT NULL,
    shuffle_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    run_id TEXT NOT NULL,
    slot INTEGER NOT NULL,
    sample INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason_code TEXT,
    error_code TEXT,
    messages_json TEXT NOT NULL,
    latency_ms INTEGER,
    attempts INTEGER NOT NULL,
    settings_json TEXT NOT NULL,
    prior_json TEXT NOT NULL,
    PRIMARY KEY (run_id, slot, sample)
);
CREATE TABLE IF NOT EXISTS votes (
    run_id TEXT PRIMARY KEY,
    choice TEXT NOT NULL,
    slot INTEGER,
    sample INTEGER,
    own_reply TEXT NOT NULL,
    ratings_used INTEGER NOT NULL,
    as_is_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    case_id TEXT,
    provider TEXT,
    model_id TEXT,
    messages_json TEXT
);
CREATE TABLE IF NOT EXISTS model_picks (
    run_id TEXT NOT NULL REFERENCES runs(id),
    slot INTEGER NOT NULL CHECK (slot BETWEEN 0 AND 3),
    sample INTEGER CHECK (sample BETWEEN 0 AND 3),
    PRIMARY KEY (run_id, slot)
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _esc(value):
    return html.escape(str(value), quote=True)


def _bubble(order, side, text):
    return {
        "order": order, "side": side, "text": text, "quote": None, "media": None,
        "unreadable": False, "clipped": False,
    }


def _bubbles(*rows):
    built = [_bubble(index, side, text) for index, (side, text) in enumerate(rows, 1)]
    parse_transcript({"bubbles": built, "incomplete": False})
    return built


def _reply_json(messages, action="reply", reason="ok"):
    return json.dumps({
        "schema_version": 1, "action": action, "reason_code": reason, "messages": messages,
        "evidence_event_ids": [], "used_memory_ids": [],
    }, ensure_ascii=False)


FIXTURES = [
    {
        "id": "maya-file", "title": "File check", "contact": "Maya", "language": "en",
        "tone": "casual", "silence_ok": False,
        "bubbles": _bubbles(("left", "did the file arrive?")),
        "media": {}, "rules": [], "reference": "yea got it, check downloads",
        "authorship": "owner_written",
    },
    {
        "id": "rohit-plan", "title": "Leaving time", "contact": "Rohit", "language": "hinglish",
        "tone": "casual", "silence_ok": False,
        "bubbles": _bubbles(
            ("right", "kal ka plan pakka?"),
            ("left", "bhai aaj aara hai kya"),
            ("left", "kitne baje nikalna hai"),
        ),
        "media": {}, "rules": [], "reference": "", "authorship": "",
    },
    {
        "id": "neel-demo", "title": "Demo worked", "contact": "Neel", "language": "en",
        "tone": "casual", "silence_ok": False,
        "bubbles": _bubbles(("right", "kal wala demo kaisa gaya"), ("left", "it finally worked")),
        "media": {}, "rules": [], "reference": "", "authorship": "",
    },
    {
        "id": "priya-talk", "title": "Presentation", "contact": "Priya", "language": "en",
        "tone": "serious", "silence_ok": False,
        "bubbles": _bubbles(
            ("right", "how did the presentation go"),
            ("left", "it went badly. i froze on the second slide"),
        ),
        "media": {}, "rules": [STYLE_RULES["tone"]["serious"]], "reference": "", "authorship": "",
    },
    {
        "id": "dev-close", "title": "Already settled", "contact": "Dev", "language": "en",
        "tone": "casual", "silence_ok": True,
        "bubbles": _bubbles(("right", "then we're set for thursday"), ("left", "ok cool")),
        "media": {}, "rules": [], "reference": "", "authorship": "",
    },
    {
        "id": "aisha-mix", "title": "Studio plan", "contact": "Aisha", "language": "en",
        "tone": "casual", "silence_ok": False,
        "bubbles": _bubbles(
            ("left", "are you coming to the studio"),
            ("left", "or should we send the mix without you"),
        ),
        "media": {}, "rules": [], "reference": "", "authorship": "",
    },
]


def lab_directory(path):
    target = Path(path).expanduser().resolve()
    root = Path(__file__).resolve().parents[1]
    if target == root or root in target.parents:
        raise ValueError("Reply lab storage must stay outside the checkout")
    target.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(target, 0o700)
        if target.stat().st_mode & 0o077:
            raise ValueError("Reply lab directory must be owner-only")
    return target


def assert_fixtures_separated():
    for case in FIXTURES:
        frozen = freeze_case(case)
        if separation_problem(case, frozen):
            raise RuntimeError(f"Fixture {case['id']} mixes its reference into the prompt")


def inbound_orders(bubbles):
    last = -1
    for index, bubble in enumerate(bubbles):
        if bubble["side"] == "right":
            last = index
    return {
        bubbles[index]["order"] for index in range(last + 1, len(bubbles))
        if bubbles[index]["side"] == "left"
    }


def freeze_case(case):
    """One screenshot reply input: voice, case rules, contract, transcript, media."""
    system = reply_system(extra=tuple(case["rules"]))
    user = json.dumps({"bubbles": case["bubbles"], "media": case["media"]}, ensure_ascii=False)
    digest = hashlib.sha256((system + "\n" + user).encode()).hexdigest()
    return {"system": system, "user": user, "digest": digest}


def separation_problem(case, frozen):
    reference = (case.get("reference") or "").strip()
    if not reference:
        return None
    # Checked cases already proved the archived reply is after the cutoff and not in the transcript.
    if case.get("checked"):
        return None
    if len(reference) < 8:
        return "Reference replies need at least 8 characters to prove they stayed out of the prompt"
    if reference in frozen["system"] or reference in frozen["user"]:
        return "This case stays out of scored runs: the reference reply appears in the prompt"
    return None


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("Archive timestamps must include a timezone") from None
    if parsed.tzinfo is None:
        raise ValueError("Archive timestamps must include a timezone")
    return parsed


def _archive_bubbles(messages):
    bubbles = []
    for index, message in enumerate(messages, 1):
        if type(message) is not dict:
            raise ValueError("Archive messages must be objects")
        role = message.get("role")
        if role == "owner":
            side = "right"
        elif role == "contact":
            side = "left"
        else:
            raise ValueError("Archive roles must be owner or contact")
        text = message.get("text")
        if type(text) is not str or not text.strip() or len(text) > 1000:
            raise ValueError("Archive messages need short text")
        ident = message.get("id")
        if type(ident) is not int or ident < 1:
            raise ValueError("Archive message ids must be positive")
        bubbles.append(_bubble(index, side, text))
    return bubbles


def load_archive(path):
    """Build lab cases from the historical export. Rules stay empty: the file has no cutoff-dated relationship rules."""
    data = json.loads(Path(path).read_text())
    if data.get("schema_version") != 1 or type(data.get("cases")) is not list:
        raise ValueError("Unrecognized reply lab case file")
    references = data.get("references_by_case_id")
    if type(references) is not dict:
        raise ValueError("Archive references are missing")
    prepared = []
    seen = set()
    for index, raw in enumerate(data["cases"]):
        if type(raw) is not dict or not CASE_ID.fullmatch(str(raw.get("case_id", ""))):
            raise ValueError("Archive case id is not usable")
        case_id = raw["case_id"]
        if case_id in seen or case_id not in references:
            raise ValueError("Archive cases must be unique and have a reference")
        seen.add(case_id)
        model_input = raw.get("model_input")
        source = raw.get("source")
        if type(model_input) is not dict or type(source) is not dict:
            raise ValueError("Archive case is missing its transcript")
        prior = model_input.get("prior_messages")
        inbound = model_input.get("inbound_messages")
        if type(prior) is not list or type(inbound) is not list or not inbound:
            raise ValueError("Archive case needs an inbound turn")
        messages = prior + inbound
        if any(type(item) is not dict for item in messages):
            raise ValueError("Archive messages must be objects")
        ids = [item.get("id") for item in messages]
        if not ids or type(ids[0]) is not int or ids != list(range(ids[0], ids[0] + len(ids))):
            raise ValueError("Transcript ids must be contiguous")
        if source.get("first_line") != ids[0] or source.get("last_inbound_line") != ids[-1]:
            raise ValueError("Source lines do not match the transcript")
        if type(source.get("chat_log")) is not str or not source["chat_log"].strip():
            raise ValueError("Source chat log is missing")
        cutoff = _stamp(raw.get("cutoff_at"))
        if any(_stamp(item.get("sent_at")) > cutoff for item in messages):
            raise ValueError("Transcript includes a message after the cutoff")
        if _stamp(inbound[-1].get("sent_at")) != cutoff or any(item.get("role") != "contact" for item in inbound):
            raise ValueError("Cutoff must be the latest inbound message")
        reference = references[case_id]
        if type(reference) is not dict or reference.get("provenance") != "owner_account_sent_human_authorship_unverified":
            raise ValueError("Reference provenance is missing")
        ref_messages = reference.get("messages")
        if type(ref_messages) is not list or not ref_messages:
            raise ValueError("Reference reply is missing")
        parts = []
        for item in ref_messages:
            if type(item) is not dict or item.get("id") in ids or type(item.get("id")) is not int or item["id"] <= ids[-1]:
                raise ValueError("Reference must be after the transcript")
            if _stamp(item.get("sent_at")) <= cutoff:
                raise ValueError("Reference must be after the cutoff")
            text = item.get("text")
            if type(text) is not str or not text.strip():
                raise ValueError("Reference reply needs text")
            parts.append(text)
        bubbles = _archive_bubbles(messages)
        tone = raw.get("category") if type(raw.get("category")) is str else "archived"
        kind = raw.get("conversation_type") if raw.get("conversation_type") in ("direct", "group") else "direct"
        prepared.append({
            "id": case_id,
            "title": f"Case {index + 1}",
            "contact": "Group" if kind == "group" else "Direct",
            "language": "archived",
            "tone": tone,
            "silence_ok": False,
            "bubbles": bubbles,
            "media": {},
            "rules": [],
            "reference": "\n".join(parts),
            "authorship": "owner_sent",
            "checked": True,
            "seq": index,
        })
    if len(prepared) != data.get("counts", {}).get("cases", len(prepared)):
        raise ValueError("Archive case count does not match")
    return prepared


def classify_reply(raw):
    if type(raw) is not str or len(raw) > 16000:
        return _invalid()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return _invalid()
    held = type(value) is dict and value.get("action") == "hold"
    try:
        payload = coerce_reply(raw) if held else raw
        outcome = parse_role("reply", payload, set(), **REPLY_PARSE)
    except (ValueError, TypeError):
        return _invalid()
    if outcome.failure or outcome.value is None:
        return _invalid()
    draft = outcome.value
    status = "hold" if held else draft.action
    return {
        "status": status, "reason_code": draft.reason_code,
        "messages": [] if status in ("hold", "skip") else list(draft.messages),
        "error_code": None,
    }


def _invalid():
    return {"status": "invalid", "reason_code": None, "messages": [], "error_code": None}


def _failure(code):
    if code not in ("timeout", "auth", "quota", "rate_limited", "provider", "truncated"):
        code = "provider"
    return {"status": "failure", "reason_code": None, "messages": [], "error_code": code}


def call_settings(spec):
    recorded = {
        "temperature": SHARED_TEMPERATURE,
        "max_tokens": SHARED_MAX_TOKENS,
        "attempts": 1,
        "timeout_seconds": 45,
    }
    recorded.update({
        "openai": {"json_mode": "json_object", "socket_timeout_seconds": 20},
        "gemini": {"json_mode": "response_mime_json", "thinking": None, "auth_key_rotation": "same_model",
                   "socket_timeout_seconds": 20, "attempts": 2},
        "bedrock": {"json_mode": "none", "socket_timeout_seconds": 20},
        "fake": {"json_mode": "fake"},
        "sarvam": {"json_mode": "json_object", "reasoning_effort": None, "socket_timeout_seconds": 45},
        "kiro": {"json_mode": "anthropic_messages", "credential": "loopback_only"},
    }[spec["provider"]])
    return recorded


def parse_paste(text):
    stripped = text.strip()
    if not stripped:
        raise ValueError("Paste a transcript")
    if stripped.startswith("{"):
        data = json.loads(stripped)
        if type(data) is not dict or "bubbles" not in data or "incomplete" not in data:
            raise ValueError("Transcript JSON needs bubbles and incomplete")
        parsed = parse_transcript(data)
        if parsed["incomplete"]:
            raise ValueError("Finish transcription before comparing replies")
        media = data.get("media") or {}
        if type(media) is not dict or len(json.dumps(media)) > 4000:
            raise ValueError("Media must be a small object")
        _bound_bubbles(parsed["bubbles"])
        return parsed["bubbles"], media
    bubbles = []
    for line in stripped.splitlines():
        if not line.strip():
            continue
        lower = line.lower()
        if lower.startswith(("them:", "left:", "in:")):
            side = "left"
        elif lower.startswith(("me:", "right:", "out:")):
            side = "right"
        else:
            raise ValueError("Start each line with them: or me:")
        body = line.split(":", 1)[1].strip()
        if not body or len(body) > 1000:
            raise ValueError("Each bubble needs text under 1000 characters")
        bubbles.append(_bubble(len(bubbles) + 1, side, body))
    _bound_bubbles(bubbles)
    return parse_transcript({"bubbles": bubbles, "incomplete": False})["bubbles"], {}


def _bound_bubbles(bubbles):
    if not bubbles or len(bubbles) > 40:
        raise ValueError("Keep a transcript between 1 and 40 bubbles")


def default_fake_lineup():
    names = ("lab-alpha", "lab-bravo", "lab-charlie", "lab-delta")
    words = ("amber", "birch", "cedar", "dune")
    return [_slot("fake", name, script=[f"{word} {index}" for index in range(1, 5)])
            for name, word in zip(names, words)]


def _slot(provider, model, base_url="", region="", script=None, fail_once=(), invalid_once=(),
          sleep=None, sleep_samples=()):
    return {
        "provider": provider, "model": model, "base_url": base_url, "region": region,
        "script": list(script) if script else None,
        "fail_once": [int(item) for item in fail_once],
        "invalid_once": [int(item) for item in invalid_once],
        "sleep": sleep, "sleep_samples": [int(item) for item in sleep_samples],
    }


def prepared_lineup():
    return [
        _slot("sarvam", os.environ.get("REPLY_LAB_SARVAM_MODEL", "sarvam-105b"),
              base_url=os.environ.get("REPLY_LAB_SARVAM_BASE_URL", "https://api.sarvam.ai/v1")),
        _slot("gemini", os.environ.get("REPLY_LAB_GEMINI_FIRST_MODEL", "gemini-3.5-flash-lite")),
        _slot("gemini", os.environ.get("REPLY_LAB_GEMINI_SECOND_MODEL", "gemini-3.1-flash-lite")),
        _slot("kiro", os.environ.get("REPLY_LAB_KIRO_MODEL", "claude-sonnet-5.5"),
              base_url=os.environ.get("REPLY_LAB_KIRO_BASE_URL", "http://127.0.0.1:8000/v1")),
    ]


def _https_base(base_url, fallback):
    base_url = base_url or fallback
    parts = urlsplit(base_url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ValueError("Provider base URL must be credential-free HTTPS")
    return base_url


def normalize_lineup(slots):
    if type(slots) is not list or len(slots) != 4:
        raise ValueError("Configure exactly four models")
    normal = []
    for raw in slots:
        if type(raw) is not dict:
            raise ValueError("Each model needs a provider and id")
        provider = raw.get("provider")
        if provider not in PROVIDERS:
            raise ValueError("Unknown provider")
        model = raw.get("model", "")
        if type(model) is not str or not model.strip() or any(char.isspace() for char in model) or len(model) > 120:
            raise ValueError("Enter four model ids")
        base_url = raw.get("base_url") or ""
        region = raw.get("region") or ""
        if type(base_url) is not str or type(region) is not str:
            raise ValueError("Provider settings must be text")
        base_url, region = base_url.strip(), region.strip()
        if provider == "openai":
            base_url = _https_base(base_url, BASELINE["base_url"])
        if provider == "sarvam":
            base_url = _https_base(base_url, "https://api.sarvam.ai/v1")
        if provider == "kiro":
            base_url = base_url or "http://127.0.0.1:8000/v1"
            parts = urlsplit(base_url)
            if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.username
                    or parts.password or parts.query or parts.fragment):
                raise ValueError("Kiro gateway must be loopback HTTP with no credentials in the URL")
        if provider == "bedrock" and region and (any(char.isspace() for char in region) or len(region) > 40):
            raise ValueError("Region must be one token")
        script = raw.get("script")
        if script is not None and (type(script) is not list or len(script) != 4
                                    or any(type(item) is not str or not item for item in script)):
            raise ValueError("A fake script needs four replies")
        normal.append(_slot(
            provider, model.strip(), base_url, region, script,
            raw.get("fail_once") or [], raw.get("invalid_once") or [],
            raw.get("sleep"), raw.get("sleep_samples") or [],
        ))
    identities = [(item["provider"], item["model"], item["base_url"], item["region"]) for item in normal]
    if len(set(identities)) != 4:
        raise ValueError("Choose four distinct provider and model combinations")
    return normal


def dispatch(spec, system, user, sample, attempt):
    provider = spec["provider"]
    if provider == "fake":
        return fake_reply(spec, sample, attempt)
    if provider == "openai":
        return openai_reply(spec, system, user)
    if provider == "gemini":
        return gemini_reply(spec, system, user)
    if provider == "bedrock":
        return bedrock_reply(spec, system, user)
    if provider == "sarvam":
        return sarvam_reply(spec, system, user)
    if provider == "kiro":
        return kiro_reply(spec, system, user)
    raise ProviderFailure("provider")


def fake_reply(spec, sample, attempt):
    if sample in spec["fail_once"] and attempt == 1:
        raise ProviderFailure("provider")
    if sample in spec["invalid_once"] and attempt == 1:
        return "nope"
    if spec["sleep"] and sample in spec["sleep_samples"]:
        time.sleep(spec["sleep"])
    script = spec["script"]
    if not script:
        token = hashlib.sha256(spec["model"].encode()).hexdigest()[:8]
        script = [f"draft {token} {index}" for index in range(1, 5)]
    return _reply_json([script[sample]])


def _chat_reply(spec, system, user, key, *, body_extra=None, timeout=20):
    transport = OpenAITransport(
        base_url=spec["base_url"], api_key=key,
        models={role: spec["model"] for role in ("risk", "reply", "extraction")},
        max_tokens=SHARED_MAX_TOKENS, json_object=True, temperature=SHARED_TEMPERATURE,
        body_extra=body_extra, timeout=timeout,
    )
    try:
        response = asyncio.run(transport.generate("reply", system, user))
    except ProviderError as exc:
        raise ProviderFailure(OPENAI_CODES.get(exc.code, "provider")) from None
    except TimeoutError:
        raise ProviderFailure("timeout") from None
    if response.stop_reason == "max_tokens":
        raise ProviderFailure("truncated")
    if response.stop_reason != "end_turn":
        raise ProviderFailure("provider")
    return response.text


def sarvam_reply(spec, system, user):
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not key:
        raise ProviderFailure("auth")
    return _chat_reply(spec, system, user, key, body_extra={"reasoning_effort": None}, timeout=45)


def kiro_reply(spec, system, user):
    key = os.environ.get("REPLY_LAB_KIRO_API_KEY", "").strip()
    if not key or any(char.isspace() for char in key):
        raise ProviderFailure("auth")
    parts = urlsplit(spec["base_url"].rstrip("/") + "/messages")
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.username
            or parts.password or parts.query or parts.fragment):
        raise ProviderFailure("provider")
    url = parts.geturl()
    body = {
        "model": spec["model"], "max_tokens": SHARED_MAX_TOKENS, "temperature": SHARED_TEMPERATURE,
        "system": system, "messages": [{"role": "user", "content": user}],
    }
    request = Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    })
    try:
        with build_opener(_NoRedirect()).open(request, timeout=45) as response:
            payload = response.read(262145)
    except HTTPError as exc:
        code = {401: "auth", 403: "auth", 402: "quota", 429: "rate_limited"}.get(exc.code, "provider")
        exc.close()
        raise ProviderFailure(code) from None
    except TimeoutError:
        raise ProviderFailure("timeout") from None
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ProviderFailure("timeout") from None
        raise ProviderFailure("provider") from None
    if len(payload) > 262144:
        raise ProviderFailure("provider")
    try:
        result = json.loads(payload)
        blocks = result.get("content")
        if type(result) is not dict or type(blocks) is not list:
            raise ValueError
        text = "".join(block.get("text", "") for block in blocks
                       if type(block) is dict and block.get("type") == "text")
    except (ValueError, TypeError):
        raise ProviderFailure("provider") from None
    if result.get("stop_reason") == "max_tokens":
        raise ProviderFailure("truncated")
    return text


def openai_reply(spec, system, user):
    key = openai_api_key()
    if not key:
        raise ProviderFailure("auth")
    return _chat_reply(spec, system, user, key)


def gemini_reply(spec, system, user):
    if not gemini_keys():
        raise ProviderFailure("auth")
    settings = call_settings(spec)
    try:
        result = gemini_generate(
            model=spec["model"], system=system, parts=[{"text": user}],
            temperature=SHARED_TEMPERATURE, max_output_tokens=SHARED_MAX_TOKENS,
            timeout=settings["socket_timeout_seconds"], attempts=settings["attempts"], thinking=settings["thinking"],
        )
    except ProviderFailure:
        raise
    except TimeoutError:
        raise ProviderFailure("timeout") from None
    except Exception:
        raise ProviderFailure("provider") from None
    return result["text"]


def bedrock_reply(spec, system, user):
    region = spec["region"] or os.environ.get("SHADOW_AWS_REGION", "").strip()
    if not region:
        raise ProviderFailure("auth")
    inference = {role: {"maxTokens": SHARED_MAX_TOKENS, "temperature": SHARED_TEMPERATURE}
                 for role in ("risk", "reply", "extraction")}
    try:
        transport = BedrockTransport(
            region=region, models={role: spec["model"] for role in ("risk", "reply", "extraction")},
            inference=inference,
        )
        response = asyncio.run(transport.generate("reply", system, user))
    except ProviderFailure:
        raise
    except TimeoutError:
        raise ProviderFailure("timeout") from None
    except Exception:
        raise ProviderFailure("provider") from None
    if response.stop_reason == "max_tokens":
        raise ProviderFailure("truncated")
    if response.stop_reason not in ("end_turn", "stop"):
        raise ProviderFailure("provider")
    return response.text


def _reviewed(run):
    return bool(run["vote"]) or len(run.get("model_picks", {})) == 4


def tally(runs):
    """Count independent model picks and preserve legacy head-to-head results."""
    ranked, reviewed, incomplete, awaiting = [], [], [], []
    for run in runs:
        if not run["complete"]:
            incomplete.append(run)
        elif not _reviewed(run):
            awaiting.append(run)
        elif run["vote"]:
            ranked.append(run)
        else:
            reviewed.append(run)
    models = {}

    def bucket(run, slot):
        spec = run["lineup"][slot]
        key = (spec["provider"], spec["model"])
        row = models.get(key)
        if row is None:
            row = models[key] = {
                "provider": spec["provider"], "model": spec["model"],
                "sole_wins": 0, "tie_credits": 0, "failures": 0, "invalid": 0,
                "attempts": 0, "retries": 0, "sendable": 0, "judged": 0,
                "unrated": 0, "first_not_reply": 0,
                "drafts_picked": 0, "none_fit": 0, "draft_errors": 0,
            }
        return row

    ties = no_reply = none = 0
    for run in ranked:
        vote = run["vote"]
        found = {(item["slot"], item["sample"]): item for item in run["candidates"]}
        for slot in range(4):
            row = bucket(run, slot)
            for sample in range(4):
                item = found[(slot, sample)]
                row["attempts"] += 1
                row["retries"] += max(0, item["attempts"] - 1)
                if item["status"] == "failure":
                    row["failures"] += 1
                elif item["status"] == "invalid":
                    row["invalid"] += 1
            first = found[(slot, 0)]
            if first["status"] != "reply":
                row["first_not_reply"] += 1
                continue
            picked = vote["choice"] == "draft" and vote["slot"] == slot and vote["sample"] == 0
            marked = vote["ratings_used"] and f"{slot}:0" in vote["as_is"]
            if not vote["ratings_used"] and not picked:
                row["unrated"] += 1
                continue
            row["judged"] += 1
            if picked or marked:
                row["sendable"] += 1
        if vote["choice"] == "no_reply":
            no_reply += 1
            continue
        if vote["choice"] == "none":
            none += 1
            continue
        chosen = found[(vote["slot"], vote["sample"])]
        text = tuple(chosen["messages"])
        winners = []
        for slot in range(4):
            if any(found[(slot, sample)]["status"] == "reply" and tuple(found[(slot, sample)]["messages"]) == text
                   for sample in range(4)):
                winners.append(slot)
        if len(winners) == 1:
            bucket(run, winners[0])["sole_wins"] += 1
        else:
            ties += 1
            for slot in winners:
                bucket(run, slot)["tie_credits"] += 1
    for run in reviewed:
        for slot, sample in run["model_picks"].items():
            row = bucket(run, slot)
            row["drafts_picked" if sample is not None else "none_fit"] += 1
        for item in run["candidates"]:
            if item["status"] in ("failure", "invalid"):
                bucket(run, item["slot"])["draft_errors"] += 1
    rows = sorted(models.values(), key=lambda item: (item["provider"], item["model"]))
    return {
        "reviewed_cases": len(reviewed),
        "ranked_turns": len(ranked), "ties": ties, "no_reply": no_reply, "none": none,
        "models": rows, "incomplete": [_brief(run) for run in incomplete],
        "awaiting": [_brief(run) for run in awaiting],
    }


def _brief(run):
    counts = {"failure": 0, "invalid": 0, "pending": 0}
    for item in run["candidates"]:
        if item["status"] in counts:
            counts[item["status"]] += 1
    terminal = sum(item["status"] != "pending" for item in run["candidates"])
    return {"id": run["id"], "title": run["title"], "terminal": terminal, "total": 16, **counts}


class Lab:
    def __init__(self, directory, *, fake=False, timeout=45, trace=False):
        assert_fixtures_separated()
        self.directory = lab_directory(directory)
        self.timeout = timeout
        self.trace = trace
        self.trace_log = []
        self._lock = threading.RLock()
        self._generation = threading.Lock()
        self._preparing = set()
        self._retrying = set()
        self._ahead_thread = None
        self.db = sqlite3.connect(self.directory / "lab.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if os.name != "nt":
            os.chmod(self.directory / "lab.sqlite3", 0o600)
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()
        self._seed()
        if fake and not self.lineup():
            self.set_lineup(default_fake_lineup())

    def _migrate(self):
        def columns(table):
            return {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        for name, decl in (("seq", "INTEGER"), ("checked", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns("cases"):
                self.db.execute(f"ALTER TABLE { 'cases' } ADD COLUMN {name} {decl}")
        for name, decl in (
            ("case_id", "TEXT"), ("provider", "TEXT"), ("model_id", "TEXT"), ("messages_json", "TEXT"),
        ):
            if name not in columns("votes"):
                self.db.execute(f"ALTER TABLE votes ADD COLUMN {name} {decl}")

    def close(self):
        with self._lock:
            self.db.close()

    def calls(self):
        with self._lock:
            return list(self.trace_log)

    def _seed(self):
        with self._lock:
            if self.db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]:
                return
            for case in FIXTURES:
                self._insert_case(case, case["id"])
            self.db.commit()

    def add_case(self, **case):
        case_id = secrets.token_hex(8)
        prepared = self._prepare_case(case)
        with self._lock:
            self._insert_case(prepared, case_id)
            self.db.commit()
        return case_id

    def _prepare_case(self, case):
        bubbles = case["bubbles"]
        media = case.get("media") or {}
        parse_transcript({"bubbles": bubbles, "incomplete": False})
        _bound_bubbles(bubbles)
        rules = tuple(case.get("rules") or [])
        if any(type(rule) is not str or not rule.strip() or len(rule) > 300 for rule in rules) or len(rules) > 8:
            raise ValueError("Use at most eight short conversation rules")
        reference = (case.get("reference") or "").strip()
        authorship = case.get("authorship") or ""
        if reference and authorship not in ("owner_written", "synthetic", "owner_sent"):
            raise ValueError("Mark a reference reply owner_written or synthetic")
        if authorship == "owner_sent" and not case.get("checked"):
            raise ValueError("Archived replies need source and cutoff validation")
        if reference and len(reference) > 1000:
            raise ValueError("Reference reply is too long")
        if not reference:
            authorship = ""
        seq = case.get("seq")
        if seq is not None and (type(seq) is not int or seq < 0):
            raise ValueError("Case order is invalid")
        return {
            "title": _label(case.get("title") or "Pasted chat", 80),
            "contact": _label(case.get("contact") or "Contact", 40),
            "language": _label(case.get("language") or "unknown", 40),
            "tone": _label(case.get("tone") or "casual", 40),
            "silence_ok": 1 if case.get("silence_ok") else 0,
            "bubbles": bubbles, "media": media, "rules": [rule.strip() for rule in rules],
            "reference": reference, "authorship": authorship,
            "parent_id": case.get("parent_id"),
            "checked": 1 if case.get("checked") else 0,
            "seq": seq,
        }

    def _insert_case(self, case, case_id):
        self.db.execute(
            """INSERT INTO cases
               (id, title, contact, language, tone, silence_ok, bubbles_json, media_json, rules_json,
                reference_text, authorship, parent_id, created_at, seq, checked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (case_id, case["title"], case["contact"], case["language"], case["tone"],
             case["silence_ok"], json.dumps(case["bubbles"], ensure_ascii=False),
             json.dumps(case["media"], ensure_ascii=False), json.dumps(case["rules"], ensure_ascii=False),
             case["reference"], case["authorship"], case.get("parent_id"), _now(),
             case.get("seq"), int(case.get("checked") or 0)))

    def cases(self):
        with self._lock:
            return [self._case_from(row) for row in self.db.execute("SELECT * FROM cases ORDER BY created_at, id")]

    def case(self, case_id):
        with self._lock:
            row = self.db.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown conversation")
        return self._case_from(row)

    def _case_from(self, row):
        case = {
            "id": row["id"], "title": row["title"], "contact": row["contact"],
            "language": row["language"], "tone": row["tone"], "silence_ok": bool(row["silence_ok"]),
            "bubbles": json.loads(row["bubbles_json"]), "media": json.loads(row["media_json"]),
            "rules": json.loads(row["rules_json"]), "reference": row["reference_text"],
            "authorship": row["authorship"], "parent_id": row["parent_id"],
            "seq": row["seq"], "checked": bool(row["checked"]),
        }
        case["blocked"] = separation_problem(case, freeze_case(case))
        return case

    def set_lineup(self, slots):
        normal = normalize_lineup(slots)
        with self._lock:
            self.db.execute("DELETE FROM lineup")
            for position, spec in enumerate(normal):
                self.db.execute("INSERT INTO lineup VALUES (?, ?)", (position, json.dumps(spec)))
            self.db.commit()

    def lineup(self):
        with self._lock:
            rows = self.db.execute("SELECT spec_json FROM lineup ORDER BY position").fetchall()
        return [json.loads(row["spec_json"]) for row in rows]

    def import_archive(self, path):
        added = 0
        with self._lock:
            for raw in load_archive(path):
                if self.db.execute("SELECT 1 FROM cases WHERE id = ?", (raw["id"],)).fetchone():
                    continue
                prepared = self._prepare_case(raw)
                self._insert_case(prepared, raw["id"])
                added += 1
            self.db.commit()
        return added

    def latest_run_id(self, case_id):
        with self._lock:
            row = self.db.execute(
                "SELECT id FROM runs WHERE case_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (case_id,)).fetchone()
        return None if row is None else row["id"]

    def _seq_of(self, case_id):
        with self._lock:
            row = self.db.execute("SELECT seq FROM cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown conversation")
        return row["seq"]

    def _neighbor(self, case_id, step):
        seq = self._seq_of(case_id)
        if seq is None:
            return None
        with self._lock:
            row = self.db.execute("SELECT id FROM cases WHERE seq = ?", (seq + step,)).fetchone()
        return None if row is None else self.case(row["id"])

    def sequence_count(self):
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM cases WHERE seq IS NOT NULL").fetchone()[0]

    def is_preparing(self, case_id):
        with self._lock:
            return case_id in self._preparing

    def resume_target(self):
        with self._lock:
            rows = list(self.db.execute("SELECT id FROM cases WHERE seq IS NOT NULL ORDER BY seq"))
        if not rows:
            return "/"
        for row in rows:
            run_id = self.latest_run_id(row["id"])
            if run_id is None:
                return f"/cases/{row['id']}"
            if not _reviewed(self.run(run_id)):
                return f"/runs/{run_id}"
        return "/board"

    def after_vote_target(self, case_id):
        if self._seq_of(case_id) is None:
            return f"/runs/{self.latest_run_id(case_id)}"
        nxt = self._neighbor(case_id, 1)
        if nxt is None:
            return "/board"
        next_run = self.latest_run_id(nxt["id"])
        if next_run:
            self._queue_ahead(nxt["id"])
            return f"/runs/{next_run}"
        self._queue_ahead(case_id)
        return f"/cases/{nxt['id']}"

    def _queue_ahead(self, case_id):
        # ponytail: one daemon thread for the next case; a second start waits on the generation lock.
        if self._seq_of(case_id) is None:
            return
        nxt = self._neighbor(case_id, 1)
        if nxt is None:
            return
        with self._lock:
            next_id = nxt["id"]
            if next_id in self._preparing or self.latest_run_id(next_id):
                return
            self._preparing.add(next_id)
            thread = threading.Thread(target=self._generate_ahead, args=(next_id,), daemon=True)
            self._ahead_thread = thread
            thread.start()

    def _generate_ahead(self, case_id):
        try:
            self._generate_locked(case_id)
        except Exception as exc:
            sys.stderr.write(f"reply-lab ahead {type(exc).__name__}\n")

    def generate(self, case_id):
        run_id = self._generate_locked(case_id)
        self._queue_ahead(case_id)
        return run_id

    def _generate_locked(self, case_id):
        with self._generation:
            with self._lock:
                self._preparing.add(case_id)
            try:
                return self._generate_now(case_id)
            finally:
                with self._lock:
                    self._preparing.discard(case_id)

    def _generate_now(self, case_id):
        if self._seq_of(case_id) is not None:
            existing = self.latest_run_id(case_id)
            if existing:
                return existing
        case = self.case(case_id)
        lineup = self.lineup()
        if len(lineup) != 4:
            raise ValueError("Configure four models first")
        problem = evidence_problem(case["bubbles"], case["media"])
        if problem:
            raise ValueError(f"This turn is not drafted: {problem}")
        if not inbound_orders(case["bubbles"]):
            raise ValueError("The transcript has no inbound turn to answer")
        frozen = freeze_case(case)
        blocked = separation_problem(case, frozen)
        if blocked:
            raise ValueError(blocked)
        run_id = secrets.token_hex(8)
        order = [(slot, sample) for slot in range(4) for sample in range(4)]
        random.Random(run_id).shuffle(order)
        with self._lock:
            self.db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, case_id, frozen["system"], frozen["user"], frozen["digest"],
                 json.dumps(lineup), json.dumps(order), _now()))
            for slot, sample in order:
                self.db.execute(
                    "INSERT INTO candidates VALUES (?, ?, ?, 'pending', NULL, NULL, '[]', NULL, 0, '{}', '[]')",
                    (run_id, slot, sample))
            self.db.commit()
        self._fill(run_id, order, first=True)
        return run_id

    def retry(self, run_id, display=None):
        with self._generation:
            with self._lock:
                run = self.run(run_id)
                if _reviewed(run):
                    raise ValueError("This review is already saved")
                keys = [(item["slot"], item["sample"]) for item in run["candidates"] if item["status"] in RETRYABLE]
                if display is not None:
                    if type(display) is not int or not 1 <= display <= 16:
                        raise ValueError("Unknown draft")
                    chosen = tuple(run["shuffle"][display - 1])
                    if chosen not in keys:
                        raise ValueError("That draft cannot be retried")
                    keys = [chosen]
                if not keys:
                    raise ValueError("Nothing to retry")
                self._retrying.add(run_id)
            try:
                self._fill(run_id, keys, first=False)
            finally:
                with self._lock:
                    self._retrying.discard(run_id)

    def _fill(self, run_id, keys, *, first):
        with self._lock:
            run = self._run_row(run_id)
            lineup = json.loads(run["lineup_json"])
            system, user = run["system_text"], run["user_text"]
            existing = {(row["slot"], row["sample"]): dict(row) for row in self._candidate_rows(run_id)}
        pool = ThreadPoolExecutor(max_workers=CONCURRENCY)
        futures = {}
        for slot, sample in keys:
            attempt = 1 if first else existing[(slot, sample)]["attempts"] + 1
            futures[(slot, sample)] = (attempt, pool.submit(self._call, lineup[slot], system, user, sample, attempt))
        finished = {}
        for key, (attempt, future) in futures.items():
            slot = key[0]
            try:
                finished[key] = (attempt, future.result(timeout=self.timeout))
            except TimeoutError:
                finished[key] = (attempt, _failure("timeout") | {
                    "latency_ms": int(self.timeout * 1000), "settings": call_settings(lineup[slot]),
                })
        # ponytail: a timed-out call can outlive its slot; process exit ends that thread.
        pool.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            for (slot, sample), (attempt, result) in finished.items():
                prior = json.loads(existing[(slot, sample)]["prior_json"])
                if attempt > 1:
                    prior.append({
                        "status": existing[(slot, sample)]["status"],
                        "error_code": existing[(slot, sample)]["error_code"],
                    })
                self.db.execute(
                    "UPDATE candidates SET status=?, reason_code=?, error_code=?, messages_json=?, "
                    "latency_ms=?, attempts=?, settings_json=?, prior_json=? "
                    "WHERE run_id=? AND slot=? AND sample=?",
                    (result["status"], result["reason_code"], result["error_code"],
                     json.dumps(result["messages"], ensure_ascii=False), result["latency_ms"], attempt,
                     json.dumps(result["settings"]), json.dumps(prior), run_id, slot, sample))
            self.db.commit()

    def _call(self, spec, system, user, sample, attempt):
        started = time.monotonic()
        if self.trace:
            with self._lock:
                self.trace_log.append({
                    "provider": spec["provider"], "model": spec["model"], "sample": sample,
                    "attempt": attempt, "system": system, "user": user,
                })
        try:
            raw = dispatch(spec, system, user, sample, attempt)
            parsed = classify_reply(raw)
        except ProviderFailure as exc:
            parsed = _failure(exc.code)
        except TimeoutError:
            parsed = _failure("timeout")
        except (AssertionError, KeyboardInterrupt):
            raise
        except Exception:
            parsed = _failure("provider")
        parsed["latency_ms"] = int((time.monotonic() - started) * 1000)
        parsed["settings"] = call_settings(spec) | {"timeout_seconds": self.timeout}
        return parsed

    def pick_model(self, run_id, slot, sample):
        """Save a choice for one model. Return true once all four are reviewed."""
        if type(slot) is not int or slot not in range(4):
            raise ValueError("Unknown model")
        if sample is not None and (type(sample) is not int or sample not in range(4)):
            raise ValueError("Unknown draft")
        with self._lock, self.db:
            run = self.run(run_id)
            if _reviewed(run):
                return True
            if not run["complete"] or run["retrying"]:
                raise ValueError("Wait for the drafts before choosing")
            if sample is not None and not any(
                item["slot"] == slot and item["sample"] == sample and item["status"] == "reply"
                for item in run["candidates"]
            ):
                raise ValueError("That draft is not a reply you can send")
            self.db.execute(
                "INSERT INTO model_picks (run_id, slot, sample) VALUES (?, ?, ?) "
                "ON CONFLICT (run_id, slot) DO UPDATE SET sample = excluded.sample",
                (run_id, slot, sample),
            )
            return len(run["model_picks"].keys() | {slot}) == 4

    def vote(self, run_id, pick, as_is, ratings_used, own_reply):
        run = self.run(run_id)
        if run["model_picks"]:
            raise ValueError("Review each model on the current screen")
        if run["vote"]:
            raise ValueError("The vote is already submitted")
        if any(item["status"] == "pending" for item in run["candidates"]):
            raise ValueError("Retry unfinished drafts before voting")
        own_reply = (own_reply or "").strip()
        if len(own_reply) > 1000:
            raise ValueError("Your reply is too long")
        found = {(item["slot"], item["sample"]): item for item in run["candidates"]}
        slot = sample = None
        if pick == "no_reply":
            choice = "no_reply"
        elif pick == "none":
            choice = "none"
        elif type(pick) is str and pick.startswith("d") and pick[1:].isdigit():
            display = int(pick[1:])
            if not 1 <= display <= 16:
                raise ValueError("Unknown draft")
            slot, sample = run["shuffle"][display - 1]
            if found[(slot, sample)]["status"] != "reply":
                raise ValueError("That draft is not a reply you can send")
            choice = "draft"
        else:
            raise ValueError("Choose a draft, no reply, or none of these")
        marked = []
        if ratings_used:
            for display in as_is:
                if type(display) is not int or not 1 <= display <= 16:
                    continue
                marked_slot, marked_sample = run["shuffle"][display - 1]
                if found[(marked_slot, marked_sample)]["status"] == "reply":
                    marked.append(f"{marked_slot}:{marked_sample}")
        provider = model_id = None
        messages = []
        if choice == "draft":
            spec = run["lineup"][slot]
            provider, model_id = spec["provider"], spec["model"]
            messages = found[(slot, sample)]["messages"]
        with self._lock:
            current = self.run(run_id)
            if current["model_picks"] or current["vote"] or current["retrying"]:
                raise ValueError("This review has already started")
            self.db.execute(
                """INSERT INTO votes
                   (run_id, choice, slot, sample, own_reply, ratings_used, as_is_json, created_at,
                    case_id, provider, model_id, messages_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, choice, slot, sample, own_reply, 1 if ratings_used else 0,
                 json.dumps(sorted(set(marked))), _now(), run["case_id"], provider, model_id,
                 json.dumps(messages, ensure_ascii=False)))
            self.db.commit()

    def continue_run(self, run_id, inbound):
        run = self.run(run_id)
        if not run["vote"]:
            raise ValueError("Vote before continuing")
        lines = [line.strip() for line in inbound.splitlines() if line.strip()]
        if not lines or len(lines) > 8 or any(len(line) > 1000 for line in lines):
            raise ValueError("Add the next inbound bubbles, one short line each")
        vote = run["vote"]
        found = {(item["slot"], item["sample"]): item for item in run["candidates"]}
        if vote["own_reply"]:
            carried = [vote["own_reply"]]
        elif vote["choice"] == "draft":
            carried = list(found[(vote["slot"], vote["sample"])]["messages"])
        elif vote["choice"] == "no_reply":
            carried = []
        else:
            raise ValueError("Write the reply you would send before continuing")
        parent = self.case(run["case_id"])
        bubbles = list(parent["bubbles"])
        for text in carried:
            bubbles.append(_bubble(len(bubbles) + 1, "right", text))
        for text in lines:
            bubbles.append(_bubble(len(bubbles) + 1, "left", text))
        bubbles = [_bubble(index, item["side"], item["text"]) for index, item in enumerate(bubbles, 1)]
        return self.add_case(
            title=f"{parent['title']} continued", contact=parent["contact"],
            language=parent["language"], tone=parent["tone"], silence_ok=False,
            bubbles=bubbles, media={}, rules=parent["rules"], parent_id=parent["id"],
        )

    def run(self, run_id):
        with self._lock:
            row = self._run_row(run_id)
            candidates = [self._candidate(item) for item in self._candidate_rows(run_id)]
            vote_row = self.db.execute("SELECT * FROM votes WHERE run_id = ?", (run_id,)).fetchone()
            picks = dict(self.db.execute("SELECT slot, sample FROM model_picks WHERE run_id = ?", (run_id,)))
            retrying = run_id in self._retrying
        if row is None:
            raise ValueError("Unknown run")
        vote = None
        if vote_row:
            vote = {
                "choice": vote_row["choice"], "slot": vote_row["slot"], "sample": vote_row["sample"],
                "own_reply": vote_row["own_reply"], "ratings_used": bool(vote_row["ratings_used"]),
                "as_is": json.loads(vote_row["as_is_json"]),
                "case_id": vote_row["case_id"], "provider": vote_row["provider"],
                "model_id": vote_row["model_id"],
                "messages": json.loads(vote_row["messages_json"] or "[]"),
            }
        return {
            "id": row["id"], "case_id": row["case_id"], "system": row["system_text"],
            "user": row["user_text"], "digest": row["digest"],
            "lineup": json.loads(row["lineup_json"]), "shuffle": json.loads(row["shuffle_json"]),
            "candidates": candidates, "vote": vote, "model_picks": picks, "retrying": retrying,
            "complete": all(item["status"] != "pending" for item in candidates) and len(candidates) == 16,
        }

    def runs(self):
        with self._lock:
            ids = [row["id"] for row in self.db.execute("SELECT id FROM runs ORDER BY created_at, id")]
        detailed = []
        for run_id in ids:
            run = self.run(run_id)
            case = self.case(run["case_id"])
            run["title"] = case["title"]
            detailed.append(run)
        return detailed

    def results(self):
        return tally(self.runs())

    def _run_row(self, run_id):
        return self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def _candidate_rows(self, run_id):
        return self.db.execute(
            "SELECT * FROM candidates WHERE run_id = ? ORDER BY slot, sample", (run_id,))

    def _candidate(self, row):
        return {
            "slot": row["slot"], "sample": row["sample"], "status": row["status"],
            "reason_code": row["reason_code"], "error_code": row["error_code"],
            "messages": json.loads(row["messages_json"]), "latency_ms": row["latency_ms"],
            "attempts": row["attempts"], "settings": json.loads(row["settings_json"]),
            "prior": json.loads(row["prior_json"]),
        }


def _label(value, limit):
    if type(value) is not str or not value.strip() or len(value.strip()) > limit:
        raise ValueError("Use a short label")
    return value.strip()


PAGE_CSS = """
:root {
  color-scheme: light;
  --page: #f5f5ef; --paper: #fffefa; --ink: #192a25; --muted: #51645b; --line: #d9e0d8;
  --green: #12513e; --green-hover: #0b3b2c; --wash: #e5f1e7; --mist: #edf0e9; --warn: #824420;
  font: 15px/1.5 system-ui, -apple-system, sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink); }
a { color: var(--green); text-underline-offset: .2em; }
button, input, select, textarea { font: inherit; }
button {
  background: var(--paper); color: var(--green); border: 1px solid var(--line); border-radius: .55rem;
  padding: .55rem .8rem; cursor: pointer; font-weight: 650; transition: background .15s, border-color .15s;
}
button:hover { background: var(--wash); border-color: #95b69f; }
button:disabled { cursor: wait; opacity: .7; }
:focus-visible { outline: 2px solid var(--green); outline-offset: 3px; }
.site-bar { background: var(--paper); border-bottom: 1px solid var(--line); }
.nav, main { max-width: 98rem; margin-inline: auto; padding-inline: clamp(1rem, 3vw, 3rem); }
.nav { display: flex; align-items: center; min-height: 3.75rem; gap: .5rem; }
.nav a { text-decoration: none; padding: .4rem .65rem; border-radius: .45rem; font-weight: 650; }
.nav a:hover, .nav a[aria-current] { background: var(--wash); }
.nav .brand { padding-left: 0; margin-right: auto; color: var(--ink); font-weight: 780; letter-spacing: -.035em; }
.brand::before { content: ""; background: var(--green); display: inline-block; width: .65rem;
  height: .65rem; margin-right: .55rem; border-radius: .2rem; transform: rotate(-12deg); }
main { padding-top: clamp(1.25rem, 3vw, 2.75rem); padding-bottom: 3rem; }
h1, h2, h3, p { margin: 0; }
h1 { font-size: clamp(1.85rem, 3vw, 2.8rem); line-height: 1.08; letter-spacing: -.055em; }
h2 { font-size: 1.15rem; letter-spacing: -.025em; }
h3 { font-size: 1rem; }
p + p { margin-top: .45rem; }
.hero { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: .9rem 1.5rem; margin-bottom: 1.1rem; }
.eyebrow { display: block; font-size: .75rem; color: var(--muted); font-weight: 720; margin-bottom: .35rem; }
.subtle { color: var(--muted); }
.hero p { margin-top: .45rem; }
.progress { min-width: 12rem; font-size: .87rem; font-weight: 680; font-variant-numeric: tabular-nums; }
progress { display: block; width: 100%; height: .45rem; margin-top: .4rem; appearance: none; background: var(--line); border: 0; border-radius: 1rem; }
progress::-webkit-progress-bar { background: var(--line); border-radius: 1rem; }
progress::-webkit-progress-value { background: var(--green); border-radius: 1rem; }
progress::-moz-progress-bar { background: var(--green); border-radius: 1rem; }
.conversation, .board-block, form.box { background: var(--paper); border: 1px solid var(--line);
  border-radius: .85rem; padding: 1rem 1.15rem; margin-bottom: 1.15rem; min-width: 0; }
.conversation h2 { margin-bottom: .55rem; }
.inbound-turn { border-inline-start: 3px solid #4d886b; padding: .45rem .85rem; background: var(--wash);
  border-radius: 0 .55rem .55rem 0; max-width: 67ch; overflow-wrap: anywhere; }
.inbound-turn p, .reply-text, .reference p { white-space: pre-wrap; }
.conversation details { margin-top: .6rem; }
summary { cursor: pointer; color: var(--green); font-weight: 650; }
.transcript { margin-top: .6rem; padding: .15rem .25rem; max-height: 23rem; overflow: auto; }
.bubble { width: fit-content; max-width: min(100%, 65ch); margin: 0 0 .45rem; padding: .45rem .7rem;
  border-radius: .55rem; background: var(--mist); overflow-wrap: anywhere; white-space: pre-wrap; }
.bubble.you { background: var(--wash); margin-inline-start: auto; }
.speaker { display: block; color: var(--muted); font-size: .72rem; font-weight: 680; }
.model-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .9rem; align-items: stretch; }
.model-panel { background: var(--paper); border: 1px solid var(--line); border-radius: .85rem;
  overflow: clip; min-width: 0; scroll-margin-block-start: .75rem; display: flex; flex-direction: column; }
.model-panel.reviewed { border-color: #a4c2aa; }
.model-header { background: var(--mist); padding: .8rem .9rem .85rem; border-bottom: 1px solid var(--line); }
.reviewed .model-header { background: var(--wash); }
.model-header h2 { font-size: 1rem; line-height: 1.25; overflow-wrap: anywhere; }
.model-meta { display: flex; align-items: baseline; justify-content: space-between; gap: .45rem;
  color: var(--muted); font-size: .75rem; font-weight: 680; margin-bottom: .25rem; }
.model-meta strong { color: var(--green); font-size: .72rem; }
.model-choices { padding: .65rem; flex: 1; display: flex; flex-direction: column; gap: .45rem; min-width: 0; }
.drafts { display: grid; gap: .45rem; grid-template-rows: repeat(4, minmax(4.4rem, 1fr)); flex: 1; }
.draft { background: var(--paper); min-width: 0; width: 100%; text-align: left; padding: .55rem .65rem;
  border: 1px solid var(--line); border-radius: .5rem; display: flex; flex-direction: column; align-items: start;
  font-weight: 450; color: var(--ink); overflow-wrap: anywhere; }
button.draft:hover { border-color: #78a788; background: #f0f7ef; }
.draft.picked, .none-fit.picked { border-color: #75a184; background: var(--wash); }
.draft.unavailable { background: #f8f7f2; }
.draft-title { display: block; color: var(--muted); font-size: .73rem; font-weight: 680; margin-bottom: .2rem; }
.draft-title span { color: var(--green); margin-left: .3rem; }
.reply-text { display: block; line-height: 1.4; }
.draft-status { color: var(--muted); font-size: .84rem; }
.draft-status.failure, .draft-status.invalid { color: var(--warn); }
.retry { font-size: .73rem; padding: .18rem .45rem; margin-top: .35rem; }
.none-fit { display: block; width: 100%; background: transparent; text-align: center; padding: .42rem; font-size: .82rem; }
p.none-fit { border: 1px solid #75a184; border-radius: .5rem; font-weight: 680; color: var(--green); }
.reference, .run-actions { margin-top: 1rem; }
.reference p { margin-top: .3rem; overflow-wrap: anywhere; }
.run-actions { display: flex; align-items: center; justify-content: space-between; gap: .6rem; flex-wrap: wrap; }
.primary { display: inline-block; padding: .63rem 1rem; background: var(--green); color: white;
  border-color: var(--green); text-decoration: none; border-radius: .55rem; }
.primary:hover { background: var(--green-hover); color: white; }
.board-block h2 { margin-bottom: .45rem; }
.board-block + form.box { margin-top: .85rem; }
.board-block ul { padding-left: 1.2rem; }
.case-list { max-height: 24rem; overflow: auto; }
form.box label { display: block; margin-block: .45rem; }
form.box textarea, form.box input:not([type=checkbox]), form.box select { display: block; width: 100%;
  padding: .4rem .55rem; border: 1px solid #aebbb0; border-radius: .4rem; background: var(--paper); }
form.box fieldset { border: 1px solid var(--line); border-radius: .45rem; margin-bottom: .65rem; }
form.box button { margin-top: .65rem; }
.table-wrap { overflow: auto; margin-top: .65rem; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: .55rem .65rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: .82rem; color: var(--muted); }
@media (max-width: 70rem) { .model-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 36rem) {
  .model-grid { grid-template-columns: minmax(0, 1fr); }
  .hero { align-items: start; }
  .progress { width: 100%; }
}
@media (prefers-reduced-motion: reduce) { button { transition: none; } }
"""


def layout(title, body, *, refresh=False, active="review"):
    meta = "<meta http-equiv='refresh' content='2'>" if refresh else ""
    review = " aria-current='page'" if active == "review" else ""
    results = " aria-current='page'" if active == "results" else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">{meta}
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · Reply Lab</title><style>{PAGE_CSS}</style></head>
<body><header class="site-bar"><nav class="nav" aria-label="Reply Lab">
<a class="brand" href="/">Reply Lab</a><a href="/"{review}>Review</a><a href="/board"{results}>Results</a>
</nav></header><main>{body}</main></body></html>"""


def transcript_html(bubbles):
    inbound = inbound_orders(bubbles)
    history, latest = [], []
    for bubble in bubbles:
        message = _esc(bubble["text"])
        if bubble["order"] in inbound:
            latest.append(f"<p>{message}</p>")
        else:
            speaker = {"right": "You", "left": "Them"}.get(bubble["side"], "Unknown")
            style = " you" if speaker == "You" else ""
            history.append(f"<div class='bubble{style}'><span class='speaker'>{speaker}</span>{message}</div>")
    parts = ["<section class='conversation'><h2>Conversation</h2>"]
    if latest:
        parts.extend(("<div class='inbound-turn'><span class='eyebrow'>Latest inbound</span>", *latest, "</div>"))
    if history:
        parts.extend((f"<details><summary>Earlier messages ({len(history)})</summary>",
                      "<div class='transcript'>", *history, "</div></details>"))
    parts.append("</section>")
    return "".join(parts)


def home_html(lab):
    report = lab.results()
    lineup = lab.lineup()
    cases = []
    for case in lab.cases():
        note = "Blocked from scoring" if case["blocked"] else case["language"] + " · " + case["tone"]
        if case["silence_ok"]:
            note += " · silence can be right"
        cases.append(
            f"<li><a href='/cases/{_esc(case['id'])}'>{_esc(case['title'])}</a> "
            f"({_esc(case['contact'])}, {_esc(note)})</li>")
    rows = []
    for position in range(4):
        spec = lineup[position] if position < len(lineup) else (BASELINE if position == 0 and not lineup else {
            "provider": "openai", "model": "", "base_url": "", "region": ""})
        options = []
        for provider in PROVIDERS:
            selected = " selected" if spec["provider"] == provider else ""
            options.append(f"<option value='{provider}'{selected}>{provider}</option>")
        rows.append(
            f"<fieldset><legend>Model {position + 1}</legend>"
            f"<label>Provider <select name='provider_{position}'>{''.join(options)}</select></label> "
            f"<label>Model <input name='model_{position}' value='{_esc(spec['model'])}' autocomplete='off'></label> "
            f"<label>Base URL <input name='base_url_{position}' value='{_esc(spec.get('base_url') or '')}' autocomplete='off'></label> "
            f"<label>Region <input name='region_{position}' value='{_esc(spec.get('region') or '')}' autocomplete='off'></label>"
            f"</fieldset>")
    body = [
        "<header class='hero'><div><h1>Results</h1>"
        "<p class='subtle'>See how often each model gave you a draft you would send.</p></div>"
        "<a class='primary' href='/'>Open review</a></header>",
        results_html(report),
        f"<details class='board-block'><summary>Saved conversations ({len(cases)})</summary>",
        "<ul class='case-list'>", *cases, "</ul></details>",
        "<details class='board-block'><summary>Paste a transcript</summary>",
        "<form class='box' method='post' action='/cases'>",
        "<label>Title <input name='title' required></label>",
        "<label>Contact <input name='contact'></label>",
        "<label>Language <input name='language' value='en'></label>",
        "<label>Tone <input name='tone' value='casual'></label>",
        "<label><input type='checkbox' name='silence_ok' value='1'> Silence can be the right call</label>",
        "<label>Transcript, one bubble per line <textarea name='transcript' rows='8' required placeholder='them: hey&#10;me: hi&#10;them: you around?'></textarea></label>",
        "<p>Screenshot chats must already be transcribed. The lab does not read images.</p>",
        "<label>Conversation rules, one per line <textarea name='rules' rows='3'></textarea></label>",
        "<label>Reference reply, optional and hidden from models <textarea name='reference' rows='2' autocomplete='off'></textarea></label>",
        "<label>Reference authorship <select name='authorship'><option value=''>None</option>",
        "<option value='owner_written'>owner_written</option><option value='synthetic'>synthetic</option></select></label>",
        "<p><button>Save conversation</button></p></form></details>",
        "<details class='board-block'><summary>Model settings</summary>",
        "<form class='box' method='post' action='/lineup'>", *rows,
        "<p><button>Save four models</button></p></form></details>",
    ]
    return layout("Results", "".join(body), active="results")


def case_html(lab, case_id):
    case = lab.case(case_id)
    open_id = lab.latest_run_id(case_id)
    preparing = lab.is_preparing(case_id) and not open_id
    heading = _esc(case["title"])
    if case["seq"] is not None:
        heading = f"Case {case['seq'] + 1} of {lab.sequence_count()}"
    bits = [f"<header class='hero'><div><h1>{heading}</h1>"
            "<p class='subtle'>Pick the best reply from each of four models.</p></div>"]
    if case["blocked"]:
        bits.append(f"<p>{_esc(case['blocked'])}</p>")
    elif open_id:
        bits.append(f"<a class='primary' href='/runs/{open_id}'>Open drafts</a>")
    elif preparing:
        bits.append("<p role='status'>Generating drafts…</p>")
    elif len(lab.lineup()) != 4:
        bits.append("<a href='/board'>Configure four models</a>")
    else:
        label = "Start" if case["seq"] is not None else "Generate 16 drafts"
        bits.append(
            f"<form method='post' action='/runs'><input type='hidden' name='case_id' value='{_esc(case['id'])}'>"
            f"<button class='primary'>{label}</button></form>")
    bits.extend(("</header>", transcript_html(case["bubbles"])))
    return layout(case["title"], "".join(bits), refresh=preparing)


def _model_panel(run, slot, found, display_numbers, busy):
    spec = run["lineup"][slot]
    run_id = run["id"]
    picks = run["model_picks"]
    legacy = run["vote"]
    choice = picks.get(slot)
    picked_none = slot in picks and choice is None
    can_retry = not _reviewed(run) and not busy
    can_choose = run["complete"] and can_retry
    status = ""
    if slot in picks:
        status = "None fit" if picked_none else "Draft selected"
    elif legacy and legacy["choice"] == "draft" and legacy["slot"] == slot:
        status, choice = "Earlier vote", legacy["sample"]
    reviewed_class = " reviewed" if status else ""
    parts = [
        f"<section class='model-panel{reviewed_class}' id='model-{slot}' data-testid='model-panel-{slot}' "
        f"aria-labelledby='model-name-{slot}'>",
        "<header class='model-header'>",
        f"<div class='model-meta'><span>{_esc(spec['provider'].capitalize())}</span>"
        + (f"<strong>{status}</strong>" if status else "") + "</div>",
        f"<h2 id='model-name-{slot}'>{_esc(spec['model'])}</h2></header>",
        f"<form class='model-choices' method='post' action='/runs/{run_id}/pick'>",
        f"<input type='hidden' name='slot' value='{slot}'><div class='drafts'>",
    ]
    for sample in range(4):
        item = found[(slot, sample)]
        selected = choice == sample
        title = f"<span class='draft-title'>Draft {sample + 1}" + (" <span>Selected</span>" if selected else "") + "</span>"
        test_id = f"data-testid='draft-{slot}-{sample}'"
        if item["status"] == "reply":
            text = "".join(f"<span class='reply-text'>{_esc(message)}</span>" for message in item["messages"])
            style = "draft picked" if selected else "draft"
            if can_choose:
                pressed = "true" if selected else "false"
                parts.append(f"<button class='{style}' {test_id} name='sample' value='{sample}' "
                             f"aria-pressed='{pressed}'>{title}{text}</button>")
            else:
                parts.append(f"<div class='{style}' {test_id}>{title}{text}</div>")
        else:
            label = {
                "skip": "Skipped", "hold": "Held", "pending": "Generating…",
                "invalid": "Invalid output", "failure": "Failed",
            }[item["status"]]
            detail = "" if not item["error_code"] else f" · {_esc(item['error_code'].replace('_', ' '))}"
            retry = ""
            if can_retry and item["status"] in RETRYABLE:
                display = display_numbers[(slot, sample)]
                retry = (f"<button class='retry' formaction='/runs/{run_id}/retry' name='retry' value='d{display}' "
                         f"aria-label='Retry draft {sample + 1}'>Retry</button>")
            parts.append(f"<div class='draft unavailable' {test_id}>{title}"
                         f"<span class='draft-status {item['status']}'><span>{label}</span>{detail}</span>{retry}</div>")
    parts.append("</div>")
    if can_choose:
        style = "none-fit picked" if picked_none else "none-fit"
        pressed = "true" if picked_none else "false"
        parts.append(f"<button class='{style}' name='sample' value='none' aria-pressed='{pressed}'>None fit</button>")
    elif picked_none:
        parts.append("<p class='none-fit picked'>None fit</p>")
    parts.append("</form></section>")
    return "".join(parts)


def run_html(lab, run_id):
    run = lab.run(run_id)
    case = lab.case(run["case_id"])
    reviewed = _reviewed(run)
    busy = run["retrying"] or (not run["complete"] and lab.is_preparing(case["id"]))
    count = len(run["model_picks"])
    found = {(item["slot"], item["sample"]): item for item in run["candidates"]}
    display_numbers = {tuple(pair): number for number, pair in enumerate(run["shuffle"], 1)}
    heading = _esc(case["title"])
    if case["seq"] is not None:
        heading = f"Case {case['seq'] + 1} of {lab.sequence_count()}"
    instruction = "Choices saved." if reviewed else "Pick one draft per model. The fourth choice opens the next case."
    if case["seq"] is None and not reviewed:
        instruction = "Pick the reply you would send from each model."
    bits = [
        f"<header class='hero'><div><h1>{heading}</h1><p class='subtle'>{instruction}</p></div>",
        "<p class='subtle'>Earlier review saved</p></header>" if run["vote"] else
        f"<div class='progress' data-testid='review-progress'>{count} of 4 models reviewed"
        f"<progress value='{count}' max='4' aria-label='Models reviewed'></progress></div></header>",
        transcript_html(case["bubbles"]),
        "<div class='model-grid'>",
        *(_model_panel(run, slot, found, display_numbers, busy) for slot in range(4)),
        "</div>",
    ]
    if reviewed and case["reference"]:
        bits.append(f"<section class='board-block reference'><h2>Archived reply</h2><p>{_esc(case['reference'])}</p></section>")
    if not reviewed and busy:
        label = "Retrying drafts…" if run["retrying"] else "Generating drafts…"
        bits.append(f"<p class='run-actions subtle' role='status'>{label}</p>")
    elif not reviewed:
        failed = sum(item["status"] in RETRYABLE for item in run["candidates"])
        if failed:
            bits.append(f"<div class='run-actions'><p class='subtle'>{failed} drafts need a retry.</p>"
                        f"<form method='post' action='/runs/{run_id}/retry'>"
                        "<button name='retry' value='all'>Retry unfinished drafts</button></form></div>")
    if reviewed and case["seq"] is not None:
        bits.append("<div class='run-actions'><a class='primary' href='/'>Continue review</a></div>")
    if run["vote"]:
        vote = run["vote"]
        carried = vote["own_reply"]
        if not carried and vote["choice"] == "draft":
            carried = " / ".join(found[(vote["slot"], vote["sample"])]["messages"])
        if carried or vote["choice"] == "no_reply":
            bits.append(
                f"<form class='box reference' method='post' action='/runs/{run_id}/continue'>"
                "<label>Next inbound messages<textarea name='inbound' rows='3' required></textarea></label>"
                "<button>Continue</button></form>")
    return layout(case["title"], "".join(bits), refresh=not reviewed and busy)


def results_html(report):
    count = report["reviewed_cases"]
    noun = "case" if count == 1 else "cases"
    body = [f"<section class='board-block'><h2>{count} {noun} reviewed</h2>"]
    if count:
        body.append(
            "<div class='table-wrap'><table><thead><tr><th scope='col'>Model</th>"
            "<th scope='col'>Draft picked</th><th scope='col'>None fit</th>"
            "<th scope='col'>Draft errors</th></tr></thead><tbody>")
        for row in report["models"]:
            if row["drafts_picked"] or row["none_fit"]:
                body.append(
                    f"<tr><th scope='row'>{_esc(row['model'])}</th>"
                    f"<td>{row['drafts_picked']}</td><td>{row['none_fit']}</td>"
                    f"<td>{row['draft_errors']}</td></tr>")
        body.append("</tbody></table></div>")
    else:
        body.append("<p class='subtle'>Review all four models in a case to add it to these results.</p>")
    body.extend(_run_list("Unfinished runs", report["incomplete"]))
    body.extend(_run_list("Ready to review", report["awaiting"]))
    body.append("</section>")
    if report["ranked_turns"]:
        body.append(
            f"<details class='board-block'><summary>Earlier single-vote results ({report['ranked_turns']})</summary>"
            "<div class='table-wrap'><table><thead><tr><th>Model</th><th>Wins</th><th>Ties</th></tr></thead><tbody>")
        for row in report["models"]:
            if row["attempts"]:
                body.append(f"<tr><th scope='row'>{_esc(row['model'])}</th>"
                            f"<td>{row['sole_wins']}</td><td>{row['tie_credits']}</td></tr>")
        body.append("</tbody></table></div></details>")
    return "".join(body)


def _run_list(title, rows):
    if not rows:
        return []
    items = [
        f"<li><a href='/runs/{_esc(row['id'])}'>{_esc(row['title'])}</a> "
        f"{row['terminal']}/{row['total']} terminal, failures {row['failure']}, "
        f"invalid {row['invalid']}, unfinished {row['pending']}</li>" for row in rows]
    return [f"<h3>{title}</h3><ul>", *items, "</ul>"]


def _form(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length < 0 or length > 200_000:
        raise ValueError("Submission is too large")
    raw = handler.rfile.read(length).decode("utf-8", "replace")
    return parse_qs(raw, keep_blank_values=True)


def _one(form, name, default=""):
    values = form.get(name) or [default]
    return values[0]


def make_handler(lab):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._guard(self._get)

        def do_POST(self):
            self._guard(self._post)

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.command} {urlsplit(self.path).path}\n")

        def _guard(self, method):
            try:
                method()
            except ValueError as exc:
                self._html(400, layout("Reply lab", f"<p>{_esc(exc)}</p><p><a href='/'>Back</a></p>"))
            except (AssertionError, KeyboardInterrupt):
                raise
            except Exception as exc:
                sys.stderr.write(f"reply-lab {type(exc).__name__}\n")
                self._html(500, layout("Reply lab", "<p>Something went wrong.</p>"))

        def _get(self):
            path = urlsplit(self.path).path
            if path in ("/", "/board"):
                if path == "/":
                    target = lab.resume_target()
                    if target != "/":
                        self._redirect(target)
                        return
                self._html(200, home_html(lab))
                return
            case_match = re.fullmatch(r"/cases/([a-z0-9-]{1,40})", path)
            if case_match and CASE_ID.fullmatch(case_match.group(1)):
                case_id = case_match.group(1)
                run_id = lab.latest_run_id(case_id) if lab._seq_of(case_id) is not None else None
                if run_id:
                    self._redirect(f"/runs/{run_id}")
                else:
                    self._html(200, case_html(lab, case_id))
                return
            run_match = re.fullmatch(r"/runs/([a-f0-9]{16})", path)
            if run_match:
                self._html(200, run_html(lab, run_match.group(1)))
                return
            self._html(404, layout("Reply lab", "<p>Not found.</p>"))

        def _post(self):
            path = urlsplit(self.path).path
            form = _form(self)
            if path == "/cases":
                bubbles, media = parse_paste(_one(form, "transcript"))
                rules = [line.strip() for line in _one(form, "rules").splitlines() if line.strip()]
                case_id = lab.add_case(
                    title=_one(form, "title"), contact=_one(form, "contact"),
                    language=_one(form, "language"), tone=_one(form, "tone"),
                    silence_ok=_one(form, "silence_ok") == "1", bubbles=bubbles, media=media,
                    rules=rules, reference=_one(form, "reference"), authorship=_one(form, "authorship"))
                self._redirect(f"/cases/{case_id}")
                return
            if path == "/lineup":
                slots = []
                for position in range(4):
                    slots.append({
                        "provider": _one(form, f"provider_{position}"),
                        "model": _one(form, f"model_{position}"),
                        "base_url": _one(form, f"base_url_{position}"),
                        "region": _one(form, f"region_{position}"),
                    })
                lab.set_lineup(slots)
                self._redirect("/")
                return
            if path == "/runs":
                run_id = lab.generate(_one(form, "case_id"))
                self._redirect(f"/runs/{run_id}")
                return
            action = re.fullmatch(r"/runs/([a-f0-9]{16})/(pick|vote|retry|continue)", path)
            if not action:
                self._html(404, layout("Reply lab", "<p>Not found.</p>"))
                return
            run_id, verb = action.group(1), action.group(2)
            if verb == "retry":
                token = _one(form, "retry")
                display = int(token[1:]) if token.startswith("d") and token[1:].isdigit() else None
                lab.retry(run_id, display)
            elif verb == "pick":
                slot, sample = _one(form, "slot"), _one(form, "sample")
                if slot not in ("0", "1", "2", "3") or sample not in ("0", "1", "2", "3", "none"):
                    raise ValueError("Choose one of this model’s drafts")
                finished = lab.pick_model(run_id, int(slot), None if sample == "none" else int(sample))
                self._redirect(lab.after_vote_target(lab.run(run_id)["case_id"]) if finished
                               else f"/runs/{run_id}#model-{slot}")
                return
            elif verb == "vote":
                displays = [int(value) for value in form.get("as_is", []) if value.isdigit()]
                lab.vote(run_id, _one(form, "pick"), displays, _one(form, "rate") == "1", _one(form, "own_reply"))
                self._redirect(lab.after_vote_target(lab.run(run_id)["case_id"]))
                return
            else:
                case_id = lab.continue_run(run_id, _one(form, "inbound"))
                self._redirect(f"/cases/{case_id}")
                return
            self._redirect(f"/runs/{run_id}")

        def _html(self, status, body):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location):
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local reply lab. Never sends a DM.")
    parser.add_argument("--dir", default=str(Path.home() / ".shadow" / "reply-lab"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fake", action="store_true")
    args = parser.parse_args(argv)
    if type(args.port) is not int or not 1 <= args.port <= 65535:
        raise SystemExit(2)
    lab = Lab(args.dir, fake=args.fake)
    if not args.fake:
        if HISTORICAL_CASES.is_file():
            lab.import_archive(HISTORICAL_CASES)
        lab.set_lineup(prepared_lineup())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(lab))
    print(f"Reply lab at http://127.0.0.1:{args.port}", flush=True)
    if lab.sequence_count():
        print(f"Historical cases: {lab.sequence_count()}. Click Start on the first case.", flush=True)
    print("Local only. No DM send, no outbox, no production database.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        lab.close()


if __name__ == "__main__":
    main()
