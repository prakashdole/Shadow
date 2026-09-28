"""Alignment tests (S02, D40, C04).

The cases that matter are the ones where guessing would silently invent or lose a
message: a conversation that repeats itself, a window that does not reach stored
history, and a window that arrives in a different order. Everything else is
arithmetic.
"""

from __future__ import annotations

import unittest

from shadow import alignment as A
from shadow.contracts import ObservedEvent


def k(sender: str, text: str, kind: str = "text") -> A.Key:
    return A.key_of(sender, kind, text)


def observed(sender: str, text: str, kind: str = "text", position: int = 0) -> ObservedEvent:
    return ObservedEvent(position=position, sender=sender, kind=kind, text=text)


class TestNormalize(unittest.TestCase):
    def test_whitespace_is_collapsed_and_trimmed(self) -> None:
        self.assertEqual(A.normalize_text("  kal\n\t milte  hai? "), "kal milte hai?")
        self.assertEqual(A.normalize_text("a  b"), "a b")
        self.assertEqual(A.normalize_text("a b"), "a b")

    def test_unicode_is_normalized_to_nfc_so_one_message_has_one_key(self) -> None:
        import unicodedata

        composed = "caf\u00e9"                     # precomposed e-acute
        decomposed = unicodedata.normalize("NFD", composed)   # e + combining acute
        self.assertNotEqual(decomposed, composed)
        self.assertEqual(A.normalize_text(decomposed), A.normalize_text(composed))
        self.assertEqual(A.key_of("contact", "text", decomposed), A.key_of("contact", "text", composed))

    def test_case_is_not_folded(self) -> None:
        # "OK" and "ok" are different messages; folding them would merge a
        # shouted reply into a quiet one.
        self.assertNotEqual(A.normalize_text("OK"), A.normalize_text("ok"))

    def test_none_and_empty_normalize_to_empty(self) -> None:
        self.assertEqual(A.normalize_text(None), "")
        self.assertEqual(A.normalize_text(""), "")

    def test_kind_is_part_of_the_key(self) -> None:
        self.assertNotEqual(
            A.key_of("contact", "text", "ha"), A.key_of("contact", "media", "ha")
        )


class TestKeysOf(unittest.TestCase):
    def test_reads_both_storage_rows_and_observed_events(self) -> None:
        rows = [{"sender": "contact", "kind": "text", "text": "one"}]
        events = [observed("contact", "one", position=0)]
        self.assertEqual(A.keys_of(rows), A.keys_of(events))

    def test_a_row_missing_a_field_becomes_unknown_not_a_guess(self) -> None:
        self.assertEqual(A.keys_of([{}]), [("unknown", "unknown", "")])
        self.assertEqual(A.keys_of([{"text": "hi"}]), [("unknown", "unknown", "hi")])


class TestBaseline(unittest.TestCase):
    def test_empty_store_makes_the_whole_window_history(self) -> None:
        window = [k("contact", "hi"), k("owner", "hello"), k("contact", "how are you")]
        result = A.align([], window)
        self.assertEqual(result.status, "baseline")
        self.assertEqual(result.new_start_index, 0)
        self.assertFalse(result.is_new)
        # baseline is not "new": the caller stores it as history, never as work
        self.assertEqual(result.new_events(window), [])

    def test_baseline_of_an_empty_window_is_still_baseline(self) -> None:
        self.assertEqual(A.align([], []).status, "baseline")


class TestOverlap(unittest.TestCase):
    def setUp(self) -> None:
        self.stored = [
            k("contact", "kal milte hai?"),
            k("owner", "haan, 7 pm?"),
            k("contact", "parso?"),
        ]

    def test_the_window_repeats_the_tail_then_adds(self) -> None:
        window = self.stored + [k("contact", "ok"), k("contact", "ok")]
        result = A.align(self.stored, window)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.new_start_index, 3)
        self.assertEqual(result.new_events(window), [k("contact", "ok"), k("contact", "ok")])

    def test_an_identical_window_adds_nothing(self) -> None:
        result = A.align(self.stored, self.stored)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.new_start_index, len(self.stored))
        self.assertEqual(result.new_events(self.stored), [])

    def test_an_empty_window_adds_nothing_and_is_not_a_gap(self) -> None:
        result = A.align(self.stored, [])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.new_events([]), [])

    def test_a_single_matching_event_is_enough(self) -> None:
        window = [k("contact", "parso?"), k("contact", "ok")]
        result = A.align(self.stored, window)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.new_start_index, 1)
        self.assertEqual(result.candidates, (1,))

    def test_a_window_that_re_reads_older_history_holds_rather_than_guessing(self) -> None:
        # Documented limitation: alignment matches the window PREFIX against the
        # stored SUFFIX, so a read that returns MORE history than was stored
        # cannot be placed. It is a gap, not a silent merge, and the caller holds
        # the thread. Recorded in D40 with its upgrade path.
        window = [k("owner", "hello"), k("contact", "hey")] + self.stored + [k("contact", "ok")]
        result = A.align(self.stored, window)
        self.assertEqual(result.status, "gap")
        self.assertEqual(result.new_events(window), [])


