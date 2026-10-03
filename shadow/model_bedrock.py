"""Optional Bedrock Converse transport for the strict v2 decision roles."""

from __future__ import annotations

import asyncio
from copy import deepcopy

from shadow.decisions import ModelResponse


class BedrockTransport:
    """Explicit role models only; no default or silent provider fallback."""

    def __init__(self, *, region: str, models: dict[str, str], client=None, inference=None,
                 context_chars=32000):
        if not region or set(models) != {"risk", "reply", "extraction"} or not all(models.values()):
            raise ValueError("region and explicit model for each role are required")
        if type(context_chars) is not int or not 1000 <= context_chars <= 1000000:
            raise ValueError("context_chars must be a bounded positive budget")
        inference = deepcopy(inference) if inference is not None else {
            role: {"maxTokens": 1024} for role in models}
        if type(inference) is not dict or set(inference) != set(models):
            raise ValueError("Explicit inference settings are required for every role")
        for settings in inference.values():
            if (type(settings) is not dict or not set(settings) <= {"maxTokens", "temperature", "topP", "stopSequences"}
                    or type(settings.get("maxTokens")) is not int or not 1 <= settings["maxTokens"] <= 8192):
                raise ValueError("Invalid role inference settings")
            for key in ("temperature", "topP"):
                if key in settings and (type(settings[key]) not in (int, float) or not 0 <= settings[key] <= 1):
                    raise ValueError("Sampling values must be between zero and one")
            if "stopSequences" in settings and (type(settings["stopSequences"]) is not list
                    or len(settings["stopSequences"]) > 4 or any(type(value) is not str or not value
                    or len(value) > 100 for value in settings["stopSequences"])):
                raise ValueError("Invalid stop sequences")
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client("bedrock-runtime", region_name=region,
                config=Config(connect_timeout=5, read_timeout=20,
                              retries={"total_max_attempts": 1}))
        self.client, self.models = client, dict(models)
        self.region, self.inference, self.context_chars = region, inference, context_chars

    def configuration(self, role):
        return {"provider": "bedrock_converse", "region": self.region, "model": self.models[role],
                "inference": self.inference[role], "context_chars": self.context_chars,
                "structured_output": "strict_json_validation", "socket_timeout_seconds": 20,
                "transport_attempts": 1}

    async def generate(self, role: str, system: str, input_text: str) -> ModelResponse:
        if role not in self.models or len(system) + len(input_text) > self.context_chars:
            raise ValueError("Unknown role or context exceeds declared provider budget")
        model = self.models[role]
        response = await asyncio.to_thread(
            self.client.converse,
            modelId=model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": input_text}]}],
            inferenceConfig=self.inference[role],
        )
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        if len(blocks) != 1 or set(blocks[0]) != {"text"}:
            return ModelResponse("", response.get("usage", {}), model, "invalid_content")
        return ModelResponse(blocks[0]["text"], response.get("usage", {}), model,
                             response.get("stopReason", "missing"))
