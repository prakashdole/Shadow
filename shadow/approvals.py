"""Assisted requests: exact decision identity, expiry and revision revalidation.

Only enqueue here. The coordinator re-reads the real thread before any input.
Live runners keep submission locked until qualification and authorization exist.
"""

import json
from datetime import timedelta

from shadow.storage import iso, parse_iso


class Approvals:
    def __init__(self, coordinator, *, allow_submission=False):
        self.coordinator = coordinator
        self.storage = coordinator.storage
        self.allow_submission = allow_submission

    def create(self, decision_id):
        now = self.coordinator.clock.now()
        with self.storage.transaction() as conn:
            decision = conn.execute("SELECT * FROM decisions WHERE id = ? AND status = 'awaiting_approval'",
                                    (decision_id,)).fetchone()
            if not decision:
                raise ValueError("Decision is not awaiting approval")
            conn.execute("UPDATE approval_requests SET state = 'superseded' "
                         "WHERE conversation_id = ? AND state IN ('pending','editing')",
                         (decision["conversation_id"],))
            conn.execute("INSERT OR IGNORE INTO approval_requests(decision_id, conversation_id, created_at, expires_at) "
                         "VALUES (?, ?, ?, ?)", (decision_id, decision["conversation_id"], iso(now),
                         iso(now + timedelta(seconds=self.coordinator.settings.draft_ttl))))
            return conn.execute("SELECT id FROM approval_requests WHERE decision_id = ?", (decision_id,)).fetchone()[0]

    def pending(self):
        self.expire()
        return [dict(row) for row in self.storage.conn.execute(
            "SELECT a.*, d.record_json, d.source_revision FROM approval_requests a "
            "JOIN decisions d ON d.id = a.decision_id WHERE a.state = 'pending' "
            "AND d.status = 'awaiting_approval' ORDER BY a.id")]

    def expire(self):
        now = self.coordinator.clock.now()
        with self.storage.transaction() as conn:
            for row in conn.execute("SELECT a.*, d.source_revision, d.status AS decision_status "
                                    "FROM approval_requests a JOIN decisions d ON d.id = a.decision_id "
                                    "WHERE a.state IN ('pending','editing')").fetchall():
                work = self.storage.get_work(row["conversation_id"])
                stale = (row["decision_status"] != "awaiting_approval" or not work
                         or work["inbound_revision"] != row["source_revision"])
                expired = parse_iso(row["expires_at"]) <= now
                if not stale and not expired:
                    continue
                conn.execute("UPDATE approval_requests SET state = ? WHERE id = ?",
                             ("superseded" if stale else "expired", row["id"]))
                if expired and not stale:
                    self.storage.set_decision_status(row["decision_id"], "expired")
                    self.storage.update_work(row["conversation_id"], state="held", held_reason="approval_expired")
                    self.storage.raise_alert(dedupe_key=f"approval_expired:{row['id']}", tier="info",
                                             reason="approval_expired", conversation_id=row["conversation_id"])

    def act(self, request_id, action, *, message_id=None, replacement=None):
        self.expire()
        with self.storage.transaction() as conn:
            request = conn.execute("SELECT a.*, d.record_json, d.source_revision FROM approval_requests a "
                                   "JOIN decisions d ON d.id = a.decision_id WHERE a.id = ?", (request_id,)).fetchone()
            if (not request or request["state"] not in ("pending", "editing")
                    or message_id is not None and request["message_id"] != message_id):
                return "This draft is out of date or not bound to that approval message."
            cid = request["conversation_id"]
            work = self.storage.get_work(cid)
            conversation = self.storage.get_conversation(cid)
            if (self.coordinator.paused or not conversation
                    or conversation["account_id"] != self.coordinator.account_id
                    or conversation["contact_status"] not in ("known", "approved_unknown")
                    or not work or work["state"] != "awaiting_approval"):
                return "Held: pause, identity or work state changed; no send queued."
            if action == "skip":
                conn.execute("UPDATE approval_requests SET state = 'skipped' WHERE id = ?", (request_id,))
                self.storage.set_decision_status(request["decision_id"], "rejected")
                self.storage.update_work(cid, state="held", held_reason="owner_skip")
                return "Draft skipped; no send queued."
            if action == "edit" and replacement is None:
                conn.execute("UPDATE approval_requests SET state = 'editing' WHERE id = ?", (request_id,))
                return f"Use /edit-draft {request_id} <exact replacement text>. No rule is applied automatically."
            if action not in ("send", "edit"):
                return "Unknown approval action."
            if not self.allow_submission:
                return "Submission locked: model qualification and controlled live authorization are required."
            if self.coordinator.mode == "observe":
                return "Observe mode cannot submit an assisted draft."
            messages = json.loads(request["record_json"])["reply"]["messages"]
            authority = "owner_approved"
            if action == "edit":
                if type(replacement) is not str or not replacement.strip():
                    return "Replacement must be non-empty text."
                messages = [replacement]
                authority = "owner_command"
            settings = self.coordinator.settings
            if len(messages) > settings.burst_max_items or any(len(text) > settings.bubble_max_chars for text in messages):
                return "Text exceeds transport limits; no truncation or send."
            for index, text in enumerate(messages):
                self.storage.add_outbox_item(decision_id=request["decision_id"], item_index=index,
                    conversation_id=cid, text=text, authority=authority, revision=request["source_revision"])
            conn.execute("UPDATE approval_requests SET state = 'approved', owner_edit = ? WHERE id = ?",
                         (replacement, request_id))
            self.storage.set_decision_status(request["decision_id"], "queued")
            due = parse_iso(work["due_at"]) or self.coordinator.clock.now()
            self.storage.update_work(cid, state="due", due_at=iso(max(due, self.coordinator.clock.now())))
            return "Owner text queued for fresh browser, identity, context and transport checks; not yet sent."