class TestNoOverlap(unittest.TestCase):
    def test_a_window_that_does_not_reach_stored_history_is_a_gap(self) -> None:
        stored = [k("contact", "one"), k("contact", "two"), k("contact", "three")]
        window = [k("contact", "seven"), k("contact", "eight")]
        result = A.align(stored, window)
        self.assertEqual(result.status, "gap")
        self.assertEqual(result.new_start_index, -1)
        self.assertEqual(result.new_events(window), [])

    def test_a_gap_stores_nothing(self) -> None:
        result = A.align([k("contact", "a")], [k("contact", "b")])
        self.assertFalse(result.is_new)

    def test_an_empty_stored_tail_with_a_window_is_baseline_not_gap(self) -> None:
        self.assertEqual(A.align([], [k("contact", "a")]).status, "baseline")


class TestRepeats(unittest.TestCase):
    def test_pure_repeats_are_ambiguous_rather_than_guessed(self) -> None:
        # A stored run of two identical messages plus a window of three matches
        # at k=1 and k=2. Either answer invents or drops a real message.
        stored = [k("contact", "ha"), k("contact", "ha")]
        window = [k("contact", "ha"), k("contact", "ha"), k("contact", "ha")]
        result = A.align(stored, window)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.new_start_index, -1)
        self.assertEqual(result.candidates, (1, 2))
        self.assertEqual(result.new_events(window), [])

    def test_a_longer_run_of_repeats_stays_ambiguous_without_a_distinct_anchor(self) -> None:
        stored = [k("contact", "ha"), k("contact", "ha"), k("contact", "ha")]
        window = [k("contact", "ha")] * 5
        result = A.align(stored, window)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.candidates, (1, 2, 3))

    def test_repeats_with_a_distinct_anchor_align_on_the_longest_overlap(self) -> None:
        stored = [k("contact", "a"), k("owner", "b"), k("contact", "a")]
        window = [k("contact", "a"), k("owner", "b"), k("contact", "a"), k("contact", "ha"), k("contact", "ha")]
        result = A.align(stored, window)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.new_start_index, 3)
        self.assertEqual(result.new_events(window), [k("contact", "ha"), k("contact", "ha")])

    def test_an_overlap_of_two_identical_keys_is_still_a_guess(self) -> None:
        stored = [k("contact", "a"), k("contact", "a")]
        window = [k("contact", "a"), k("contact", "a"), k("contact", "a")]
        result = A.align(stored, window)
        self.assertEqual(result.status, "ambiguous")
        # The thresholds are the documented ones: an overlap must be at least
        # this long AND carry this many distinct keys.
        self.assertEqual(A.MIN_TRUSTED_OVERLAP, 3)
        self.assertEqual(A.MIN_DISTINCT_KEYS, 2)

    def test_a_single_stored_repeat_aligns_unambiguously(self) -> None:
        # One stored "ha" and three in the window is not ambiguous: only k=1 can
        # match, so the two later ones are plainly new.
        result = A.align([k("contact", "ha")], [k("contact", "ha")] * 3)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.new_start_index, 1)

    def test_a_repeat_at_a_new_position_is_a_new_event_not_a_duplicate(self) -> None:
        stored = [k("contact", "ok"), k("owner", "done")]
        window = [k("contact", "ok"), k("owner", "done"), k("contact", "ok")]
        result = A.align(stored, window)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.new_start_index, 2)
        # "ok" appeared before and is still one more event.
        self.assertEqual(result.new_events(window), [k("contact", "ok")])


