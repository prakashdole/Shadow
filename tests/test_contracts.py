"""The shared contracts must match the storage CHECK constraints."""

import re
import unittest
from pathlib import Path

from shadow import contracts, storage


def _check_values(column: str) -> set[str]:
    match = re.search(column + r"[^\n]*\n?[^\n]*CHECK \(" + column + r" IN \(([^)]*)\)", storage.SCHEMA_V1)
    if match is None:
        match = re.search(r"CHECK \(" + column + r" IN \(([^)]*)\)", storage.SCHEMA_V1)
    assert match, f"no CHECK for {column}"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


class TestContractsMatchStorage(unittest.TestCase):
    def test_sender_and_kind(self):
        self.assertEqual(set(contracts.Sender.__args__), _check_values("sender"))
        self.assertEqual(set(contracts.Kind.__args__), _check_values("kind"))

    def test_send_statuses_are_outbox_states(self):
        self.assertTrue(set(contracts.SEND_STATUSES) <= set(storage.ALLOWED))

    def test_global_pause_statuses_are_read_statuses(self):
        self.assertTrue(contracts.GLOBAL_PAUSE_STATUSES <= set(contracts.READ_STATUSES))

    def test_dataclasses_are_frozen(self):
        event = contracts.ObservedEvent(position=0, sender="contact", kind="text", text="kal?")
        with self.assertRaises(Exception):
            event.text = "changed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
