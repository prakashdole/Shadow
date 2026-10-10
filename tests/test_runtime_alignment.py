"""Qualified runtime-ID alignment stays optional, ordered, and fail-closed."""

from __future__ import annotations

from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_ports import event, read
from shadow import alignment as A
from shadow import storage as ST
from shadow.coordinator import TickReport
from test_coordinator import CoordinatorCase, THREAD_A, settings


def observed(position: int, sender: str, text: str, identity: str | None = None,
             *, kind: str = "text", media_label: str | None = None,
             reply_to_text: str | None = None, source: str = A.RUNTIME_ID_SOURCE):
    evidence = ({"runtime_message_id": identity, "runtime_identity_source": source}
                if identity is not None else {})
    return replace(event(position, sender, text, kind), evidence=evidence,
                   media_label=media_label, reply_to_text=reply_to_text)


def stored(event_id: int, sender: str, text: str, identity: str | None = None,
           *, kind: str = "text", media_label: str | None = None,
           reply_to: str | None = None, source: str = A.RUNTIME_ID_SOURCE):
    evidence = ({"runtime_message_id": identity, "runtime_identity_source": source}
                if identity is not None else {})
    return {"id": event_id, "sender": sender, "kind": kind, "text": text,
            "media_label": media_label, "reply_to": reply_to,
            "evidence_json": json.dumps(evidence)}


class TestRuntimeAlignment(unittest.TestCase):
    def test_restoration_requires_a_full_original_anchor(self) -> None:
        from shadow.instagram import _restored_content_matches

        self.assertFalse(_restored_content_matches(
            [observed(0, "contact", "a"), observed(1, "contact", "b")],
            [observed(0, "contact", "b")],
        ))
        self.assertFalse(_restored_content_matches([observed(0, "contact", "a")], []))
        self.assertTrue(_restored_content_matches(
            [observed(0, "contact", "same"), observed(1, "contact", "same")],
            [observed(0, "contact", "same"), observed(1, "contact", "same")],
        ))
        self.assertTrue(_restored_content_matches(
            [observed(0, "contact", "same", "stable")],
            [observed(0, "contact", "same")],
        ))
        self.assertFalse(_restored_content_matches(
            [observed(0, "contact", "same", "stable")],
            [observed(0, "contact", "same"), observed(1, "contact", "changed", "stable")],
        ))

    def test_distinct_ids_resolve_repeated_identical_content(self) -> None:
        prior = [stored(1, "contact", "same", "one"),
                 stored(2, "contact", "same", "two")]
        window = [observed(0, "contact", "same", "two"),
                  observed(1, "contact", "same", "three")]

        result = A.align_events(prior, window)

        self.assertEqual((result.status, result.new_start_index, result.overlap), ("ok", 1, 1))
        self.assertEqual(result.candidates, (1,))

    def test_same_id_requires_every_alignment_key_field_to_match(self) -> None:
        baseline = stored(1, "contact", "same", "stable", media_label="photo", reply_to="quoted")
        cases = (
            observed(0, "owner", "same", "stable", media_label="photo", reply_to_text="quoted"),
            observed(0, "contact", "same", "stable", kind="media", media_label="photo", reply_to_text="quoted"),
            observed(0, "contact", "changed", "stable", media_label="photo", reply_to_text="quoted"),
            observed(0, "contact", "same", "stable", media_label="video", reply_to_text="quoted"),
            observed(0, "contact", "same", "stable", media_label="photo", reply_to_text="other"),
        )

        for current in cases:
            with self.subTest(current=current):
                result = A.align_events([baseline], [current])
                self.assertEqual(result.status, "gap")
                self.assertTrue(A.is_runtime_identity_hold(result))

    def test_duplicate_qualified_id_and_ordered_replay_hold(self) -> None:
        duplicate = [observed(0, "contact", "one", "duplicate"),
                     observed(1, "contact", "two", "duplicate")]
        duplicate_result = A.align_events((), duplicate)
        self.assertEqual(duplicate_result.status, "ambiguous")
        self.assertTrue(A.is_runtime_identity_hold(duplicate_result))

        prior = [stored(1, "contact", "one", "one"), stored(2, "contact", "two", "two")]
        reordered = [observed(0, "contact", "two", "two"), observed(1, "contact", "one", "one")]
        reorder_result = A.align_events(prior, reordered)
        self.assertEqual(reorder_result.status, "gap")
        self.assertTrue(A.is_runtime_identity_hold(reorder_result))

    def test_different_known_ids_never_fall_back_to_equal_content(self) -> None:
        result = A.align_events(
            [stored(1, "contact", "same", "stored-id")],
            [observed(0, "contact", "same", "observed-id")],
        )

        self.assertEqual(result.status, "gap")
        self.assertTrue(A.is_runtime_identity_hold(result))

    def test_mixed_shared_and_different_known_ids_hold(self) -> None:
        result = A.align_events(
            [stored(1, "contact", "first", "shared"), stored(2, "contact", "same", "stored-id")],
            [observed(0, "contact", "first", "shared"), observed(1, "contact", "same", "observed-id")],
        )

        self.assertEqual(result.status, "gap")
        self.assertTrue(A.is_runtime_identity_hold(result))

    def test_wrong_source_is_not_runtime_identity(self) -> None:
        prior = [stored(1, "contact", "same", "one", source="unqualified_v0")]
        window = [observed(0, "contact", "same", "one", source="unqualified_v0"),
                  observed(1, "contact", "same", "two", source="unqualified_v0")]

        self.assertIsNone(A.runtime_message_id(prior[0]))
        self.assertIsNone(A.runtime_message_id(window[0]))
        self.assertEqual(A.align_events(prior, window).status, "ambiguous")

    def test_explicit_runtime_conflict_flags_hold_without_an_id(self) -> None:
        conflicted = replace(event(0, "contact", "same"), evidence={
            "runtime_outgoing_sender_conflict": True,
        })

        result = A.align_events([], [conflicted])

        self.assertEqual(result.status, "gap")
        self.assertTrue(A.is_runtime_identity_hold(result))

    def test_backfill_requires_unique_content_alignment(self) -> None:
        prior = [stored(1, "contact", "same"), stored(2, "contact", "same")]
        window = [observed(0, "contact", "same", "one"),
                  observed(1, "contact", "same", "two")]

        self.assertEqual(A.runtime_backfill_pairs(prior, window), [])


