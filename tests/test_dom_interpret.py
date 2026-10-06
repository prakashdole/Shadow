"""tests/test_dom_interpret.py — pure-Python interpretation, no browser.

The browser tests in tests/browser/ prove the JavaScript extracts the structure
this module expects. This file proves the DECISIONS are right, and it needs no
browser and no private file, so a failure here is always a rule failure.
"""

from __future__ import annotations

import unittest

from shadow.dom import (
    classify_preview, inbox_status, interpret_inbox, interpret_read, normalize,
    page_status, read_coverage, same_thread, strip_age, thread_id_from_url,
)
from shadow.contracts import ReadResult


class CoverageTest(unittest.TestCase):
    def test_collection_interruption_is_not_only_owner_takeover(self):
        cases = [
            ('history_owner_interrupted', True, True),
            ('history_pane_replaced', True, False),
            ('history_view_replaced', True, False),
            ('thread_changed_during_history', True, False),
            ('thread_changed_during_read', True, False),
            ('history_count_reached', False, False),
            ('history_load_unsettled', False, False),
        ]
        for detail, interrupted, owner in cases:
            with self.subTest(detail=detail):
                result = read_coverage(ReadResult('dom_unknown', detail=detail), 10)
                self.assertEqual(result.coverage['collection_interrupted'], interrupted)
                self.assertEqual(result.coverage['owner_interrupted'], owner)
                self.assertFalse(result.coverage['requested_count_reached'])

    def test_existing_interruption_evidence_survives_normalization(self):
        result = read_coverage(ReadResult('dom_unknown', detail='history_view_restore_failed',
                                         coverage={'collection_interrupted': True}), 10)
        self.assertTrue(result.coverage['collection_interrupted'])

    def test_merged_history_counts_are_derived_from_returned_events(self):
        result = interpret_read(build([('a', 'one', 'contact'), ('a', 'two', 'owner')]), '123')
        from dataclasses import replace
        result = read_coverage(replace(result, coverage={**result.coverage,
                                                       'rendered_count': 1}), 2)
        self.assertEqual(result.coverage['rendered_count'], 2)
        self.assertEqual(result.coverage['collected_readable_count'], 2)

PANE = {"left": 0.0, "right": 1000.0, "width": 1000.0}


def raw(**kw) -> dict:
    base = {
        "path": "/direct/t/123/",
        "list_present": True,
        "rows": [],
        "pane_present": True,
        "pane": dict(PANE),
        "at_start": True,
        "header": {"profile_links": 1, "avatars": 1},
        "articles": [],
        "outside": [],
        "article_orders": [],
        "composer_text": "",
        "seen": False,
    }
    base.update(kw)
    return base


def build(items, **kw) -> dict:
    """Interleave articles and outside leaves into the raw reader shape.

    items: ("a", text, "owner"|"contact"|"unlaid") or ("o", text, width, laid).
    """
    articles, outs, orders = [], [], []
    for order, item in enumerate(items):
        if item[0] == "a":
            orders.append(order)
            unlaid = item[2] == "unlaid"
            left = 0.0 if unlaid else (600.0 if item[2] == "owner" else 100.0)
            articles.append({
                "left": left, "width": 0.0 if unlaid else 300.0,
                "centre": 0.0 if unlaid else left + 150.0, "laid": not unlaid,
                "top": order * 10, "text": item[1], "media_label": None,
                "reply_to_text": None, "edited": False,
            })
        else:
            width, laid = item[2], item[3]
            outs.append({"order": order, "left": 0.0, "width": width,
                         "centre": width / 2, "laid": laid, "text": item[1]})
    return raw(articles=articles, outside=outs, article_orders=orders, **kw)


def row(label, *lines, unread=False) -> dict:
    text = "\n".join((label, *lines))
    if unread:
        text += "\nUnread"
    return {"top": 0, "height": 72, "lines": [label, *lines],
            "full_text": text, "pressed": False, "width": 460.0}


