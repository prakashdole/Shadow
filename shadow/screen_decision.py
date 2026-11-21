"""Gemini's bounded choices for the screenshot navigation trial."""

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class DecisionError(RuntimeError):
    """The screenshot cannot be used for a browser action."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def choose_action(api_key: str, image: bytes, task: str, choices: dict[str, str]) -> tuple[str, float]:
    if not api_key or not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DecisionError("A Gemini API key and PNG screenshot are required.")
    if not 2 <= len(choices) <= 255 or "hold" not in choices:
        raise DecisionError("Supply 2–255 named choices, including hold.")
    payload = {
        "model": "gemini-decision-1",
        "input": [{"type": "image", "mime_type": "image/png",
                   "data": base64.b64encode(image).decode("ascii")}],
        "questions": {"action": {"type": "choice", "instructions": task, "criteria": choices}},
    }
    request = Request(
        "https://generativelanguage.googleapis.com/v1beta/decisions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    try:
        with build_opener(_NoRedirects()).open(request, timeout=20) as response:
            answer = json.load(response)["answers"]["action"]
            choice, confidence = answer["choice"], answer["confidence"]
    except HTTPError as exc:
        raise DecisionError(f"Gemini Decisions returned HTTP {exc.code}.") from None
    except (URLError, OSError, ValueError, KeyError, TypeError) as exc:
        raise DecisionError("Gemini Decisions did not return a usable action.") from None
    if (not isinstance(choice, str) or choice not in choices
            or type(confidence) not in (float, int)
            or not 0 <= confidence <= 1):
        raise DecisionError("Gemini Decisions returned an invalid choice or confidence.")
    return choice, float(confidence)
