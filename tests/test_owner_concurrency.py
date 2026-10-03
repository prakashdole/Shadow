"""One store/loop: owner pause stays responsive while decisions are pending."""

import asyncio
import tempfile
import unittest

from fake_ports import FakeDecisions, event, read, row
from shadow.contracts import InboxResult
from shadow.observe import observe_loop
from shadow.settings import Settings
from shadow.storage import open_storage
from test_observe import LifecycleBrowser


class OwnerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_is_processed_during_model_call(self):
        with tempfile.TemporaryDirectory() as directory, open_storage(directory) as storage:
            cid = storage.get_or_create_conversation("fictional", "100200300", "Test One")
            storage.set_contact_status(cid, "known")
            storage.mark_baseline_done(cid)
            storage.add_event(conversation_id=cid, sender="contact", kind="text",
                              text="first", source="dom", origin="unknown")
            storage.set_work(cid, inbound_revision=0, state="idle")
            started = asyncio.Event()
            processed = asyncio.Event()

            class Waiting(FakeDecisions):
                async def assess(self, data):
                    started.set()
                    await processed.wait()
                    return await super().assess(data)

            class OwnerTransport:
                async def poll_once_async(self, handler):
                    await started.wait()
                    handler.process({"update_id": 1, "message": {
                        "from": {"id": 11}, "chat": {"id": 22, "type": "private"},
                        "text": "/pause"}})
                    processed.set()
                    await asyncio.Event().wait()

            browser = LifecycleBrowser()
            browser.inbox_result = InboxResult("ok", rows=(row(0),))
            browser.reads["100200300"] = read("100200300", (
                event(0, "contact", "first"), event(1, "contact", "second")))
            await asyncio.wait_for(observe_loop(
                storage, browser, Settings(), "fictional", Waiting(), once=True,
                owner_transport=OwnerTransport(), owner_user_id=11, owner_chat_id=22), timeout=2)
            self.assertTrue(processed.is_set())
            self.assertEqual(storage.get_runtime()["pause_reason"], "owner")
            self.assertEqual(storage.outbox_items(), [])
            self.assertTrue(browser.closed)


if __name__ == "__main__":
    unittest.main()
