"""Check that the packaged voice guide reaches both reply providers."""

import json
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from shadow.context_memory import ContextMemory
from shadow.screenshot_loop import gemini_fallback, openai_primary, reply_system
from shadow.storage import open_storage


class ReplyStyleTests(TestCase):
    def test_both_reply_providers_keep_style_and_chat_separate(self):
        system = reply_system()
        self.assertIn("Instagram DM reply style", system)
        self.assertIn("latest unresolved message", system)
        self.assertIn("Return one JSON object and nothing else", system)
        conversation = '{"bubbles":[{"text":"A new example for this turn"}]}'

        openai_response = MagicMock()
        openai_response.read.return_value = json.dumps({
            "model": "gpt-4o-mini",
            "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
        }).encode()
        with patch("shadow.model_openai.build_opener") as openai_opener:
            opener = openai_opener.return_value
            opener.open.return_value.__enter__.return_value = openai_response
            openai_primary("fixture-key")(system, conversation)
            openai_payload = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(openai_payload["messages"], [
            {"role": "system", "content": system}, {"role": "user", "content": conversation},
        ])
        self.assertEqual(openai_payload["response_format"], {"type": "json_object"})

        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
        }).encode()
        with patch("shadow.gemini_client.build_opener") as build_opener:
            opener = build_opener.return_value
            opener.open.return_value.__enter__.return_value = response
            gemini_fallback(["fixture-key"])(system, conversation)
            request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["systemInstruction"], {"parts": [{"text": system}]})
        self.assertEqual(payload["contents"], [{"role": "user", "parts": [{"text": conversation}]}])

    def test_chat_style_correction_takes_priority(self):
        with TemporaryDirectory() as directory, open_storage(directory) as store:
            chat = store.get_or_create_conversation("account", "1")
            store.record_owner_update(1, "style correction", "ok")
            memory = ContextMemory(store)
            memory.correct(None, "language", "english", 1)
            memory.correct(chat, "language", "hinglish", 1)
            prompt = reply_system(store, chat)
            self.assertIn("Use natural Latin-script Hinglish when appropriate.", prompt)
            self.assertNotIn("Prefer English unless the contact explicitly requests another language.", prompt)