class PreviewTest(unittest.TestCase):
    def test_preview_classes(self) -> None:
        cases = [
            ("You: chalo", "owner_last"),
            ("You sent an attachment", "owner_last"),
            ("You sent a reel", "owner_last"),
            ("Test Contact reacted to your message", "reaction_to_owner"),
            ("Test Contact Two sent an attachment.", "attachment_in"),
            ("Test Contact sent a reel", "attachment_in"),
            ("Test Contact sent a photo", "attachment_in"),
            ("Shared a post", "attachment_in"),
            ("kal milte hai?", "text_in"),
            ("", "unknown"),
            ("   ", "unknown"),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(classify_preview(text), want)

    def test_age_and_separator_are_stripped_from_the_preview(self) -> None:
        self.assertEqual(strip_age("chalo · 5m"), "chalo")
        self.assertEqual(strip_age("chalo 12h"), "chalo")
        self.assertEqual(strip_age("chalo·5m"), "chalo")
        self.assertEqual(strip_age("chalo · 3d"), "chalo")
        self.assertEqual(strip_age("chalo"), "chalo")
        self.assertEqual(strip_age("Reacted 👍 to your message · 14m Unread"),
                         "Reacted 👍 to your message")
        self.assertEqual(classify_preview("This account can't receive your message because of restrictions."),
                         "unknown")

    def test_unread_hint_is_inferred_and_labelled_as_such(self) -> None:
        r = interpret_inbox(raw(rows=[
            row("Alpha", "You: chalo · 5m"),
            row("Beta", "kal milte hai? · 2m"),
            row("Gamma", "You sent an attachment · 1m"),
            row("Delta", "seen before · 9m", unread=True),
        ]))
        self.assertEqual(r.status, "ok")
        by_label = {row_.label: row_ for row_ in r.rows}
        self.assertFalse(by_label["Alpha"].unread_hint)
        self.assertTrue(by_label["Beta"].unread_hint)
        self.assertFalse(by_label["Gamma"].unread_hint)
        self.assertTrue(by_label["Delta"].unread_hint)   # explicit token wins
        for row_ in r.rows:
            self.assertIsNone(row_.group_hint)           # P00b Q7: unknowable

    def test_label_is_unique_across_rows(self) -> None:
        r = interpret_inbox(raw(rows=[
            row("Test Contact", "hello · 1m"),
            row("Test Contact", "hello again · 2m"),
            row("Other Contact", "hi · 3m"),
        ]))
        unique = {row_.label: row_.label_is_unique for row_ in r.rows}
        self.assertEqual(unique, {"Test Contact": False, "Other Contact": True})
        self.assertEqual([r_.row_index for r_ in r.rows], [0, 1, 2])


class SeparatorAndOutsideTest(unittest.TestCase):
    def test_both_separator_formats_are_dropped_by_text(self) -> None:
        for sep in ("12 Sep, 3:04", "Tue 12:03", "Thu 4:58 PM", "7:04\u202fPM", "Today", "Yesterday"):
            with self.subTest(sep=sep):
                r = interpret_read(build([
                    ("a", "hello", "contact"),
                    ("o", sep, 200.0, True),      # narrow: width rule cannot catch it
                    ("a", "world", "contact"),
                ]), "123")
                self.assertEqual([e.text for e in r.events], ["hello", "world"])

    def test_a_wide_outside_leaf_is_a_separator_by_structure(self) -> None:
        r = interpret_read(build([
            ("a", "hello", "contact"),
            ("o", "some full width banner", 980.0, True),
            ("a", "world", "contact"),
        ]), "123")
        self.assertEqual([e.text for e in r.events], ["hello", "world"])

    def test_a_reaction_outside_an_article_is_never_a_message(self) -> None:
        r = interpret_read(build([
            ("a", "hello", "contact"),
            ("o", "Test Contact reacted to your message", 240.0, True),
            ("a", "world", "contact"),
        ]), "123")
        kinds = [(e.sender, e.kind, e.text) for e in r.events]
        self.assertEqual(kinds, [
            ("contact", "text", "hello"),
            ("unknown", "reaction", "Test Contact reacted to your message"),
            ("contact", "text", "world"),
        ])

    def test_replied_to_attaches_to_the_next_article(self) -> None:
        r = interpret_read(build([
            ("a", "first", "contact"),
            ("o", "Test Contact replied to you · 2m", 200.0, True),
            ("a", "answering that", "contact"),
            ("a", "mine", "owner"),
        ]), "123")
        self.assertIsNone(r.events[0].reply_to_text)
        self.assertEqual(r.events[1].reply_to_text, "Test Contact replied to you · 2m")
        self.assertIsNone(r.events[2].reply_to_text)

    def test_other_outside_leaves_are_system_events(self) -> None:
        r = interpret_read(build([
            ("o", "Test Contact One liked a message", 180.0, True),
            ("a", "hello", "contact"),
        ]), "123")
        self.assertEqual([e.kind for e in r.events], ["reaction", "text"])

    def test_a_trailing_outside_leaf_is_still_classified(self) -> None:
        r = interpret_read(build([
            ("a", "hello", "contact"),
            ("o", "Test Contact reacted to your message", 240.0, True),
        ]), "123")
        self.assertEqual([e.kind for e in r.events], ["text", "reaction"])


class EventTest(unittest.TestCase):
    def test_two_identical_messages_stay_two_events(self) -> None:
        r = interpret_read(build([
            ("a", "ok", "contact"),
            ("a", "ok", "contact"),
        ]), "123")
        self.assertEqual([e.text for e in r.events], ["ok", "ok"])
        self.assertEqual([e.position for e in r.events], [0, 1])

    def test_sender_comes_from_geometry_only(self) -> None:
        r = interpret_read(build([
            ("a", "mine", "owner"),
            ("a", "theirs", "contact"),
        ]), "123")
        self.assertEqual([e.sender for e in r.events], ["owner", "contact"])

    def test_no_layout_means_unknown_sender_not_a_guess(self) -> None:
        r = interpret_read(build([
            ("a", "unlaid", "unlaid"),
        ]), "123")
        event = r.events[0]
        self.assertEqual(event.sender, "unknown")
        self.assertFalse(event.laid_out)
        # and never remembered from a previous read: no memo exists any more
        again = interpret_read(build([("a", "unlaid", "unlaid")]), "123")
        self.assertEqual(again.events[0].sender, "unknown")

    def test_media_labels(self) -> None:
        for label, text, want_text in (
            ("reel", "Clip", "Clip"),
            ("post", "Post", "Post"),
            ("video", "Video", "Video"),
        ):
            with self.subTest(label=label):
                art = {"left": 100.0, "width": 300.0, "centre": 250.0, "laid": True,
                       "top": 0, "text": text, "media_label": label,
                       "reply_to_text": None, "edited": False}
                r = interpret_read(raw(articles=[art], article_orders=[0]), "123")
                self.assertEqual(r.events[0].kind, "media")
                self.assertEqual(r.events[0].media_label, label)
                self.assertEqual(r.events[0].text, want_text)

    def test_unavailable_media_is_kept_and_labelled(self) -> None:
        for text, label in (("reel unavailable", "reel_unavailable"),
                            ("post unavailable", "post_unavailable")):
            with self.subTest(text=text):
                r = interpret_read(build([("a", text, "contact")]), "123")
                self.assertEqual(len(r.events), 1, "an expired share must not vanish")
                self.assertEqual(r.events[0].kind, "media")
                self.assertEqual(r.events[0].media_label, label)
                self.assertEqual(r.events[0].text, text)

    def test_empty_article_is_unknown_not_dropped(self) -> None:
        r = interpret_read(build([("a", "", "contact")]), "123")
        self.assertEqual(len(r.events), 1)
        self.assertEqual(r.events[0].kind, "unknown")


class BindingTest(unittest.TestCase):
    def test_group_evidence(self) -> None:
        cases = [
            ({"profile_links": 3, "avatars": 2}, True),
            ({"profile_links": 5, "avatars": 1}, True),
            ({"profile_links": 2, "avatars": 1}, False),
            ({"profile_links": 1, "avatars": 1}, False),
            ({"profile_links": 0, "avatars": 0}, None),
            ({"profile_links": 1, "avatars": 0}, None),
        ]
        for header, want in cases:
            with self.subTest(header=header):
                r = interpret_read(build([("a", "hi", "contact")], header=header), "123")
                self.assertIs(r.is_group, want)
                self.assertEqual(r.participant_links, header["profile_links"])

    def test_composer_text_is_reported(self) -> None:
        r = interpret_read(build([("a", "hi", "contact")], composer_text="  typed by hand "), "123")
        self.assertEqual(r.composer_text, "typed by hand")
        self.assertEqual(interpret_read(build([("a", "hi", "contact")]), "123").composer_text, "")

    def test_complete_only_when_the_window_reached_the_start(self) -> None:
        self.assertTrue(interpret_read(build([("a", "hi", "contact")], at_start=True), "1").complete)
        self.assertFalse(interpret_read(build([("a", "hi", "contact")], at_start=False), "1").complete)


class StatusTest(unittest.TestCase):
    def test_page_status_from_the_path(self) -> None:
        self.assertEqual(page_status("/accounts/login/"), "login_required")
        self.assertEqual(page_status("/direct/t/12/challenge/"), "challenge")
        self.assertEqual(page_status("/checkpoint/"), "challenge")
        self.assertEqual(page_status("/accounts/suspended/"), "challenge")
        self.assertEqual(page_status("/suspended/"), "challenge")
        self.assertIsNone(page_status("/direct/inbox/"))

    def test_missing_thread_list_is_dom_unknown_never_empty(self) -> None:
        r = interpret_inbox(raw(list_present=False, rows=[]))
        self.assertEqual(r.status, "dom_unknown")
        self.assertEqual(r.rows, ())

    def test_rendered_list_with_no_rows_is_verified_empty(self) -> None:
        r = interpret_inbox(raw(list_present=True, rows=[]))
        self.assertEqual(r.status, "empty_verified")

    def test_missing_pane_is_dom_unknown_never_empty(self) -> None:
        r = interpret_read(raw(pane_present=False), "123")
        self.assertEqual(r.status, "dom_unknown")
        self.assertEqual(r.events, ())

    def test_a_seen_leaf_is_receipts_visible(self) -> None:
        r = interpret_read(raw(seen=True), "123")
        self.assertEqual(r.status, "receipts_visible")
        self.assertIn("receipts_visible", __import__(
            "shadow.contracts", fromlist=["GLOBAL_PAUSE_STATUSES"]
        ).GLOBAL_PAUSE_STATUSES)

    def test_inbox_status_helper(self) -> None:
        self.assertEqual(inbox_status(raw(path="/accounts/login/"))[0], "login_required")
        self.assertEqual(inbox_status(raw(path="/challenge/"))[0], "challenge")
        self.assertEqual(inbox_status(raw(rows=[row("A", "b · 1m")]))[0], "ok")
        self.assertEqual(inbox_status(raw(seen=True, rows=[row("A", "b · 1m")]))[0],
                         "receipts_visible")


class ThreadIdTest(unittest.TestCase):
    def test_only_direct_t_digits_is_a_thread(self) -> None:
        self.assertEqual(thread_id_from_url("https://www.instagram.com/direct/t/12345/"), "12345")
        self.assertEqual(thread_id_from_url("/direct/t/12/"), "12")
        self.assertIsNone(thread_id_from_url("/direct/inbox/"))
        self.assertIsNone(thread_id_from_url("/direct/t/abc/"))
        self.assertIsNone(thread_id_from_url("/direct/t/"))
        self.assertIsNone(thread_id_from_url(""))

    def test_id_prefix_collision_is_not_equality(self) -> None:
        self.assertTrue(same_thread("12", "12"))
        self.assertFalse(same_thread("12", "123"))
        self.assertFalse(same_thread("123", "12"))
        self.assertFalse(same_thread(None, "12"))
        self.assertFalse(same_thread("", ""))
        # the legacy reader asked `wanted_id in page_url`, which is substring
        # containment: asking for thread 12 while 12345 is open matched.
        self.assertNotEqual("https://www.instagram.com/direct/t/12345/".find("12"), -1)

    def test_normalize_is_nfc_and_whitespace_only(self) -> None:
        self.assertEqual(normalize("  a\n b  "), "a b")
        self.assertEqual(normalize("é"), "é")     # composed
        self.assertEqual(normalize("OK"), "OK")   # never case-folded


if __name__ == "__main__":
    unittest.main()
