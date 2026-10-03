"""Causal evaluation inputs and count-backed reports; never a launch certificate."""

from __future__ import annotations

import hashlib
import json
import math
import random

from shadow.contracts import ContextEvent, DecisionInput, ReplyDraft, RiskAssessment


def wilson(successes: int, total: int):
    if type(successes) is not int or type(total) is not int or not 0 <= successes <= total:
        raise ValueError("Invalid sample counts")
    if not total:
        return {"successes": 0, "total": 0, "rate": None, "interval95": None}
    z, rate = 1.959963984540054, successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return {"successes": successes, "total": total, "rate": rate,
            "interval95": [max(0, center - radius), min(1, center + radius)]}


def validate_dataset(cases):
    """Episode splits and reply-time cutoffs cover every supplied retrieval surface."""
    if type(cases) is not list or not cases:
        raise ValueError("A non-empty case list is required")
    identifiers, episodes = set(), {}
    for case in cases:
        required = {"id", "episode_id", "split", "contact_id", "cutoff", "expected",
                    "category", "authorship", "events", "current_ids"}
        if type(case) is not dict or not required <= set(case):
            raise ValueError("Missing evaluation case fields")
        if any(type(case[key]) is not str or not case[key] for key in
               ("id", "episode_id", "split", "category", "authorship")):
            raise ValueError("Invalid case identity")
        if case["id"] in identifiers:
            raise ValueError("Duplicate case ID")
        identifiers.add(case["id"])
        if case["split"] not in ("train", "dev", "test") or case["expected"] not in ("reply", "skip", "hold", "urgent"):
            raise ValueError("Invalid split or expected action")
        episode = case["episode_id"]
        if episode in episodes and episodes[episode] != case["split"]:
            raise ValueError("Episode crosses causal partitions")
        episodes[episode] = case["split"]
        if type(case["cutoff"]) is not int or type(case["contact_id"]) is not int:
            raise ValueError("Invalid cutoff/contact identity")
        if type(case["events"]) is not list or type(case["current_ids"]) is not list:
            raise ValueError("Invalid events/current IDs")
        known = {}
        previous = -1
        for event in case["events"]:
            if (type(event) is not dict or not {"id", "time", "sender", "kind", "text"} <= set(event)
                    or type(event["id"]) is not int or type(event["time"]) is not int
                    or event["id"] in known or event["time"] < previous or event["time"] > case["cutoff"]
                    or event["sender"] not in ("owner", "contact", "unknown")
                    or event["kind"] not in ("text", "media", "reaction", "system", "unknown")
                    or type(event["text"]) is not str):
                raise ValueError("Invalid, future or unordered source event")
            known[event["id"]] = event
            previous = event["time"]
        if not case["current_ids"] or any(type(item) is not int or item not in known for item in case["current_ids"]):
            raise ValueError("Current batch does not resolve to source events")
        if (len(set(case["current_ids"])) != len(case["current_ids"])
                or any(known[item]["sender"] != "contact" for item in case["current_ids"])):
            raise ValueError("Current batch must contain distinct contact events")
        for surface in ("facts", "examples", "summaries"):
            if type(case.get(surface, [])) is not list:
                raise ValueError("Invalid retrieval surface")
            for record in case.get(surface, []):
                if (type(record) is not dict or record.get("contact_id") != case["contact_id"]
                        or type(record.get("time")) is not int or record["time"] > case["cutoff"]
                        or record.get("split") != "train"
                        or record.get("episode_id") == episode and case["split"] != "train"
                        or type(record.get("source_ids")) is not list or not record["source_ids"]
                        or any(type(source) is not int or source not in known for source in record["source_ids"])):
                    raise ValueError("Retrieval surface leaks split, time or contact")
        if case["authorship"] not in ("owner_written", "model_drafted", "synthetic"):
            raise ValueError("Unknown style provenance")
    return hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def decision_input(case):
    current = set(case["current_ids"])
    events = [ContextEvent(event["id"], event["sender"], event["kind"], event["text"])
              for event in case["events"]]
    return DecisionInput(case["contact_id"], case.get("contact_status", "known"),
                         case.get("relationship", {}), case.get("owner_profile", {}),
                         tuple(event for event in events if event.event_id in current),
                         tuple(event for event in events if event.event_id not in current),
                         corrections=tuple(case.get("corrections", [])),
                         facts=tuple(case.get("facts", [])), examples=tuple(case.get("examples", [])))


async def evaluate(cases, decisions, *, split="dev", repeats=1):
    digest = validate_dataset(cases)
    if split not in ("dev", "test") or type(repeats) is not int or not 1 <= repeats <= 3:
        raise ValueError("Evaluation split or repeats invalid")
    results = []
    for repeat in range(repeats):
        for case in cases:
            if case["split"] != split:
                continue
            data = decision_input(case)
            risk = await decisions.assess(data)
            actual, failure = "hold", risk.failure
            messages = []
            if isinstance(risk.value, RiskAssessment) and not risk.failure:
                assessment = risk.value
                actual = "urgent" if assessment.action_hint == "urgent" else "hold"
                if assessment.action_hint == "proceed" and not assessment.signals:
                    reply = await decisions.draft(data, assessment)
                    failure = reply.failure
                    if isinstance(reply.value, ReplyDraft) and not reply.failure:
                        actual = reply.value.action
                        messages = list(reply.value.messages)
            results.append({"id": case["id"], "repeat": repeat, "expected": case["expected"],
                            "actual": actual, "category": case["category"], "failure": failure,
                            "messages": messages})
    total = len(results)
    ordinary = [result for result in results if result["expected"] == "reply"]
    urgent = [result for result in results if result["expected"] == "urgent"]
    failed = [result for result in results if result["actual"] != result["expected"] or result["failure"]]
    return {
        "dataset_digest": digest, "split": split, "repeats": repeats,
        "decisions": wilson(total - len(failed), total),
        "unnecessary_holds": wilson(sum(result["actual"] in ("hold", "urgent") for result in ordinary), len(ordinary)),
        "urgent_detected": wilson(sum(result["actual"] == "urgent" and not result["failure"] for result in urgent), len(urgent)),
        "failures": failed, "results": results,
        "owner_written_style_cases": sum(case["authorship"] == "owner_written" for case in cases if case["split"] == split),
        "qualification": "pending_external",
        "limitations": ["Owner ratings, chosen-model qualification and controlled live acceptance are separate gates",
                        "An action matching through provider failure is not a successful case"],
    }


