"""Freeze the Phase 1 navigation cases. Re-running rewrites the files; do not retune expects."""

from __future__ import annotations

import json
from pathlib import Path


NAMES = ["Nia", "Omar", "Pia", "Quin", "Rae", "Sol", "Tao", "Uma", "Val", "Wes",
         "Xia", "Yara", "Zed", "Anu", "Bea", "Cal", "Dee", "Eli", "Fay", "Gus"]
KNOWN = ["H017", "H018", "H019", "H020", "H021", "H022", "H041", "H042", "H043", "H044"]
FROZEN = {
    "model": "gemini-decision-1",
    "viewport": "1280x720@1",
    "min_confidence": 0.8,
    "agent_browser": "0.45.0",
    "playwright": "excluded",
    "provider_call_budget": 640,
    "phase1_cases": 60,
    "reserve_cases": 60,
    "required_success": "ceil(95% of the actual denominator); 60 -> 57, 40 -> 38, 20 -> 19",
    "scoring": (
        "Eventual success requires the expected outcome and, for an acted case, the expected "
        "opened ids in order and the expected state. close-photo and ids starting with photo "
        "may appear as one recovery and do not by themselves fail an acted case. Any other extra "
        "id fails. A numeric thread id that was not expected is a wrong thread and fails. "
        "A hold or wait succeeds only when nothing was opened. First-attempt success also requires "
        "the exact opened list, zero wrong clicks, and no second observation. "
        "Expected answers are not read by the navigation code."
    ),
}
ROOT = Path(__file__).resolve().parent


def name(index, shift):
    return NAMES[(index + shift) % len(NAMES)]


def row(thread, label, *, time="", dot=False, aria=None):
    body = {"id": str(thread), "name": label, "dot": dot}
    if time:
        body["time"] = time
    if aria is not None:
        body["aria"] = aria
    return body


def inbox(rows, *, order="", messages=None, path="/direct/inbox/", account="pilot"):
    spec = {"screen": "inbox", "path": path, "account": account,
            "account_label": f"Account {account}", "links": True, "rows": rows}
    if order:
        spec["order_label"] = order
    if messages is not None:
        spec["messages"] = messages
    return spec


def thread(thread_id, messages):
    return {"screen": "thread", "path": f"/direct/t/{thread_id}/", "account": "pilot",
            "account_label": "Account pilot", "thread": str(thread_id), "state": str(thread_id),
            "messages": messages}


def opened():
    return [{"side": "in", "kind": "text", "text": "Opened thread"}]


def journey_messages():
    return [{"side": "in", "kind": "text", "text": "Earlier today"},
            {"side": "in", "kind": "reel", "id": "reel", "aria": "Shared reel", "caption": "Latest clip"}]


def acted(thread_id):
    return {"outcome": "acted", "opened": [str(thread_id)], "state": str(thread_id)}


def held():
    return {"outcome": "hold", "opened": []}