class TestRuntimeMetadataStorage(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="shadow-runtime-id-"))
        self.storage = ST.open_storage(self.directory)
        self.addCleanup(self._close)
        self.addCleanup(self._cleanup)
        self.conversation_id = self.storage.get_or_create_conversation("account", "100200300", "Test")

    def _close(self) -> None:
        if self.storage.conn is not None:
            self.storage.close()

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)

    def _add(self, text: str, *, platform_msg_id: str | None = None) -> int:
        return self.storage.add_event(
            conversation_id=self.conversation_id, sender="contact", kind="text", text=text,
            source="dom", origin="unknown", platform_msg_id=platform_msg_id,
        )

    def test_unique_backfill_survives_reopen_without_touching_platform_id(self) -> None:
        event_id = self._add("anchor", platform_msg_id="platform-untouched")
        row = self.storage.get_events(self.conversation_id)[0]
        pairs = A.runtime_backfill_pairs([row], [observed(0, "contact", "anchor", "runtime-1")])
        self.assertEqual(pairs, [(event_id, "runtime-1")])
        self.storage.bind_runtime_message_ids(self.conversation_id, pairs)
        self.storage.close()
        self.storage = ST.open_storage(self.directory)

        saved = self.storage.get_events(self.conversation_id)[0]
        self.assertEqual(saved["platform_msg_id"], "platform-untouched")
        self.assertEqual(json.loads(saved["evidence_json"]), {
            "runtime_message_id": "runtime-1",
            "runtime_identity_source": A.RUNTIME_ID_SOURCE,
        })

    def test_storage_rejects_a_runtime_id_bound_to_another_event(self) -> None:
        first, second = self._add("one"), self._add("two")
        self.storage.bind_runtime_message_ids(self.conversation_id, [(first, "runtime-1")])
        with self.assertRaisesRegex(ST.StorageError, "Conflicting runtime"):
            self.storage.bind_runtime_message_ids(self.conversation_id, [(second, "runtime-1")])


