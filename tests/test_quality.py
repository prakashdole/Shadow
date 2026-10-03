"""Real confirmed-turn metrics and one downgrade, with fictional messages."""

import tempfile
import unittest
from datetime import datetime, timezone

from shadow.coordinator import Coordinator
from shadow.observe import NoDecisionProvider
from shadow.pacing import ManualClock
from shadow.quality import metrics, record_signals
from shadow.settings import Settings
from shadow.storage import open_storage


class QualityTests(unittest.TestCase):
    def test_confusion_after_confirmed_send_downgrades_once(self):
        with tempfile.TemporaryDirectory() as directory, open_storage(directory) as storage:
            coordinator = Coordinator(storage, None, NoDecisionProvider(),
                Settings(mode="autonomous", confusion_downgrade=(10, 604800, 1)),
                clock=ManualClock(datetime.now(timezone.utc)), account_id="fictional")
            cid = storage.get_or_create_conversation("fictional", "123", "Friend")
            storage.set_work(cid, inbound_revision=1, state="idle")
            decision = storage.add_decision(conversation_id=cid, source_revision=1,
                                             record_json="{}", versions_json="{}", status="queued")
            item = storage.add_outbox_item(decision_id=decision, item_index=0, conversation_id=cid,
                                          text="fictional reply", authority="autonomous", revision=1)
            storage.transition(item, "attempting")
            storage.confirm_send(item, {"text": "fictional reply"})
            record_signals(coordinator, cid, 2, {"confusion"})
            record_signals(coordinator, cid, 2, {"confusion"})
            self.assertEqual(coordinator.mode, "assisted")
            report = metrics(storage, coordinator.clock.now())
            self.assertEqual(report["autonomous_turns_7d"], 1)
            self.assertEqual(report["confusing_turns_7d"], 1)
            self.assertEqual(len([alert for alert in storage.alerts() if alert["reason"] == "quality_downgrade"]), 1)


if __name__ == "__main__":
    unittest.main()