class TestReorderingIsNeverRepaired(unittest.TestCase):
    def test_a_window_whose_tail_no_longer_matches_is_a_gap(self) -> None:
        stored = [k("contact", "one"), k("contact", "two"), k("contact", "three")]
        window = [k("contact", "two"), k("contact", "one"), k("contact", "three")]
        self.assertEqual(A.align(stored, window).status, "gap")

    def test_added_events_keep_the_windows_own_order_and_are_never_sorted(self) -> None:
        # Documented limitation: a reorder that still ends on the newest stored
        # event is indistinguishable from a window with new messages after it,
        # because there is no per-message id to consult (D31). What must hold is
        # that nothing is reordered: the added keys are the window's tail, in
        # the window's order.
        stored = [k("contact", "one"), k("contact", "two"), k("contact", "three")]
        window = [k("contact", "three"), k("contact", "one"), k("contact", "two")]
        result = A.align(stored, window)
        self.assertEqual(result.status, "ok")
        self.assertEqual(
            result.new_events(window), [k("contact", "one"), k("contact", "two")]
        )
        self.assertEqual(result.new_events(window), list(window[1:]))

    def test_sender_order_matters_for_the_overlap(self) -> None:
        stored = [k("contact", "same"), k("owner", "same")]
        window = [k("owner", "same"), k("contact", "same")]
        result = A.align(stored, window)
        # The overlapping key is the same text with a different sender, and the
        # event taken as new keeps its own sender rather than inheriting one.
        self.assertEqual(result.new_events(window), [k("contact", "same")])


class TestUnknownSender(unittest.TestCase):
    def test_an_unknown_sender_event_keeps_its_position(self) -> None:
        stored = [
            k("contact", "one"),
            k("unknown", "muddled"),
            k("owner", "two"),
        ]
        window = stored + [k("unknown", "muddled again")]
        result = A.align(stored, window)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.new_start_index, 3)
        # The unknown event in the overlap stayed in place; it was not dropped
        # as noise nor promoted to a known sender.
        self.assertEqual(result.new_events(window), [k("unknown", "muddled again")])

    def test_an_unknown_sender_never_counts_as_the_owner(self) -> None:
        stored = [k("unknown", "x")]
        self.assertEqual(A.trailing_unanswered_count(stored, 0), 0)

    def test_unknown_kind_never_aligns_with_a_known_kind(self) -> None:
        # Same sender, same text, different kind: still not the same event, and
        # the window is held rather than aligned onto a bubble of another kind.
        stored = [k("contact", "same", kind="unknown")]
        window = [k("contact", "same", kind="text"), k("contact", "same", kind="unknown")]
        result = A.align(stored, window)
        self.assertEqual(result.status, "gap")
        self.assertEqual(result.new_events(window), [])


class TestTrailingUnanswered(unittest.TestCase):
    def test_counts_contact_events_after_the_last_owner_event(self) -> None:
        stored = [
            k("owner", "hi"),
            k("contact", "how are you"),
            k("contact", "you there?"),
        ]
        self.assertEqual(A.trailing_unanswered_count(stored, 0), 2)

    def test_zero_when_the_owner_spoke_last(self) -> None:
        stored = [k("contact", "hi"), k("owner", "hello")]
        self.assertEqual(A.trailing_unanswered_count(stored, 0), 0)

    def test_zero_when_the_owner_spoke_first_and_nothing_followed(self) -> None:
        self.assertEqual(A.trailing_unanswered_count([k("owner", "hello")], 0), 0)

    def test_only_contact_events_count(self) -> None:
        stored = [k("owner", "hi"), k("unknown", "system note"), k("contact", "yo")]
        self.assertEqual(A.trailing_unanswered_count(stored, 0), 1)

    def test_an_empty_history_has_nothing_trailing(self) -> None:
        self.assertEqual(A.trailing_unanswered_count([], 0), 0)


class TestObservedEventWindows(unittest.TestCase):
    def test_a_real_window_of_observed_events_aligns(self) -> None:
        stored = [k("contact", "kal milte hai?"), k("owner", "haan, 7 pm?")]
        window = A.keys_of(
            [
                observed("contact", "kal milte hai?", position=0),
                observed("owner", "haan, 7 pm?", position=1),
                observed("contact", "parso?", position=2),
            ]
        )
        result = A.align(stored, window)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.new_events(window), [k("contact", "parso?")])

    def test_an_emoji_only_message_still_has_a_key(self) -> None:
        stored = [k("owner", "haan, 7 pm?")]
        window = A.keys_of([observed("owner", "haan, 7 pm?"), observed("contact", "😂")])
        result = A.align(stored, window)
        self.assertEqual(result.new_events(window), [k("contact", "😂")])


if __name__ == "__main__":
    unittest.main()
