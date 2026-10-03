"""Post-send quality signals and rolling metrics, not subjective style proof."""

from datetime import timedelta

from shadow.storage import iso


def metrics(storage, now):
    cutoff = iso(now - timedelta(days=7))
    turns = storage.conn.execute(
        "SELECT COUNT(DISTINCT decision_id) FROM outbox WHERE state = 'confirmed' "
        "AND authority = 'autonomous' AND confirmed_at >= ?", (cutoff,)).fetchone()[0]
    confusing = storage.conn.execute(
        "SELECT COUNT(DISTINCT q.decision_id) FROM quality_signals q JOIN outbox o ON o.decision_id = q.decision_id "
        "WHERE q.signal = 'confusion' AND q.created_at >= ? AND o.authority = 'autonomous' AND o.state = 'confirmed'",
        (cutoff,)).fetchone()[0]
    approvals = {row[0]: row[1] for row in storage.conn.execute(
        "SELECT state, COUNT(*) FROM approval_requests WHERE created_at >= ? GROUP BY state", (cutoff,))}
    return {"autonomous_turns_7d": turns, "confusing_turns_7d": confusing,
            "confusion_rate": confusing / turns if turns else None, "approval_states_7d": approvals}


def record_signals(coordinator, cid, revision, signals):
    selected = set(signals) & {"confusion", "automation_question"}
    if not selected:
        return
    storage, now = coordinator.storage, coordinator.clock.now()
    latest = storage.conn.execute("SELECT decision_id FROM outbox WHERE conversation_id = ? AND state = 'confirmed' "
                                  "ORDER BY confirmed_at DESC, id DESC LIMIT 1", (cid,)).fetchone()
    if latest is None:
        return
    with storage.transaction() as conn:
        for signal in selected:
            conn.execute("INSERT OR IGNORE INTO quality_signals(conversation_id, inbound_revision, decision_id, signal, created_at) "
                         "VALUES (?, ?, ?, ?, ?)", (cid, revision, latest[0], signal, iso(now)))
        storage.cancel_outbox_items(cid, "quality_signal")
        storage.raise_alert(dedupe_key=f"quality:{cid}:{revision}", tier="action",
                            reason="quality_signal", conversation_id=cid)
    rate_percent, window, minimum = coordinator.settings.confusion_downgrade
    cutoff = iso(now - timedelta(seconds=window))
    turns = storage.conn.execute("SELECT COUNT(DISTINCT decision_id) FROM outbox WHERE state = 'confirmed' "
                                 "AND authority = 'autonomous' AND confirmed_at >= ?", (cutoff,)).fetchone()[0]
    confusing = storage.conn.execute("SELECT COUNT(DISTINCT q.decision_id) FROM quality_signals q "
        "JOIN outbox o ON o.decision_id = q.decision_id WHERE q.signal = 'confusion' AND q.created_at >= ? "
        "AND o.authority = 'autonomous' AND o.state = 'confirmed'", (cutoff,)).fetchone()[0]
    if coordinator.mode == "autonomous" and turns >= minimum and confusing * 100 > turns * rate_percent:
        coordinator.set_mode("assisted")
        storage.raise_alert(dedupe_key=f"quality_downgrade:{iso(now)[:10]}", tier="action",
                            reason="quality_downgrade")
