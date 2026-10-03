#!/usr/bin/env python3
"""Three bounded live role calls on fictional input. No Instagram or key storage."""

import argparse
import asyncio
import getpass
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shadow.contracts import ContextEvent, DecisionInput
from shadow.decisions import StructuredDecisions
from shadow.evaluation import write_private_json
from shadow.model_openai import OpenAITransport
from shadow.operations import private_destination
from shadow.settings import Settings
from shadow.storage import open_storage


async def check(transport, storage):
    raw_responses = []
    original = transport.generate
    async def capture(role, system, input_text):
        response = await original(role, system, input_text)
        raw_responses.append({"role": role, **asdict(response)})
        return response
    transport.generate = capture
    data = DecisionInput(1, "known", {}, {},
                         (ContextEvent(4, "contact", "text", "I prefer tea to coffee."),), ())
    decisions = StructuredDecisions(transport, storage, Settings())
    outcomes = [await decisions.assess(data)]
    if outcomes[0].value and outcomes[0].value.action_hint == "proceed":
        outcomes.append(await decisions.draft(data, outcomes[0].value))
    outcomes.append(await decisions.extract(data))
    return {"results": [asdict(result) for result in outcomes], "raw_responses": raw_responses, "role_calls": [
        dict(row) for row in storage.conn.execute("SELECT * FROM role_calls ORDER BY id")]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    private_destination(args.output)
    key = getpass.getpass("API key (hidden, not stored): ")
    transport = OpenAITransport(base_url=args.base_url, api_key=key,
                               models=dict.fromkeys(("risk", "reply", "extraction"), args.model), max_tokens=512)
    del key
    with tempfile.TemporaryDirectory(prefix="shadow-live-model-") as scratch, open_storage(scratch) as storage:
        report = asyncio.run(check(transport, storage))
    write_private_json(args.output, report)
    print(json.dumps({"roles": [{"role": r["role"], "failure": r["failure"],
                                 "usage": r["usage"], "versions": r["versions"]}
                                for r in report["results"]], "Instagram_sends": 0}, sort_keys=True))


if __name__ == "__main__":
    main()
