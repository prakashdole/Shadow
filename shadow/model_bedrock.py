"""Optional Bedrock Converse transport for the strict v2 decision roles."""

from __future__ import annotations

import asyncio

from shadow.decisions import ModelResponse


class BedrockTransport:
    """Explicit role models only; no default or silent provider fallback."""

    def __init__(self, *, region: str, models: dict[str, str], client=None):
        if not region or set(models) != {"risk", "reply", "extraction"} or not all(models.values()):
            raise ValueError("region and explicit model for each role are required")
        if client is None:
            import boto3
            client = boto3.client("bedrock-runtime", region_name=region)
        self.client, self.models = client, models

    async def generate(self, role: str, system: str, input_text: str) -> ModelResponse:
        model = self.models[role]
        response = await asyncio.to_thread(
            self.client.converse,
            modelId=model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": input_text}]}],
            inferenceConfig={"maxTokens": 1024},
        )
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        if len(blocks) != 1 or set(blocks[0]) != {"text"}:
            return ModelResponse("", response.get("usage", {}), model, "invalid_content")
        return ModelResponse(blocks[0]["text"], response.get("usage", {}), model,
                             response.get("stopReason", "missing"))
