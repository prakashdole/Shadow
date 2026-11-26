"""Freeze the navigation splits. Re-running this rewrites the files; do not retune them."""

from __future__ import annotations

import json
from pathlib import Path


NAMES = ["Asha", "Bela", "Chet", "Diya", "Evan", "Fay", "Gita", "Hari", "Isha", "Jay",
         "Kala", "Lila", "Maya", "Neil", "Omar", "Priya", "Quinn", "Ravi", "Sia", "Tara"]
FROZEN = {
    "model": "gemini-decision-1",
    "viewport": "1280x720@1",
    "min_confidence": 0.8,
    "agent_browser": "0.45.0",
    "provider_call_budget": 280,
    "timing_samples": 100,
    "latency_gate": "C warm p95 outside Gemini <= A p95 + max(75ms, 15% of A p95)",
    "promotion": "5 percentage points verified task success, or 20% lower p95 outside Gemini, or 20% fewer billed input tokens per success",
}
ROOT = Path(__file__).resolve().parent


def row(thread, name, *, dot=False, aria=None):
    return {"id": str(thread), "name": name, "aria": aria if aria is not None else name, "dot": dot}


def add(cases, family, kind, screen, expect, *, task, live=False, **extra):
    cases.append({"id": f"H{len(cases) + 1:03d}", "split": "heldout", "family": family, "kind": kind,
                  "task": task, "screen": screen, "expect": expect, "live": live,
                  "reserve": (len(cases) + 1) % 6 == 0, "inject": "confirm", **extra})


