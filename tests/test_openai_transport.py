"""Focused checks at the request/response boundary; no external credentials."""

import asyncio
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from shadow.model_openai import OpenAITransport, ProviderError, configured_transport


class OpenAITransportTests(unittest.TestCase):
    def test_private_role_report_serializes_signals_before_creating_file(self):
        import tempfile
        from pathlib import Path
        from shadow.evaluation import write_private_json

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'report.json'
            write_private_json(destination, {'signals': frozenset({'owner_needed'})})
            self.assertEqual(json.loads(destination.read_text())['signals'], ['owner_needed'])
            invalid = Path(directory) / 'invalid.json'
            with self.assertRaises(TypeError):
                write_private_json(invalid, {'unsupported': object()})
            self.assertFalse(invalid.exists())

    def transport(self):
        return OpenAITransport(base_url="https://api.minimax.io/v1", api_key="fictional-test-key",
                               models=dict.fromkeys(("risk", "reply", "extraction"), "fictional-model"))

    def response(self, **changes):
        result = {"model": "fictional-model", "base_resp": {"status_code": 0},
                  "usage": {"prompt_tokens": 7, "completion_tokens": 2}, "choices": [
                      {"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}]}
        result.update(changes)
        return io.BytesIO(json.dumps(result).encode())

    def test_request_has_budgets_and_reasoning_split_and_records_usage(self):
        transport = self.transport()
        transport._opener = Mock()
        transport._opener.open.return_value = self.response()
        result = asyncio.run(transport.generate("risk", "instruction", "fictional conversation"))
        request = transport._opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.minimax.io/v1/chat/completions")
        self.assertEqual(body["max_tokens"], 1024)
        self.assertTrue(body["reasoning_split"])
        self.assertFalse(body["stream"])
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.usage["prompt_tokens"], 7)
        self.assertNotIn("fictional-test-key", repr(transport.configuration("risk")))

    def test_truncation_and_refusal_never_become_success(self):
        for finish, expected in (("length", "max_tokens"), ("content_filter", "refusal"),
                                 ("tool_calls", "invalid_content")):
            with self.subTest(finish=finish):
                transport = self.transport()
                transport._opener = Mock()
                response = json.load(self.response())
                response["choices"][0]["finish_reason"] = finish
                transport._opener.open.return_value = io.BytesIO(json.dumps(response).encode())
                self.assertEqual(asyncio.run(transport.generate("risk", "s", "u")).stop_reason, expected)

    def test_http_and_provider_errors_are_redacted_and_quota_is_not_balance(self):
        for status, code, expected in ((429, 2056, "provider_token_limit"),
                                        (400, 1008, "provider_balance_exhausted"),
                                        (401, 1004, "provider_auth_failed")):
            transport = self.transport()
            transport._opener = Mock()
            body = json.dumps({"base_resp": {"status_code": code, "status_msg": "private text"}}).encode()
            transport._opener.open.side_effect = HTTPError("https://api.minimax.io", status,
                                                         "private text", {}, io.BytesIO(body))
            with self.assertRaises(ProviderError) as caught:
                asyncio.run(transport.generate("risk", "s", "u"))
            self.assertEqual(str(caught.exception), expected)

    def test_http_200_provider_rejection_and_invalid_output_fail_closed(self):
        for result in ({"base_resp": {"status_code": 2056}},
                       {"choices": []}, {"choices": [{"message": {"role": "assistant", "content": None}}]}):
            transport = self.transport()
            transport._opener = Mock()
            transport._opener.open.return_value = io.BytesIO(json.dumps(result).encode())
            with self.assertRaises(ProviderError):
                asyncio.run(transport.generate("risk", "s", "u"))

    def test_budget_and_provider_choice_have_no_silent_fallback(self):
        transport = self.transport()
        with self.assertRaises(ValueError):
            asyncio.run(transport.generate("risk", "s", "u" * 32000))
        with patch.dict("os.environ", {"SHADOW_MODEL_PROVIDER": "unconfigured"}):
            with self.assertRaises(ValueError):
                configured_transport()
        with self.assertRaises(ValueError):
            OpenAITransport(base_url="https://secret@api.minimax.io/v1", api_key="fictional-key",
                            models=transport.models)

    def test_redirect_never_forwards_authorization(self):
        from shadow.model_openai import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))