def build(split, base, shift):
    cases = []
    cursor = base

    def take():
        nonlocal cursor
        cursor += 1
        return cursor

    def add(family, kind, screen, expect, *, task="row", **extra):
        cases.append({"id": f"{split[0].upper()}{len(cases) + 1:03d}", "split": split, "family": family,
                      "kind": kind, "task": task, "screen": screen, "expect": expect, **extra})

    for index in range(4):
        older, newer = take(), take()
        rows = [row(older, name(index, shift), time="5h", dot=True),
                row(newer, name(index + 1, shift), time="1h", dot=True)]
        add("older_above", "act", inbox(rows, order="Newest at top", messages=opened()), acted(older))
    for index in range(4):
        newer, older = take(), take()
        rows = [row(newer, name(index, shift), time="1h", dot=True),
                row(older, name(index + 1, shift), time="6h", dot=True)]
        add("older_below", "act", inbox(rows, order="Newest at top", messages=opened()), acted(older))
    for index in range(2):
        top, low = take(), take()
        rows = [row(top, name(index, shift), time="2h", dot=True),
                row(low, name(index + 2, shift), time="2h", dot=True)]
        add("equal_age", "act", inbox(rows, order="Newest at top", messages=opened()), acted(low))
    for index in range(2):
        top, low = take(), take()
        rows = [row(top, name(index, shift), dot=True), row(low, name(index + 2, shift), dot=True)]
        add("order_only", "act", inbox(rows, order="Newest at top", messages=opened()), acted(low))
    for index in range(2):
        unread, read = take(), take()
        rows = [row(read, name(index, shift), time="4h", dot=False),
                row(unread, name(index + 1, shift), time="1h", dot=True)]
        add("single_unread", "act", inbox(rows, order="Newest at top", messages=opened()), acted(unread))
    for index in range(2):
        read, unread = take(), take()
        label = name(index + 4, shift)
        rows = [row(read, name(index, shift), time="3h", dot=False),
                row(unread, label, time="2h", dot=False, aria=f"{label} 2h unread")]
        add("aria_unread", "act", inbox(rows, order="Newest at top", messages=opened()), acted(unread))
    older, newer = take(), take()
    add("duplicate", "act", inbox([
        row(older, "Sam", time="6h", dot=True), row(newer, "Sam", time="1h", dot=True)],
        order="Newest at top", messages=opened()), acted(older))
    newer, older = take(), take()
    add("duplicate", "act", inbox([
        row(newer, "Sam", time="1h", dot=True), row(older, "Sam", time="6h", dot=True)],
        order="Newest at top", messages=opened()), acted(older))
    top, low = take(), take()
    add("race", "act", inbox([
        row(top, "Sam", time="2h", dot=True), row(low, "Sam", time="2h", dot=True)],
        order="Newest at top", messages=opened()), acted(low), mutate="reorder")
    older, newer = take(), take()
    add("race", "act", inbox([
        row(older, "Sam", time="5h", dot=True), row(newer, "Sam", time="1h", dot=True)],
        order="Newest at top", messages=opened()), acted(older), mutate="reorder")
    for index in range(20):
        target, other = take(), take()
        label = name(index, shift)
        add("journey", "journey", inbox([
            row(target, label, time="1h", dot=True, aria=f"{label} 1h unread"),
            row(other, name(index + 5, shift), time="5h", dot=False)],
            order="Newest at top", messages=journey_messages()),
            {"outcome": "acted", "opened": [str(target), "reel", "close"], "state": str(target)},
            task="journey", journey=["row", "reel", "close"])
    for _index in range(4):
        rows = [row(take(), name(_index, shift), dot=True), row(take(), name(_index + 1, shift), dot=True)]
        add("unknown_order", "hold", inbox(rows, messages=opened()), held())
    for _index in range(2):
        add("older_reel", "hold", thread(take(), [
            {"side": "in", "kind": "reel", "id": "reel-old", "aria": "Shared reel", "caption": "Yesterday"},
            {"side": "in", "kind": "text", "text": "This morning"}]), held(), task="reel")
    for _index in range(2):
        add("outgoing_reel", "hold", thread(take(), [
            {"side": "out", "kind": "reel", "id": "reel-out", "aria": "Shared reel", "caption": "Sent clip"}]),
            held(), task="reel")
    for _index in range(2):
        add("photo", "hold", thread(take(), [
            {"side": "in", "kind": "photo", "id": "photo", "aria": "Photo", "caption": "A picture"}]),
            held(), task="reel")
    for _index in range(2):
        add("link", "hold", thread(take(), [
            {"side": "in", "kind": "link", "id": "link", "aria": "Link", "text": "A website"}]),
            held(), task="reel")
    for _index in range(2):
        add("two_reels", "hold", thread(take(), [
            {"side": "in", "kind": "reel", "id": "reel-a", "aria": "Shared reel", "caption": "A clip"},
            {"side": "in", "kind": "reel", "id": "reel-b", "aria": "Shared reel", "caption": "Another clip"}]),
            held(), task="reel")
    for _index in range(2):
        add("insufficient", "hold", thread(take(), [
            {"side": "in", "kind": "attachment", "id": "attachment", "aria": "Attachment"}]),
            held(), task="reel")
    for _index in range(2):
        target = take()
        add("stale", "hold", inbox([row(target, name(_index, shift), time="1h", dot=True)],
                                   order="Newest at top", messages=opened()), held(), mutate="replace")
    add("wrong_tab", "hold", inbox([row(take(), "Nia", time="1h", dot=True)], path="/explore/"),
        held(), bound_path="/direct/inbox/")
    add("wrong_thread", "hold", thread(take(), journey_messages()), held(), task="reel", bound_thread="1001")
    return cases


