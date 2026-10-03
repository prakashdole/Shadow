"""Scoped canonical memory. Model extraction creates candidates, never authority."""

from __future__ import annotations

import json
from datetime import timedelta

from shadow.storage import iso, parse_iso, utcnow

STYLE_RULES = {
    "emoji": {"avoid": "Avoid emoji in ordinary replies.", "contextual": "Use emoji only when context warrants it."},
    "language": {"english": "Prefer English unless the contact explicitly requests another language.",
                 "hinglish": "Use natural Latin-script Hinglish when appropriate.",
                 "mirror": "Match the contact's language without forcing a mixture."},
    "length": {"concise": "Be concise while answering the actual question.",
               "contextual": "Use the length needed for a useful reply."},
    "profanity": {"avoid": "Avoid profanity.", "contextual": "Do not force profanity; follow established relationship boundaries."},
    "tone": {"serious": "Prefer straightforward, serious replies.",
             "contextual": "Let humour and seriousness follow the conversation."},
}


class ContextMemory:
    def __init__(self, storage):
        self.storage = storage

    def revision(self, cid):
        versions = dict(self.storage.conn.execute("SELECT scope, revision FROM memory_versions"))
        return f"{versions.get('global', 0)}:{versions.get(str(cid), 0)}"

    def invalidate(self, cid=None):
        scope = "global" if cid is None else str(cid)
        with self.storage.transaction() as conn:
            conn.execute("INSERT INTO memory_versions(scope, revision) VALUES (?, 1) "
                         "ON CONFLICT(scope) DO UPDATE SET revision = revision + 1", (scope,))
            if cid is None:
                ids = [row[0] for row in conn.execute("SELECT id FROM conversations")]
            else:
                ids = [cid]
            for target in ids:
                self.storage.cancel_outbox_items(target, "memory_changed")
                self.storage.cancel_decisions(target, ("recorded", "awaiting_approval", "approved", "queued"))
                work = self.storage.get_work(target)
                if work:
                    self.storage.update_work(target, cancel_generation=work["cancel_generation"] + 1,
                                             state="held", held_reason="memory_changed")

    def correct(self, cid, dimension, value, update_id):
        if dimension not in STYLE_RULES or value not in STYLE_RULES[dimension]:
            raise ValueError("Unsupported style rule; safety and capability boundaries cannot be changed.")
        with self.storage.transaction() as conn:
            prior = conn.execute("SELECT id FROM correction_rules WHERE conversation_id IS ? "
                                 "AND dimension = ? AND active = 1 ORDER BY id DESC LIMIT 1",
                                 (cid, dimension)).fetchone()
            conn.execute("UPDATE correction_rules SET active = 0 WHERE conversation_id IS ? AND dimension = ?",
                         (cid, dimension))
            cursor = conn.execute("INSERT INTO correction_rules(conversation_id, dimension, value_text, "
                                  "source_update_id, created_at, supersedes_id) VALUES (?, ?, ?, ?, ?, ?)",
                                  (cid, dimension, value, update_id, utcnow(), prior[0] if prior else None))
            self.invalidate(cid)
            return cursor.lastrowid

    def undo(self, rule_id):
        with self.storage.transaction() as conn:
            rule = conn.execute("SELECT * FROM correction_rules WHERE id = ? AND active = 1", (rule_id,)).fetchone()
            if not rule:
                return False
            conn.execute("UPDATE correction_rules SET active = 0 WHERE id = ?", (rule_id,))
            if rule["supersedes_id"]:
                conn.execute("UPDATE correction_rules SET active = 1 WHERE id = ?", (rule["supersedes_id"],))
            self.invalidate(rule["conversation_id"])
            return True

    def rules(self, cid):
        rows = self.storage.conn.execute("SELECT * FROM correction_rules WHERE active = 1 "
                                         "AND (conversation_id IS NULL OR conversation_id = ?) ORDER BY id", (cid,))
        selected = {}
        local = {}
        for row in rows:
            (local if row["conversation_id"] is not None else selected)[row["dimension"]] = row
        selected.update(local)
        return tuple(STYLE_RULES[row["dimension"]][row["value_text"]] for row in selected.values())

    def save_candidates(self, cid, facts, allowed_ids):
        if type(facts) not in (list, tuple) or len(facts) > 5:
            raise ValueError("At most five candidate facts per batch")
        events = {event["id"]: event for event in self.storage.get_events(cid)}
        conversation = self.storage.get_conversation(cid)
        if not conversation or conversation["contact_status"] not in ("known", "approved_unknown"):
            return 0
        inserted = 0
        for fact in facts:
            try:
                if type(fact) is not dict or set(fact) != {"subject_id", "kind", "value_text", "source_event_ids"}:
                    continue
                sources, value = fact["source_event_ids"], fact["value_text"]
                if (fact["subject_id"] != f"contact:{cid}" or fact["kind"] not in ("preference", "plan", "relationship")
                        or type(value) is not str or not value.strip() or len(value) > 300
                        or type(sources) is not list or not sources or len(set(sources)) != len(sources)
                        or any(type(source) is not int or source not in allowed_ids or source not in events
                               for source in sources)):
                    continue
                evidence = [events[source] for source in sources]
                if any(event["sender"] != "contact" or event["kind"] != "text" or event["reply_to"]
                       or event["source"] != "dom" or event["origin"] == "shadow_generated" for event in evidence):
                    continue
                # Exact reported text only, not a model paraphrase claiming new facts.
                if not any(value in (event["text"] or "") for event in evidence):
                    continue
                observed = max(event["observed_at"] for event in evidence)
                moment = parse_iso(observed)
                if moment is None:
                    continue
                until = iso(moment + timedelta(days=7)) if fact["kind"] == "plan" else None
                with self.storage.transaction() as conn:
                    cursor = conn.execute("INSERT OR IGNORE INTO memory_facts(conversation_id, subject_id, "
                                          "kind, value_text, source_event_ids, observed_at, valid_until) "
                                          "VALUES (?, ?, ?, ?, ?, ?, ?)", (cid, fact["subject_id"], fact["kind"],
                                          value, json.dumps(sorted(sources)), observed, until))
                    inserted += cursor.rowcount
            except (TypeError, ValueError):
                continue
        return inserted

    def facts(self, cid, now, *, candidates=False):
        states = ("active", "candidate", "uncertain") if candidates else ("active",)
        rows = self.storage.conn.execute("SELECT * FROM memory_facts WHERE conversation_id = ? "
                                         "AND status IN (" + ",".join("?" for _ in states) + ") ORDER BY id", (cid, *states))
        return [dict(row) for row in rows if (not row["valid_from"] or parse_iso(row["valid_from"]) <= now)
                and (not row["valid_until"] or parse_iso(row["valid_until"]) > now)]

    def accept(self, fact_id, now):
        with self.storage.transaction() as conn:
            fact = conn.execute("SELECT * FROM memory_facts WHERE id = ? AND status = 'candidate'", (fact_id,)).fetchone()
            if not fact or fact["valid_until"] and parse_iso(fact["valid_until"]) <= now:
                return False
            # A contrary claim requires explicit owner review, not last-write-wins.
            conflicting = conn.execute("SELECT 1 FROM memory_facts WHERE conversation_id = ? "
                                       "AND kind = ? AND status = 'active' AND value_text != ?",
                                       (fact["conversation_id"], fact["kind"], fact["value_text"])).fetchone()
            conn.execute("UPDATE memory_facts SET status = ? WHERE id = ?",
                         ("uncertain" if conflicting else "active", fact_id))
            self.invalidate(fact["conversation_id"])
            return not conflicting

    def expire(self, now):
        with self.storage.transaction() as conn:
            ids = [row[0] for row in conn.execute("SELECT DISTINCT conversation_id FROM memory_facts "
                                                "WHERE valid_until <= ? AND status IN ('active','candidate','uncertain')", (iso(now),))]
            conn.execute("UPDATE memory_facts SET status = 'expired' WHERE valid_until <= ? "
                         "AND status IN ('active','candidate','uncertain')", (iso(now),))
            for cid in ids:
                self.invalidate(cid)

    def forget(self, kind, record_id):
        table = {"fact": "memory_facts", "rule": "correction_rules"}.get(kind)
        if not table:
            raise ValueError("Forget supports exact fact or rule IDs")
        with self.storage.transaction() as conn:
            row = conn.execute(f"SELECT conversation_id FROM {table} WHERE id = ?", (record_id,)).fetchone()
            if row is None:
                return False
            # No value retained in a tombstone or command result.
            if kind == "rule":
                conn.execute("UPDATE correction_rules SET supersedes_id = NULL WHERE supersedes_id = ?", (record_id,))
            else:
                conn.execute("UPDATE memory_facts SET supersedes_id = NULL WHERE supersedes_id = ?", (record_id,))
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
            self.invalidate(row[0])
            return True

    def bundle(self, cid, now):
        self.expire(now)
        profile = self.storage.conn.execute("SELECT profile_json FROM owner_profile WHERE id = 1").fetchone()
        facts = tuple({key: row[key] for key in ("id", "value_text", "kind", "source_event_ids", "confidence_label", "valid_until")}
                      for row in self.facts(cid, now))
        return {"owner_profile": json.loads(profile[0]) if profile else {}, "corrections": self.rules(cid),
                "facts": facts, "memory_revision": self.revision(cid)}

    def set_profile(self, profile):
        allowed = {"preferred_name", "language", "style", "routine", "current_update"}
        if type(profile) is not dict or not set(profile) <= allowed or len(json.dumps(profile)) > 8000:
            raise ValueError("Invalid owner profile fields or size")
        if any(type(profile.get(key)) is not str for key in ("preferred_name", "language", "style") if key in profile):
            raise ValueError("Profile text fields must be strings")
        with self.storage.transaction() as conn:
            conn.execute("INSERT INTO owner_profile(id, profile_json, updated_at) VALUES(1, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET profile_json = excluded.profile_json, "
                         "updated_at = excluded.updated_at, revision = revision + 1", (json.dumps(profile), utcnow()))
            self.invalidate()