class TestRuntimeCoordinator(CoordinatorCase):
    async def test_baseline_event_write_and_marker_roll_back_together(self) -> None:
        conversation = self.conversation()
        baseline = (observed(0, "contact", "first", "one"),
                    observed(1, "contact", "second", "two"))
        failed_report = TickReport()

        with patch.object(self.storage, "mark_baseline_done", side_effect=ST.StorageError("marker failure")):
            with self.assertRaisesRegex(ST.StorageError, "marker failure"):
                await self.coordinator._ingest(
                    conversation, read(THREAD_A, baseline), failed_report
                )

        self.assertEqual(self.storage.get_events(conversation), [])
        self.assertFalse(self.storage.get_conversation(conversation)["first_seen_baseline_done"])
        self.assertEqual(self.storage.outbox_items(conversation), [])
        self.assertEqual(failed_report.ingested, 0)

        retry_report = TickReport()
        await self.coordinator._ingest(conversation, read(THREAD_A, baseline), retry_report)

        self.assertEqual([row["text"] for row in self.storage.get_events(conversation)], ["first", "second"])
        self.assertTrue(self.storage.get_conversation(conversation)["first_seen_baseline_done"])
        self.assertEqual(self.storage.outbox_items(conversation), [])
        self.assertEqual(retry_report.ingested, 2)

    async def test_conflicted_runtime_metadata_cannot_become_a_baseline(self) -> None:
        conversation = self.conversation()
        conflicted = replace(event(0, "contact", "same"), evidence={
            "runtime_identity_conflict": True,
        })

        await self.coordinator._ingest(conversation, read(THREAD_A, (conflicted,)), TickReport())

        self.assertEqual(self.storage.get_events(conversation), [])
        self.assertFalse(self.storage.get_conversation(conversation)["first_seen_baseline_done"])

    async def test_ingest_uses_ids_for_repeated_content_without_dropping_or_duplicating(self) -> None:
        conversation = self.conversation()
        baseline = (observed(0, "contact", "same", "one"),
                    observed(1, "contact", "same", "two"))
        await self.coordinator._ingest(conversation, read(THREAD_A, baseline), TickReport())

        report = TickReport()
        await self.coordinator._ingest(
            conversation,
            read(THREAD_A, (observed(0, "contact", "same", "two"),
                            observed(1, "contact", "next", "three"))),
            report,
        )

        self.assertEqual(report.ingested, 1)
        self.assertEqual([row["text"] for row in self.storage.get_events(conversation)],
                         ["same", "same", "next"])

    async def test_conflicting_id_holds_before_canonical_mutation_or_recovery(self) -> None:
        conversation = self.conversation()
        await self.coordinator._ingest(
            conversation, read(THREAD_A, (observed(0, "contact", "before", "stable"),)), TickReport()
        )
        self.storage.set_work(conversation, inbound_revision=0, state="idle")

        report = TickReport()
        await self.coordinator._ingest(
            conversation, read(THREAD_A, (observed(0, "contact", "after", "stable"),)), report
        )

        self.assertEqual([row["text"] for row in self.storage.get_events(conversation)], ["before"])
        self.assertEqual(self.storage.get_work(conversation)["held_reason"], "align_gap")
        self.assertEqual(self.browser.history_requests, [])
        self.assertEqual(self.decisions.calls, [])

    async def test_same_node_edit_cannot_replace_a_different_runtime_id(self) -> None:
        conversation = self.conversation()
        baseline = (
            replace(observed(0, "owner", "left", "left"), evidence={
                "runtime_message_id": "left", "runtime_identity_source": A.RUNTIME_ID_SOURCE,
                "node_key": "node-left",
            }),
            replace(observed(1, "contact", "before", "stored-middle"), evidence={
                "runtime_message_id": "stored-middle", "runtime_identity_source": A.RUNTIME_ID_SOURCE,
                "node_key": "node-middle",
            }),
            replace(observed(2, "owner", "right", "right"), evidence={
                "runtime_message_id": "right", "runtime_identity_source": A.RUNTIME_ID_SOURCE,
                "node_key": "node-right",
            }),
        )
        await self.coordinator._ingest(conversation, read(THREAD_A, baseline), TickReport())
        self.storage.set_work(conversation, inbound_revision=0, state="idle")
        changed_middle = replace(observed(1, "contact", "after", "observed-middle"), evidence={
            "runtime_message_id": "observed-middle", "runtime_identity_source": A.RUNTIME_ID_SOURCE,
            "node_key": "node-middle", "edited": True,
        })

        await self.coordinator._ingest(
            conversation, read(THREAD_A, (baseline[0], changed_middle, baseline[2])),
            TickReport(), recover=False,
        )

        middle = self.storage.get_events(conversation)[1]
        self.assertEqual(middle["text"], "before")
        self.assertEqual(self.storage.event_revisions(middle["id"]), [])
        self.assertEqual(self.storage.get_work(conversation)["held_reason"], "align_gap")

    async def test_same_node_edit_requires_qualified_flank_agreement(self) -> None:
        conversation = self.conversation()
        baseline = (
            replace(observed(0, "owner", "left", "left-1"), evidence={
                "runtime_message_id": "left-1", "runtime_identity_source": A.RUNTIME_ID_SOURCE,
            }),
            replace(observed(1, "contact", "before"), evidence={"node_key": "node-middle"}),
            replace(observed(2, "owner", "right", "right-1"), evidence={
                "runtime_message_id": "right-1", "runtime_identity_source": A.RUNTIME_ID_SOURCE,
            }),
        )
        await self.coordinator._ingest(conversation, read(THREAD_A, baseline), TickReport())
        self.storage.set_work(conversation, inbound_revision=0, state="idle")
        changed_middle = replace(observed(1, "contact", "after"), evidence={
            "node_key": "node-middle", "edited": True,
        })
        changed_flanks = (
            replace(baseline[0], evidence={
                "runtime_message_id": "left-2", "runtime_identity_source": A.RUNTIME_ID_SOURCE,
            }),
            changed_middle,
            replace(baseline[2], evidence={
                "runtime_message_id": "right-2", "runtime_identity_source": A.RUNTIME_ID_SOURCE,
            }),
        )

        await self.coordinator._ingest(
            conversation, read(THREAD_A, changed_flanks), TickReport(),
        )

        middle = self.storage.get_events(conversation)[1]
        self.assertEqual(middle["text"], "before")
        self.assertEqual(self.storage.event_revisions(middle["id"]), [])
        self.assertEqual(self.storage.get_work(conversation)["held_reason"], "align_gap")
        self.assertEqual(self.browser.history_requests, [])

    async def test_pre_send_alignment_backfills_legacy_id_without_cancelling_draft(self) -> None:
        conversation = self.conversation()
        self.seed_history(conversation, text="anchor")
        self.queue_due_send(conversation, "draft")
        self.browser.reads[THREAD_A] = read(THREAD_A, (observed(0, "contact", "anchor", "runtime-1"),))

        allowed = await self.coordinator._fresh_for_send(conversation, TickReport())

        self.assertTrue(allowed)
        saved = self.storage.get_events(conversation)[0]
        self.assertEqual(A.runtime_message_id(saved), "runtime-1")
        self.assertEqual(self.storage.outbox_items(conversation)[0]["state"], "pending")

    async def test_ingest_holds_when_legacy_backfill_id_exists_before_the_tail(self) -> None:
        self.coordinator = self.build(settings=settings(align_tail_events=1))
        conversation = self.conversation()
        self.storage.add_event(
            conversation_id=conversation, sender="contact", kind="text", text="older",
            source="dom", origin="unknown", evidence={
                "runtime_message_id": "existing-id",
                "runtime_identity_source": A.RUNTIME_ID_SOURCE,
            },
        )
        self.seed_history(conversation, text="anchor")
        self.storage.set_work(conversation, inbound_revision=0, state="idle")

        await self.coordinator._ingest(
            conversation, read(THREAD_A, (observed(0, "contact", "anchor", "existing-id"),)),
            TickReport(),
        )

        saved = self.storage.get_events(conversation)
        self.assertEqual([row["text"] for row in saved], ["older", "anchor"])
        self.assertEqual(A.runtime_message_id(saved[1]), None)
        self.assertEqual(self.storage.get_work(conversation)["held_reason"], "align_gap")

    async def test_pre_send_holds_when_legacy_backfill_id_exists_before_the_tail(self) -> None:
        self.coordinator = self.build(settings=settings(align_tail_events=1))
        conversation = self.conversation()
        self.storage.add_event(
            conversation_id=conversation, sender="contact", kind="text", text="older",
            source="dom", origin="unknown", evidence={
                "runtime_message_id": "existing-id",
                "runtime_identity_source": A.RUNTIME_ID_SOURCE,
            },
        )
        self.seed_history(conversation, text="anchor")
        self.queue_due_send(conversation, "draft")
        self.browser.reads[THREAD_A] = read(THREAD_A, (
            observed(0, "contact", "anchor", "existing-id"),
        ))

        allowed = await self.coordinator._fresh_for_send(conversation, TickReport())

        saved = self.storage.get_events(conversation)
        self.assertFalse(allowed)
        self.assertEqual([row["text"] for row in saved], ["older", "anchor"])
        self.assertEqual(A.runtime_message_id(saved[1]), None)
        self.assertEqual(self.storage.get_work(conversation)["held_reason"], "align_gap")
        self.assertEqual(self.storage.outbox_items(conversation)[0]["state"], "pending")

    async def test_old_unqualified_runtime_evidence_blocks_backfill_as_a_hold(self) -> None:
        self.coordinator = self.build(settings=settings(align_tail_events=1))
        conversation = self.conversation()
        self.storage.add_event(
            conversation_id=conversation, sender="contact", kind="text", text="older",
            source="dom", origin="unknown", evidence={
                "runtime_message_id": "stale-id",
                "runtime_identity_source": "unqualified_v0",
            },
        )
        self.seed_history(conversation, text="anchor")
        self.storage.set_work(conversation, inbound_revision=0, state="idle")

        await self.coordinator._ingest(
            conversation, read(THREAD_A, (observed(0, "contact", "anchor", "stale-id"),)),
            TickReport(),
        )

        saved = self.storage.get_events(conversation)
        self.assertEqual(A.runtime_message_id(saved[1]), None)
        self.assertEqual(self.storage.get_work(conversation)["held_reason"], "align_gap")

    def test_unrelated_storage_error_still_propagates(self) -> None:
        conversation = self.conversation()
        event_id = self.storage.add_event(
            conversation_id=conversation, sender="contact", kind="text", text="anchor",
            source="dom", origin="unknown",
        )
        stored_event = self.storage.get_events(conversation)
        window = (observed(0, "contact", "anchor", "runtime-id"),)
        self.assertEqual(event_id, stored_event[0]["id"])

        with patch.object(self.storage, "bind_runtime_message_ids", side_effect=ST.StorageError("disk failure")):
            with self.assertRaisesRegex(ST.StorageError, "disk failure"):
                self.coordinator._align_window(conversation, stored_event, window)

    async def test_new_event_cannot_reuse_id_outside_alignment_tail(self) -> None:
        self.coordinator = self.build(settings=settings(align_tail_events=1))
        conversation = self.conversation()
        self.storage.add_event(
            conversation_id=conversation, sender="contact", kind="text", text="older",
            source="dom", origin="unknown", evidence={
                "runtime_message_id": "already-stored",
                "runtime_identity_source": A.RUNTIME_ID_SOURCE,
            },
        )
        self.seed_history(conversation, text="anchor")
        self.storage.set_work(conversation, inbound_revision=0, state="idle")

        report = TickReport()
        await self.coordinator._ingest(
            conversation,
            read(THREAD_A, (observed(0, "contact", "anchor"),
                            observed(1, "contact", "new", "already-stored"))),
            report,
        )

        self.assertEqual([row["text"] for row in self.storage.get_events(conversation)], ["older", "anchor"])
        self.assertEqual(self.storage.get_work(conversation)["held_reason"], "align_gap")
        self.assertEqual(report.ingested, 0)


if __name__ == "__main__":
    unittest.main()
