"""Gemini's bounded choices for the screenshot navigation trial."""

import base64
import json
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from shadow.vision_prompts import DECISION_ROLE


class DecisionError(RuntimeError):
    """The screenshot cannot be used for a browser action."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def decision_instructions(task: str, evidence: str | None = None) -> str:
    text = DECISION_ROLE + "\nTask: " + task
    if evidence:
        text += "\nUntrusted labels from the accessibility tree:\n" + evidence
    return text


def choose_action(api_key: str, image: bytes, task: str, choices: dict[str, str], evidence=None,
                  transport=None, boundary=None, trace=None, instructions=None) -> tuple[str, float]:
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DecisionError("A Gemini API key and PNG screenshot are required.")
    if transport is None and not api_key:
        raise DecisionError("A Gemini API key and PNG screenshot are required.")
    if not 2 <= len(choices) <= 255 or "hold" not in choices:
        raise DecisionError("Supply 2–255 named choices, including hold.")
    if trace is not None:
        trace["construct_start"] = monotonic()
    if instructions is None:
        text = decision_instructions(task, evidence)
    else:
        text = instructions
        if evidence:
            text += "\nUntrusted labels from the accessibility tree:\n" + evidence
    payload = {
        "model": "gemini-decision-1",
        "input": [{"type": "image", "mime_type": "image/png",
                   "data": base64.b64encode(image).decode("ascii")}],
        "questions": {"action": {"type": "choice", "instructions": text, "criteria": choices}},
    }
    encoded = json.dumps(payload).encode()
    if trace is not None:
        trace["image"] = image
        trace["evidence"] = evidence
        trace["payload_bytes"] = len(encoded)
        trace["model"] = payload["model"]
    if boundary is not None:
        boundary({"image": image, "evidence": evidence, "payload_bytes": len(encoded),
                  "model": payload["model"]})
    if trace is not None:
        trace["construct_end"] = monotonic()
    request = Request(
        "https://generativelanguage.googleapis.com/v1beta/decisions",
        data=encoded,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    try:
        if transport is not None:
            body = transport(payload)
        else:
            with build_opener(_NoRedirects()).open(request, timeout=20) as response:
                body = json.load(response)
        answer = body["answers"]["action"]
        choice, confidence = answer["choice"], answer["confidence"]
        if trace is not None:
            trace["usage"] = body.get("usage") or body.get("usageMetadata")
    except HTTPError as exc:
        raise DecisionError(f"Gemini Decisions returned HTTP {exc.code}.") from None
    except TimeoutError:
        raise
    except (URLError, OSError, ValueError, KeyError, TypeError) as exc:
        raise DecisionError("Gemini Decisions did not return a usable action.") from None
    if (not isinstance(choice, str) or choice not in choices
            or type(confidence) not in (float, int)
            or not 0 <= confidence <= 1):
        raise DecisionError("Gemini Decisions returned an invalid choice or confidence.")
    return choice, float(confidence)