def build():
    cases = []
    for index in range(16):
        count = 2 + (index % 2)
        pick = index % count
        rows = [row(4100 + index * 10 + slot, NAMES[(index + slot) % len(NAMES)], dot=slot == pick)
                for slot in range(count)]
        add(cases, "visual", "act", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot", "rows": rows},
            {"outcome": "acted", "opened": [rows[pick]["id"]], "state": rows[pick]["id"]},
            task="row", live=index < 12)
    for index in range(8):
        name = NAMES[index]
        rows = [row(5100 + index * 10, name, dot=True), row(5100 + index * 10 + 1, NAMES[index + 1], dot=True)]
        add(cases, "lowest", "act", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot", "rows": rows},
            {"outcome": "acted", "opened": [rows[1]["id"]], "state": rows[1]["id"]},
            task="row", live=index < 6)
    for index in range(8):
        name = NAMES[index]
        rows = [row(5200 + index * 10, name, dot=False), row(5200 + index * 10 + 1, name, dot=True)]
        add(cases, "duplicate", "act", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot", "rows": rows},
            {"outcome": "acted", "opened": [rows[1]["id"]], "state": rows[1]["id"]},
            task="row", live=index < 6)
    for index in range(8):
        rows = [row(5300 + index * 10, NAMES[index], dot=False),
                row(5300 + index * 10 + 1, NAMES[index + 1], dot=False, aria=NAMES[index + 1] + " unread")]
        add(cases, "aria", "act", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot", "rows": rows},
            {"outcome": "acted", "opened": [rows[1]["id"]], "state": rows[1]["id"]},
            task="row", live=True)
    for index in range(8):
        thread = str(5400 + index)
        add(cases, "reel", "act", {"screen": "thread", "path": f"/direct/t/{thread}/", "account": "pilot",
                                   "thread": thread, "state": thread},
            {"outcome": "acted", "opened": ["reel"], "state": "reel"}, task="reel", live=index < 4)
    for index in range(8):
        thread = str(5500 + index)
        add(cases, "close", "act", {"screen": "player", "path": f"/direct/t/{thread}/", "account": "pilot",
                                    "thread": thread, "state": "reel"},
            {"outcome": "acted", "opened": ["close"], "state": thread}, task="close", live=index < 4)
    for index in range(10):
        thread = str(5600 + index)
        rows = [row(thread, NAMES[index], dot=True), row(str(5700 + index), NAMES[index + 1])]
        add(cases, "journey", "journey", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot", "rows": rows},
            {"outcome": "acted", "opened": [thread, "reel", "close"], "state": thread},
            task="journey", journey=["row", "reel", "close"], live=index < 4)
    for index in range(6):
        rows = [row(5800 + index * 10, "Sam", dot=True), row(5800 + index * 10 + 1, "Sam", dot=True)]
        add(cases, "race", "race", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot", "rows": rows},
            {"any": [{"outcome": "acted", "opened": [rows[1]["id"]], "state": rows[1]["id"]},
                     {"outcome": "hold", "opened": []}]},
            task="row", mutate="reorder", live=index < 4)
    for index in range(8):
        rows = [row(5900 + index * 10 + slot, "", aria="Conversation") for slot in range(2)]
        add(cases, "missing", "hold", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot", "rows": rows},
            {"outcome": "hold", "opened": []}, task="row", live=index < 4)
    for index in range(6):
        thread = str(6000 + index)
        add(cases, "loading", "wait", {"screen": "loading", "path": f"/direct/t/{thread}/", "account": "pilot",
                                       "thread": thread},
            {"outcome": "wait", "opened": []}, task="row", live=index < 4)
    for index in range(4):
        thread = str(6100 + index)
        add(cases, "covered", "wait", {"screen": "player", "path": f"/direct/t/{thread}/", "account": "pilot",
                                       "thread": thread, "state": "reel"},
            {"outcome": "wait", "opened": []}, task="row")
    for index in range(3):
        add(cases, "wrong_tab", "hold", {"screen": "inbox", "path": "/explore/", "account": "pilot",
                                         "rows": [row(6200 + index, NAMES[index], dot=True)]},
            {"outcome": "hold", "opened": []}, task="row", bound_path="/direct/inbox/", live=index < 2)
    add(cases, "account", "hold", {"screen": "inbox", "path": "/direct/inbox/", "account": "other",
                                   "rows": [row(6290, "Uma", dot=True)]},
        {"outcome": "hold", "opened": []}, task="row", bound_account="pilot")
    for index in range(4):
        thread = str(6300 + index)
        add(cases, "wrong_thread", "hold", {"screen": "thread", "path": f"/direct/t/{thread}/", "account": "pilot",
                                            "thread": thread, "state": thread},
            {"outcome": "hold", "opened": []}, task="reel", bound_thread="1001", live=index < 2)
    for index in range(4):
        rows = [row(6400 + index, NAMES[index], dot=True)]
        add(cases, "stale", "hold", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot", "rows": rows},
            {"outcome": "hold", "opened": []}, task="row", mutate="replace")
    for index in range(2):
        add(cases, "detach", "hold", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot",
                                      "rows": [row(6500 + index, "Detached", dot=True)]},
            {"outcome": "hold", "opened": []}, task="row", detach=True)
    for index in range(2):
        add(cases, "timeout", "hold", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot",
                                       "rows": [row(6600 + index, NAMES[index], dot=True)]},
            {"outcome": "hold", "opened": []}, task="row", inject="timeout")
    for index in range(2):
        add(cases, "rate", "pending", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot",
                                       "rows": [row(6700 + index, NAMES[index], dot=True)]},
            {"outcome": "pending", "opened": []}, task="row", inject="rate")
    for index in range(4):
        add(cases, "malformed", "pending", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot",
                                            "rows": [row(6800 + index, NAMES[index], dot=True)]},
            {"outcome": "pending", "opened": []}, task="row", inject="malformed")
    for index in range(8):
        rows = [row(6900 + index * 10, NAMES[index], dot=True), row(6900 + index * 10 + 1, NAMES[index + 1], dot=True)]
        add(cases, "wrong_choice", "hold", {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot", "rows": rows},
            {"outcome": "hold", "opened": []}, task="row", inject="wrong")
    return cases


def dev_cases():
    visual = {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot",
              "rows": [row(8001, "DevA", dot=True), row(8002, "DevB")]}
    aria = {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot",
            "rows": [row(8011, "DevA"), row(8012, "DevB", aria="DevB unread")]}
    race = {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot",
            "rows": [row(8021, "Sam", dot=True), row(8022, "Sam", dot=True)]}
    journey = {"screen": "inbox", "path": "/direct/inbox/", "account": "pilot",
               "rows": [row(8031, "DevJ", dot=True), row(8032, "Other")]}
    loading = {"screen": "loading", "path": "/direct/t/8041/", "account": "pilot", "thread": "8041"}
    return [
        {"id": "D001", "split": "dev", "family": "visual", "kind": "act", "task": "row", "screen": visual,
         "expect": {"outcome": "acted", "opened": ["8001"], "state": "8001"}, "inject": "confirm"},
        {"id": "D002", "split": "dev", "family": "aria", "kind": "act", "task": "row", "screen": aria,
         "expect": {"outcome": "acted", "opened": ["8012"], "state": "8012"}, "inject": "confirm"},
        {"id": "D003", "split": "dev", "family": "race", "kind": "race", "task": "row", "screen": race,
         "expect": {"any": [{"outcome": "acted", "opened": ["8022"], "state": "8022"},
                            {"outcome": "hold", "opened": []}]}, "inject": "confirm", "mutate": "reorder"},
        {"id": "D004", "split": "dev", "family": "journey", "kind": "journey", "task": "journey", "screen": journey,
         "expect": {"outcome": "acted", "opened": ["8031", "reel", "close"], "state": "8031"},
         "inject": "confirm", "journey": ["row", "reel", "close"]},
        {"id": "D005", "split": "dev", "family": "loading", "kind": "wait", "task": "row", "screen": loading,
         "expect": {"outcome": "wait", "opened": []}, "inject": "confirm"},
        {"id": "D006", "split": "dev", "family": "wrong_choice", "kind": "hold", "task": "row", "screen": race,
         "expect": {"outcome": "hold", "opened": []}, "inject": "wrong"},
    ]


def main():
    cases = build()
    payload = {"frozen": FROZEN, "cases": cases}
    (ROOT / "nav_heldout.json").write_text(json.dumps(payload, indent=2) + "\n")
    (ROOT / "nav_dev.json").write_text(json.dumps({"frozen": FROZEN, "cases": dev_cases()}, indent=2) + "\n")
    kinds = {}
    for case in cases:
        kinds[case["kind"]] = kinds.get(case["kind"], 0) + 1
    print(len(cases), kinds, "live", sum(case["live"] for case in cases),
          "reserve", sum(case["reserve"] for case in cases))


if __name__ == "__main__":
    main()