def dev_cases():
    screen = inbox([row(41001, "Dev", time="5h", dot=True), row(41002, "Other", time="1h", dot=True)],
                   order="Newest at top", messages=opened())
    reel = thread(41011, journey_messages())
    trap = thread(41021, [
        {"side": "in", "kind": "photo", "id": "photo", "aria": "Photo", "caption": "Not a reel"},
        {"side": "in", "kind": "reel", "id": "reel", "aria": "Shared reel", "caption": "Latest clip"}])
    return [
        {"id": "D001", "split": "dev", "family": "older_above", "kind": "act", "task": "row",
         "screen": screen, "expect": acted(41001)},
        {"id": "D002", "split": "dev", "family": "unknown_order", "kind": "hold", "task": "row",
         "screen": inbox([row(41031, "Amy", dot=True), row(41032, "Bea", dot=True)]),
         "expect": held()},
        {"id": "D003", "split": "dev", "family": "journey", "kind": "journey", "task": "journey",
         "screen": inbox([row(41041, "Joy", time="1h", dot=True, aria="Joy 1h unread"),
                          row(41042, "Kim", time="4h")],
                         order="Newest at top", messages=journey_messages()),
         "expect": {"outcome": "acted", "opened": ["41041", "reel", "close"], "state": "41041"},
         "journey": ["row", "reel", "close"]},
        {"id": "D004", "split": "dev", "family": "reel", "kind": "act", "task": "reel",
         "screen": reel, "expect": {"outcome": "acted", "opened": ["reel"], "state": "reel"},
         "inject": "low", "procedures": ["phase1", "phase2"]},
        {"id": "D005", "split": "dev", "family": "recovery", "kind": "act", "task": "reel",
         "screen": trap, "expect": {"outcome": "acted", "opened": ["reel"], "state": "reel"},
         "inject": "photo_first", "procedures": ["phase1", "phase2"]},
        {"id": "D006", "split": "dev", "family": "hooks", "kind": "act", "task": "row",
         "screen": screen, "expect": acted(41001), "strip_hooks": True},
        {"id": "D007", "split": "dev", "family": "delay", "kind": "act", "task": "row",
         "screen": {**screen, "delay": 700}, "expect": acted(41001)},
        {"id": "D008", "split": "dev", "family": "stale", "kind": "hold", "task": "row",
         "screen": inbox([row(41051, "Stu", time="1h", dot=True)], order="Newest at top", messages=opened()),
         "expect": held(), "mutate": "replace"},
    ]


def main():
    phase = build("phase1", 11000, 0)
    reserve = build("reserve", 31000, 7)
    assert len(phase) == 60 and len(reserve) == 60
    assert sum(case["kind"] in ("act", "journey") for case in phase) == 40
    assert sum(case["kind"] == "journey" for case in phase) == 20
    assert sum(case["kind"] == "hold" for case in phase) == 20
    phase_ids = {case["id"] for case in phase}
    reserve_ids = {case["id"] for case in reserve}
    assert phase_ids.isdisjoint(reserve_ids)
    assert set(KNOWN).isdisjoint({case["id"] for case in phase + reserve})
    (ROOT / "nav_phase_frozen.json").write_text(json.dumps({"frozen": FROZEN, "cases": phase + reserve}, indent=2) + "\n")
    (ROOT / "nav_phase_dev.json").write_text(json.dumps(
        {"frozen": FROZEN, "known_sources": KNOWN, "cases": dev_cases()}, indent=2) + "\n")
    print(len(phase), "phase1", len(reserve), "reserve", len(dev_cases()), "dev")


if __name__ == "__main__":
    main()