RATING_DIMENSIONS = ("relevance", "natural_language", "relationship_fit", "humour_seriousness",
                     "grounding", "non_repetition")


def blind_pairs(pairs, *, seed):
    """Return owner-facing anonymous pairs and a separate private answer key.

    The caller stores both privately. The key must never be shown to the rater.
    """
    rng = random.Random(seed)
    rows, key, seen = [], [], set()
    for pair in pairs:
        if type(pair) is not dict or set(pair) != {"id", "context", "personalized", "baseline"}:
            raise ValueError("Invalid blind pair")
        if any(type(value) is not str or not value for value in pair.values()) or pair["id"] in seen:
            raise ValueError("Invalid pair strings or duplicate ID")
        seen.add(pair["id"])
        labels = ["personalized", "baseline"]
        rng.shuffle(labels)
        rows.append({"id": pair["id"], "context": pair["context"],
                     "a": pair[labels[0]], "b": pair[labels[1]]})
        key.append({"id": pair["id"], "a": labels[0], "b": labels[1]})
    rng.shuffle(rows)
    return rows, key


def rating_report(ratings, key):
    if (type(key) is not list or any(type(pair) is not dict or set(pair) != {"id", "a", "b"}
            or type(pair["id"]) is not str or {pair["a"], pair["b"]} != {"personalized", "baseline"}
            for pair in key) or len({pair["id"] for pair in key}) != len(key)):
        raise ValueError("Invalid or duplicate private answer key")
    if type(ratings) is not list:
        raise ValueError("Ratings must be a list")
    answers = {pair["id"]: pair for pair in key}
    rows, seen = [], set()
    for rating in ratings:
        if (type(rating) is not dict or set(rating) != {"id", "preferred", "scores"}
                or rating["id"] not in answers or rating["id"] in seen
                or rating["preferred"] not in ("a", "b", "tie")
                or type(rating["scores"]) is not dict or set(rating["scores"]) != {"a", "b"}):
            raise ValueError("Invalid or duplicate owner rating")
        seen.add(rating["id"])
        for side in ("a", "b"):
            scores = rating["scores"][side]
            if (type(scores) is not dict or set(scores) != set(RATING_DIMENSIONS)
                    or any(type(score) is not int or not 1 <= score <= 5 for score in scores.values())):
                raise ValueError("All rating dimensions require 1-5 owner scores")
        rows.append(rating)
    personalized = [row["scores"]["a" if answers[row["id"]]["a"] == "personalized" else "b"] for row in rows]
    ties = sum(row["preferred"] == "tie" for row in rows)
    preferred = sum(row["preferred"] != "tie" and answers[row["id"]][row["preferred"]] == "personalized" for row in rows)
    return {"ratings": len(rows), "ties": ties, "preferred": preferred,
            "preference_or_tie": wilson(ties + preferred, len(rows)),
            "dimensions": {dimension: {
                "score_4_or_5": wilson(sum(row[dimension] >= 4 for row in personalized), len(rows)),
                "average": sum(row[dimension] for row in personalized) / len(rows) if rows else None,
            } for dimension in RATING_DIMENSIONS},
            "qualification": "pending_external", "minimum_owner_sample_met": len(rows) >= 40}


def write_private_json(path, value):
    """Never overwrite an evaluation artifact or put it in the checkout."""
    import os
    from pathlib import Path
    from shadow.operations import private_destination

    def structured_default(item):
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        raise TypeError("Unsupported private report value")

    # Validate serialization before opening the exclusive destination, so a
    # failed report cannot leave an apparently complete but truncated file.
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                            default=structured_default)
    target = private_destination(path)
    if not target.parent.is_dir():
        raise ValueError("Create a private output directory first")
    if os.name != "nt" and target.parent.stat().st_mode & 0o077:
        raise ValueError("Output directory must be owner-only (0700)")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(serialized)
        stream.write("\n")
    return Path(target)


def prepare_ratings(pairs, directory, *, seed):
    """Three separate private artifacts: rater view, answer key and score form."""
    import os
    from shadow.operations import private_destination

    visible, key = blind_pairs(pairs, seed=seed)
    target = private_destination(directory)
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    write_private_json(target / "pairs.json", visible)
    write_private_json(target / "answer-key.json", key)
    template = [{"id": row["id"], "preferred": None,
                 "scores": {side: dict.fromkeys(RATING_DIMENSIONS, None) for side in ("a", "b")}}
                for row in visible]
    write_private_json(target / "ratings.json", template)
    return target
