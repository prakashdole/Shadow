"""Canonical storage: one private SQLite database, one process, one writer.

Design rules baked in here (TRD "Required persisted records", "Outbox state
machine", [rules.md] 13-18):

* the database lives in the private data directory, never in the checkout;
* a process lock refuses a second writer, and stale-lock recovery is the
  kernel's job: `flock` is released when the process dies, so a crash never
  leaves a lock that needs manual clearing (the pid inside the file is for
  diagnostics only, never for deciding whether the lock is live);
* the schema is versioned with `PRAGMA user_version` and migrated forward
  only, one transaction per version, with a verified private backup taken
  before touching a database that already holds data;
* corruption is never repaired, never recreated and never "started fresh" —
  a damaged database stops the runtime, because silently re-initialising
  would re-answer old conversations;
* confirmation of a send and the event that proves it are ONE transaction,
  so an outbox item can never read `confirmed` without its sent event.
"""

from __future__ import annotations

import os
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from shadow.settings import data_dir as default_data_dir

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

DB_FILENAME = "shadow.sqlite3"
LOCK_FILENAME = "shadow.lock"
BACKUP_DIR = "backups"
DEFAULT_BUSY_TIMEOUT_MS = 5000

# --- the TRD outbox state machine, exactly ----------------------------
# pending -> attempting -> confirmed
#                      -> failed_before_submit
#                      -> uncertain
# pending -> cancelled          (pause / stale revision / changed policy)
# failed_before_submit -> pending        (bounded retry after fresh checks)
# uncertain -> confirmed / held          (owner decision required)
ALLOWED: dict[str, frozenset[str]] = {
    "pending": frozenset({"attempting", "cancelled"}),
    "attempting": frozenset({"confirmed", "failed_before_submit", "uncertain"}),
    "failed_before_submit": frozenset({"pending"}),
    "uncertain": frozenset({"confirmed", "held"}),
    "confirmed": frozenset(),
    "cancelled": frozenset(),
    "held": frozenset(),
}

# An outbox item only becomes `confirmed` through confirm_send(), which also
# writes the sent event. A bare transition to `confirmed` is refused so the
# state can never be set without its evidence (S04).
CONFIRM_ONLY_TARGETS = frozenset({"confirmed"})

AUTHORITY_ORIGIN = {
    "autonomous": "shadow_generated",
    "owner_approved": "owner_approved",
    "owner_command": "owner_authored",
}

LATENCY_BUCKETS = ("00-06", "06-12", "12-18", "18-24")

# conversation_work.state and decisions.status are provisional: only the
# states P01 can justify are allowed. Later phases extend them by migration
# rather than by relaxing a CHECK in place.
SCHEMA_V1 = """
CREATE TABLE runtime (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    account_id TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'observe'
        CHECK (mode IN ('observe', 'assisted', 'autonomous')),
    autonomy_scope TEXT NOT NULL DEFAULT 'none',
    pause_reason TEXT,
    config_digest TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE conversations (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    thread_id TEXT NOT NULL
        CHECK (thread_id <> '' AND thread_id NOT GLOB '*[^0-9]*'),
    label TEXT,
    contact_status TEXT NOT NULL DEFAULT 'unknown_pending'
        CHECK (contact_status IN ('known', 'unknown_pending', 'approved_unknown', 'excluded')),
    binding_evidence TEXT,
    owner_active_until TEXT,
    last_checked_at TEXT,
    UNIQUE (account_id, thread_id)
);

CREATE TABLE legacy_contacts (
    id INTEGER PRIMARY KEY,
    folder_name TEXT NOT NULL UNIQUE,
    closeness INTEGER,
    relationship TEXT,
    binding_status TEXT NOT NULL DEFAULT 'unbound'
        CHECK (binding_status IN ('unbound', 'bound')),
    bound_conversation_id INTEGER REFERENCES conversations (id),
    bound_evidence TEXT,
    bound_at TEXT
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations (id),
    legacy_contact_id INTEGER REFERENCES legacy_contacts (id),
    sender TEXT NOT NULL CHECK (sender IN ('owner', 'contact', 'unknown')),
    kind TEXT NOT NULL
        CHECK (kind IN ('text', 'media', 'reaction', 'system', 'unknown')),
    text TEXT,
    observed_at TEXT NOT NULL,
    platform_ts TEXT,
    platform_msg_id TEXT,
    reply_to TEXT,
    source TEXT NOT NULL CHECK (source IN ('import', 'dom', 'confirmed_send')),
    origin TEXT NOT NULL
        CHECK (origin IN ('owner_authored', 'owner_approved', 'shadow_generated', 'unknown')),
    seq INTEGER NOT NULL,
    import_key TEXT UNIQUE,
    CHECK ((conversation_id IS NULL) <> (legacy_contact_id IS NULL)),
    UNIQUE (conversation_id, platform_msg_id)
);

CREATE INDEX events_by_conversation ON events (conversation_id, seq);
CREATE INDEX events_by_legacy_contact ON events (legacy_contact_id, seq);

CREATE TABLE conversation_work (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations (id),
    inbound_revision INTEGER NOT NULL DEFAULT 0,
    due_at TEXT,
    first_pending_at TEXT,
    state TEXT NOT NULL DEFAULT 'idle' CHECK (state IN ('idle', 'pending', 'blocked')),
    cancel_generation INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations (id),
    source_revision INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    versions_json TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'superseded', 'completed')),
    created_at TEXT NOT NULL
);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions (id),
    item_index INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL REFERENCES conversations (id),
    text TEXT NOT NULL,
    authority TEXT NOT NULL
        CHECK (authority IN ('autonomous', 'owner_approved', 'owner_command')),
    revision INTEGER NOT NULL,
    config_digest TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'attempting', 'confirmed',
        'failed_before_submit', 'uncertain', 'cancelled', 'held')),
    attempted_at TEXT,
    confirmed_at TEXT,
    evidence TEXT,
    confirmed_event_id INTEGER REFERENCES events (id),
    UNIQUE (decision_id, item_index)
);

CREATE TABLE imports (
    id INTEGER PRIMARY KEY,
    source_label_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_imported INTEGER NOT NULL DEFAULT 0,
    rows_duplicate INTEGER NOT NULL DEFAULT 0,
    rows_quarantined INTEGER NOT NULL DEFAULT 0
);

-- Reason codes only. Quarantined row content never enters the database.
CREATE TABLE quarantine (
    id INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES imports (id),
    file_hash TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE owner_latency_profile (
    bucket TEXT NOT NULL
        CHECK (bucket IN ('00-06', '06-12', '12-18', '18-24')),
    samples INTEGER NOT NULL,
    p25 REAL NOT NULL,
    p50 REAL NOT NULL,
    p75 REAL NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (bucket)
);
"""

# --- schema v2 (P03) ----------------------------------------------------
# Additive, plus two table rebuilds. A CHECK constraint cannot be widened in
# place in SQLite, so `conversation_work` and `decisions` are recreated and
# copied; `decisions` needs the foreign-key-off path below because `outbox`
# references it. The v1 SQL above is never edited: an old database is always
# migrated forward, so v1 must keep describing v1.
SCHEMA_V2 = """
CREATE TABLE usage_counters (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    window_start TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kind, window_start)
);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL CHECK (tier IN ('urgent', 'action', 'info')),
    reason TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversations (id),
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'sent', 'acknowledged', 'resolved')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    digest_included INTEGER NOT NULL DEFAULT 0 CHECK (digest_included IN (0, 1))
);

CREATE INDEX alerts_by_state ON alerts (state, next_attempt_at);

-- Columns P03 needs but v1 had no place for.
ALTER TABLE conversations ADD COLUMN paused_reason TEXT;
ALTER TABLE conversations ADD COLUMN first_seen_baseline_done INTEGER NOT NULL DEFAULT 0
    CHECK (first_seen_baseline_done IN (0, 1));
ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;

CREATE TABLE conversation_work_v2 (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations (id),
    inbound_revision INTEGER NOT NULL DEFAULT 0,
    due_at TEXT,
    first_pending_at TEXT,
    state TEXT NOT NULL DEFAULT 'idle'
        CHECK (state IN ('idle', 'pending', 'assessing', 'drafted', 'awaiting_approval',
                         'due', 'sending', 'held', 'owner_active', 'blocked')),
    cancel_generation INTEGER NOT NULL DEFAULT 0,
    sampled_delay_seconds INTEGER,
    last_inbound_at TEXT,
    held_reason TEXT
);

INSERT INTO conversation_work_v2 (conversation_id, inbound_revision, due_at,
        first_pending_at, state, cancel_generation, sampled_delay_seconds, last_inbound_at,
        held_reason)
    SELECT conversation_id, inbound_revision, due_at, first_pending_at,
           CASE state WHEN 'blocked' THEN 'held' ELSE state END,
           cancel_generation, NULL, NULL, NULL
    FROM conversation_work;

DROP TABLE conversation_work;
ALTER TABLE conversation_work_v2 RENAME TO conversation_work;

CREATE TABLE decisions_v2 (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations (id),
    source_revision INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    versions_json TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('recorded', 'awaiting_approval', 'approved', 'rejected', 'expired',
                          'superseded', 'queued', 'done')),
    created_at TEXT NOT NULL
);

-- v1 statuses mapped forward, never dropped: 'pending' becomes 'recorded' (a
-- stored decision nothing has acted on) and 'completed' becomes 'done'.
INSERT INTO decisions_v2 (id, conversation_id, source_revision, record_json, versions_json,
        status, created_at)
    SELECT id, conversation_id, source_revision, record_json, versions_json,
           CASE status WHEN 'pending' THEN 'recorded' WHEN 'completed' THEN 'done'
                ELSE status END,
           created_at
    FROM decisions;

DROP TABLE decisions;
ALTER TABLE decisions_v2 RENAME TO decisions;
"""

# Forward-only: each entry is (version, sql) or (version, sql, needs_fk_off).
# Statements are applied inside one BEGIN IMMEDIATE per version with
# `user_version` set in that same transaction, so a crash mid-migration leaves
# the old version intact.
#
# A rebuild of a table that another table references needs `PRAGMA foreign_keys`
# OFF, and that pragma is a no-op inside a transaction, so it is switched around
# the transaction rather than inside it. A rebuild that would break a reference
# is caught by `PRAGMA foreign_key_check` before the COMMIT, so it still rolls
# back whole.
SCHEMA_V3 = """
CREATE TABLE owner_updates (
    update_id INTEGER PRIMARY KEY,
    command TEXT NOT NULL,
    outcome TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0, 1)),
    processed_at TEXT NOT NULL
);
"""

SCHEMA_V4 = """
ALTER TABLE alerts ADD COLUMN sent_at TEXT;
ALTER TABLE alerts ADD COLUMN repeat_index INTEGER NOT NULL DEFAULT 0;
ALTER TABLE alerts ADD COLUMN escalated_at TEXT;
"""

SCHEMA_V5 = """
CREATE TABLE memory_versions (
    scope TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE memory_facts (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    subject_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('preference', 'plan', 'relationship')),
    value_text TEXT NOT NULL,
    source_event_ids TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'extraction',
    asserted_by TEXT NOT NULL DEFAULT 'contact',
    confidence_label TEXT NOT NULL DEFAULT 'reported',
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    status TEXT NOT NULL DEFAULT 'candidate'
      CHECK(status IN ('candidate', 'active', 'uncertain', 'superseded', 'expired', 'deleted')),
    supersedes_id INTEGER REFERENCES memory_facts(id),
    schema_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(conversation_id, kind, value_text, source_event_ids)
);
CREATE TABLE correction_rules (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    dimension TEXT NOT NULL,
    value_text TEXT NOT NULL,
    source_update_id INTEGER NOT NULL REFERENCES owner_updates(update_id) DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    supersedes_id INTEGER REFERENCES correction_rules(id)
);
CREATE TABLE owner_profile (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    profile_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
"""

SCHEMA_V6 = """
CREATE TABLE approval_requests (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL UNIQUE REFERENCES decisions(id),
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    state TEXT NOT NULL DEFAULT 'pending'
      CHECK(state IN ('pending','editing','approved','skipped','expired','superseded')),
    message_id INTEGER,
    owner_edit TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

SCHEMA_V7 = """
ALTER TABLE conversation_work ADD COLUMN first_pending_seq INTEGER;
CREATE TABLE quality_signals (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    inbound_revision INTEGER NOT NULL,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    signal TEXT NOT NULL CHECK(signal IN ('confusion','automation_question')),
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, inbound_revision, signal)
);
"""

SCHEMA_V8 = """
CREATE TABLE role_calls (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    role TEXT NOT NULL CHECK(role IN ('risk','reply','extraction')),
    started_at TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    failure TEXT,
    usage_json TEXT NOT NULL,
    versions_json TEXT NOT NULL
);
"""

SCHEMA_V9 = """
ALTER TABLE events ADD COLUMN media_label TEXT;
"""

SCHEMA_V10 = """
ALTER TABLE events ADD COLUMN evidence_json TEXT;
CREATE TABLE dom_observations (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX dom_observations_thread ON dom_observations(conversation_id, id);
"""

MIGRATIONS: tuple[tuple, ...] = (
    (1, SCHEMA_V1), (2, SCHEMA_V2, True), (3, SCHEMA_V3), (4, SCHEMA_V4), (5, SCHEMA_V5),
    (6, SCHEMA_V6), (7, SCHEMA_V7), (8, SCHEMA_V8), (9, SCHEMA_V9), (10, SCHEMA_V10),
)
CURRENT_VERSION = MIGRATIONS[-1][0]


class StorageError(Exception):
    """Any refusal to keep using the database as if nothing happened."""


class StorageLocked(StorageError):
    """Another live process holds the data directory."""


class StorageCorrupt(StorageError):
    """Integrity check failed. Never repaired, never recreated."""


class UnsupportedSchema(StorageError):
    """The database was written by a newer version of this code."""


class InvalidTransition(StorageError):
    """An outbox state change the state machine does not allow."""


def utcnow() -> str:
    """UTC timestamp, the only format written for observed times."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso(when: datetime) -> str:
    """Format an aware datetime the way utcnow() does. A naive datetime is
    refused rather than assumed to be UTC: a clock that lost its timezone must
    not silently become a valid timestamp."""
    if when.tzinfo is None:
        raise StorageError(f"{when!r} has no timezone; pass an aware datetime")
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str | None) -> datetime | None:
    """Read a stored timestamp back. Returns None for a NULL column, so a
    caller can treat "never" and "unknown" the same way instead of guessing."""
    if not value:
        return None
    return datetime.fromisoformat(value)


def _statements(sql: str) -> list[str]:
    """Split a migration into statements. The schema has no semicolons inside
    a statement, so executescript() (which commits implicitly) is not used."""
    return [part.strip() for part in sql.split(";") if part.strip()]


def _chmod_private(path: Path | str) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)


# --- process lock -----------------------------------------------------


def _acquire_lock(directory: Path) -> int:
    """Take the exclusive lock on `shadow.lock`. Returns the file descriptor.

    Stale locks cannot happen on purpose: the kernel drops an flock when the
    holding process exits, so a crashed run leaves no lock behind. The pid in
    the file is written for diagnostics only and is never trusted to decide
    whether the lock is live (S06).
    """
    path = directory / LOCK_FILENAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    _chmod_private(path)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # pragma: no cover - Windows
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        os.close(fd)
        raise StorageLocked(
            f"another Shadow process is using {directory} ({exc})"
        ) from exc
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    os.fsync(fd)
    return fd


def _release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - best effort on close
        pass
    finally:
        os.close(fd)


# --- integrity, backup, migration -------------------------------------


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in names}


def _verify_integrity(conn: sqlite3.Connection, label: str) -> str:
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise StorageCorrupt(f"{label}: unreadable database ({exc})") from exc
    result = row[0] if row else "no result"
    if result != "ok":
        raise StorageCorrupt(
            f"{label}: quick_check reported {result!r}; refusing to repair, "
            "recreate or start fresh"
        )
    return result


def _copy_database(source: sqlite3.Connection, dest: Path) -> None:
    """Copy a live database with the sqlite3 backup API."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(dest.parent, 0o700)
    with closing(sqlite3.connect(dest)) as target:
        target.execute("PRAGMA journal_mode = DELETE")
        source.backup(target)
    _chmod_private(dest)


def _verify_copy(source: sqlite3.Connection, dest: Path) -> None:
    with closing(sqlite3.connect(f"file:{dest}?mode=ro", uri=True)) as copy:
        _verify_integrity(copy, str(dest))
        if _row_counts(source) != _row_counts(copy):
            raise StorageError(
                f"{dest}: backup row counts differ from the source database"
            )


def _migrate(conn: sqlite3.Connection, directory: Path, path: Path) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > CURRENT_VERSION:
        raise UnsupportedSchema(
            f"{path}: schema version {current} is newer than this code knows "
            f"({CURRENT_VERSION}); upgrade Shadow before running it"
        )
    for entry in MIGRATIONS:
        target, sql = entry[0], entry[1]
        fk_off = len(entry) > 2 and bool(entry[2])
        if target <= current:
            continue
        if _has_user_tables(conn):
            # A database that already holds data is backed up and the backup
            # is verified before any migration statement runs. The name carries
            # the version being migrated FROM, so one file per step: with more
            # than one pending version the label must follow `current`, not the
            # version read at open time.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = directory / BACKUP_DIR / f"pre-migration-v{current}-{stamp}.sqlite3"
            _copy_database(conn, dest)
            _verify_copy(conn, dest)
        if fk_off:
            conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _statements(sql):
                conn.execute(statement)
            if fk_off:
                broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise StorageError(
                        f"migration to v{target} would break {len(broken)} foreign "
                        f"key reference(s); rolled back"
                    )
            conn.execute(f"PRAGMA user_version = {int(target)}")
        except BaseException:
            conn.execute("ROLLBACK")
            if fk_off:
                conn.execute("PRAGMA foreign_keys = ON")
            raise
        conn.execute("COMMIT")
        if fk_off:
            conn.execute("PRAGMA foreign_keys = ON")
        current = target


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
    )


# --- storage ----------------------------------------------------------


class Storage:
    def __init__(self, directory: Path, conn: sqlite3.Connection, lock_fd: int) -> None:
        self.directory = directory
        self.path = directory / DB_FILENAME
        self._conn = conn
        self._lock_fd = lock_fd

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        _release_lock(self._lock_fd)
        self._lock_fd = None

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        """One explicit write transaction. Rolls back on any exception, so a
        crash or a raised error never leaves half of a change behind.

        A nested call joins the transaction that is already open, which is how
        the importer commits a whole folder with one fsync. An exception from
        an inner `with` still propagates to the outer COMMIT/ROLLBACK.
        """
        if self._conn.in_transaction:
            yield self._conn
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # --- runtime ----------------------------------------------------
    def get_runtime(self) -> dict | None:
        row = self._conn.execute("SELECT * FROM runtime WHERE id = 1").fetchone()
        return dict(row) if row else None

    def set_runtime(
        self,
        *,
        account_id: str = "",
        mode: str = "observe",
        autonomy_scope: str = "none",
        pause_reason: str | None = None,
        config_digest: str | None = None,
    ) -> None:
        """Create or replace the single runtime row. No numeric setting is
        hard-coded here; callers pass values from the settings module."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO runtime (id, account_id, mode, autonomy_scope, "
                "pause_reason, config_digest, created_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET account_id = excluded.account_id, "
                "mode = excluded.mode, autonomy_scope = excluded.autonomy_scope, "
                "pause_reason = excluded.pause_reason, "
                "config_digest = excluded.config_digest",
                (account_id, mode, autonomy_scope, pause_reason, config_digest, utcnow()),
            )

    # --- conversations ----------------------------------------------
    def get_or_create_conversation(
        self, account_id: str, thread_id: str, label: str | None = None
    ) -> int:
        """Find or create the conversation for this EXACT (account, thread)
        pair. The label is presentation and is never a lookup key: no fuzzy,
        substring or name matching exists in this module (TRD, rules 8)."""
        thread_id = str(thread_id)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id, label FROM conversations "
                "WHERE account_id = ? AND thread_id = ?",
                (account_id, thread_id),
            ).fetchone()
            if row is not None:
                if label and not row["label"]:
                    conn.execute(
                        "UPDATE conversations SET label = ? WHERE id = ?", (label, row["id"])
                    )
                return row["id"]
            cursor = conn.execute(
                "INSERT INTO conversations (account_id, thread_id, label) VALUES (?, ?, ?)",
                (account_id, thread_id, label),
            )
            return cursor.lastrowid

    def get_conversation(self, conversation_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_contact_status(self, conversation_id: int, status: str, evidence=None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE conversations SET contact_status = ?, binding_evidence = ? "
                "WHERE id = ?",
                (status, evidence, conversation_id),
            )

    def get_conversation_by_thread(self, account_id: str, thread_id: str) -> dict | None:
        """Look a conversation up by its exact platform thread id. The only
        supported lookup by thread: never by label (rules 8)."""
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE account_id = ? AND thread_id = ?",
            (account_id, str(thread_id)),
        ).fetchone()
        return dict(row) if row else None

    def set_owner_active_until(self, conversation_id: int, until: str | None) -> None:
        """Takeover window end, or None to clear it (`/resume`)."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE conversations SET owner_active_until = ? WHERE id = ?",
                (until, conversation_id),
            )

    def set_paused_reason(self, conversation_id: int, reason: str | None) -> None:
        """Per-conversation hold reason (manual_text, group, takeover, ...).
        None clears it. This is local state, never a send decision."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE conversations SET paused_reason = ? WHERE id = ?",
                (reason, conversation_id),
            )

    def mark_baseline_done(self, conversation_id: int, done: bool = True) -> None:
        """The first read of a thread is history, not new work (D40). Once
        recorded, later reads are alignments against that history."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE conversations SET first_seen_baseline_done = ? WHERE id = ?",
                (1 if done else 0, conversation_id),
            )

    def get_work(self, conversation_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM conversation_work WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_work(
        self,
        conversation_id: int,
        *,
        inbound_revision: int,
        due_at: str | None = None,
        first_pending_at: str | None = None,
        state: str = "pending",
        cancel_generation: int = 0,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO conversation_work (conversation_id, inbound_revision, due_at, "
                "first_pending_at, state, cancel_generation) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (conversation_id) DO UPDATE SET "
                "inbound_revision = excluded.inbound_revision, due_at = excluded.due_at, "
                "first_pending_at = excluded.first_pending_at, state = excluded.state, "
                "cancel_generation = excluded.cancel_generation",
                (conversation_id, inbound_revision, due_at, first_pending_at, state,
                 cancel_generation),
            )

    # Columns `update_work` may change. An explicit list, so a caller cannot
    # invent a column or quietly rewrite the conversation id.
    _WORK_UPDATABLE = frozenset(
        {
            "inbound_revision",
            "due_at",
            "first_pending_at",
            "state",
            "cancel_generation",
            "sampled_delay_seconds",
            "last_inbound_at",
            "held_reason",
            "first_pending_seq",
        }
    )

    def update_work(self, conversation_id: int, **fields) -> None:
        """Change only the named work columns.

        `set_work` replaces the whole row and needs a revision; this is for the
        coordinator moving one thread's state without touching anything else.
        Raises before writing if a name is not updatable.
        """
        unknown = sorted(set(fields) - self._WORK_UPDATABLE)
        if unknown:
            raise StorageError(
                f"update_work: not updatable: {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(self._WORK_UPDATABLE))}"
            )
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [conversation_id]
        with self.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM conversation_work WHERE conversation_id = ?", (conversation_id,)
            ).fetchone() is None:
                raise StorageError(
                    f"conversation {conversation_id} has no work row; create it "
                    f"with set_work() before updating it"
                )
            conn.execute(
                f"UPDATE conversation_work SET {assignments} WHERE conversation_id = ?", values
            )

    def work_in_states(self, states) -> list[dict]:
        names = tuple(states)
        if not names:
            return []
        marks = ", ".join("?" for _ in names)
        rows = self._conn.execute(
            f"SELECT w.*, c.account_id, c.thread_id, c.contact_status, c.label, "
            f"c.paused_reason, c.owner_active_until, c.first_seen_baseline_done "
            f"FROM conversation_work w JOIN conversations c ON c.id = w.conversation_id "
            f"WHERE w.state IN ({marks}) ORDER BY w.conversation_id",
            names,
        ).fetchall()
        return [dict(row) for row in rows]

    # --- legacy contacts and explicit binding -----------------------
    def add_legacy_contact(
        self, folder_name: str, closeness=None, relationship: str | None = None
    ) -> int:
        """One row per imported folder, always unbound. Nothing binds a folder
        to a conversation by itself (TRD, "Observation identity and import")."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO legacy_contacts (folder_name, closeness, relationship) "
                "VALUES (?, ?, ?)",
                (folder_name, closeness, relationship),
            )
            row = conn.execute(
                "SELECT id FROM legacy_contacts WHERE folder_name = ?", (folder_name,)
            ).fetchone()
            return row["id"]

    def get_legacy_contact(self, legacy_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM legacy_contacts WHERE id = ?", (legacy_id,)
        ).fetchone()
        return dict(row) if row else None

    def bind_legacy_contact(
        self, legacy_id: int, conversation_id: int, evidence: str
    ) -> None:
        """Owner-driven binding of one folder to one verified conversation."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE legacy_contacts SET binding_status = 'bound', "
                "bound_conversation_id = ?, bound_evidence = ?, bound_at = ? "
                "WHERE id = ? AND binding_status = 'unbound'",
                (conversation_id, evidence, utcnow(), legacy_id),
            )
            if cursor.rowcount != 1:
                raise StorageError(
                    f"legacy contact {legacy_id} is missing or already bound; "
                    "binding is explicit and never automatic"
                )

    def unbind_legacy_contact(self, legacy_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE legacy_contacts SET binding_status = 'unbound', "
                "bound_conversation_id = NULL, bound_evidence = NULL, bound_at = NULL "
                "WHERE id = ?",
                (legacy_id,),
            )

    # --- events ------------------------------------------------------
    def _next_seq(self, conn: sqlite3.Connection, conversation_id, legacy_contact_id) -> int:
        if conversation_id is not None:
            row = conn.execute(
                "SELECT MAX(seq) FROM events WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(seq) FROM events WHERE legacy_contact_id = ?", (legacy_contact_id,)
            ).fetchone()
        return 1 if row[0] is None else row[0] + 1

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id=None,
        legacy_contact_id=None,
        sender: str,
        kind: str,
        text: str | None,
        observed_at: str | None = None,
        platform_ts: str | None = None,
        platform_msg_id: str | None = None,
        reply_to: str | None = None,
        media_label: str | None = None,
        evidence: dict | None = None,
        source: str,
        origin: str,
        seq: int | None = None,
        import_key: str | None = None,
        ignore_conflict: bool = False,
    ) -> int:
        if (conversation_id is None) == (legacy_contact_id is None):
            raise StorageError(
                "an event belongs to exactly one of conversation_id or legacy_contact_id"
            )
        if media_label is not None and (kind != 'media' or type(media_label) is not str
                                        or not media_label or len(media_label) > 80):
            raise StorageError('Invalid media label')
        values = (
            conversation_id,
            legacy_contact_id,
            sender,
            kind,
            text,
            observed_at or utcnow(),
            platform_ts,
            platform_msg_id,
            reply_to,
            source,
            origin,
            seq if seq is not None else self._next_seq(conn, conversation_id, legacy_contact_id),
            import_key,
            media_label,
            json.dumps(evidence, ensure_ascii=False, allow_nan=False) if evidence is not None else None,
        )
        verb = "INSERT OR IGNORE" if ignore_conflict else "INSERT"
        cursor = conn.execute(
            f"{verb} INTO events (conversation_id, legacy_contact_id, sender, kind, text, "
            "observed_at, platform_ts, platform_msg_id, reply_to, source, origin, seq, "
            "import_key, media_label, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        if ignore_conflict and cursor.rowcount == 0:
            return 0  # already imported; the caller counts it as a duplicate
        return cursor.lastrowid

    def add_event(self, *, ignore_conflict: bool = False, **fields) -> int:
        """Record one observed event.

        Without `ignore_conflict` a repeated platform message id inside one
        conversation raises IntegrityError, because "the same text twice" is
        two events but "the same platform id twice" is one event seen twice.
        `ignore_conflict=True` is for idempotent re-import only, and returns 0
        for a row that was already there.
        """
        with self.transaction() as conn:
            return self._insert_event(conn, ignore_conflict=ignore_conflict, **fields)

    def get_events(self, conversation_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE conversation_id = ? ORDER BY seq",
            (conversation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_events(self, events: list[dict]) -> list[int]:
        """One aligned window is atomic; a failed item cannot leave its prefix."""
        with self.transaction() as conn:
            return [self._insert_event(conn, **fields) for fields in events]

    def get_legacy_events(self, legacy_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE legacy_contact_id = ? ORDER BY seq", (legacy_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def record_dom_observation(self, conversation_id: int, payload: dict, *,
                               limit: int, byte_limit: int) -> int:
        """Retain changed read evidence; never reconcile missing nodes as deletes.

        Evidence is local/private and is not automatically exposed to model
        context. Window orders and candidate identifiers remain unresolved.
        """
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if type(limit) is not int or limit <= 0 or type(byte_limit) is not int or byte_limit <= 0:
            raise StorageError('Invalid observation retention limit')
        if len(encoded.encode('utf-8')) > byte_limit:
            raise StorageError('DOM observation exceeds evidence budget')
        with self.transaction() as conn:
            previous = conn.execute('SELECT payload_json FROM dom_observations '
                                    'WHERE conversation_id = ? ORDER BY id DESC LIMIT 1',
                                    (conversation_id,)).fetchone()
            if previous and previous[0] == encoded:
                return 0
            cursor = conn.execute('INSERT INTO dom_observations '
                                  '(conversation_id, observed_at, payload_json) VALUES (?, ?, ?)',
                                  (conversation_id, utcnow(), encoded))
            conn.execute('DELETE FROM dom_observations WHERE conversation_id = ? AND id NOT IN '
                         '(SELECT id FROM dom_observations WHERE conversation_id = ? '
                         'ORDER BY id DESC LIMIT ?)', (conversation_id, conversation_id, limit))
            return cursor.lastrowid

    def dom_observations(self, conversation_id: int) -> list[dict]:
        rows = self._conn.execute('SELECT * FROM dom_observations WHERE conversation_id = ? '
                                  'ORDER BY id', (conversation_id,)).fetchall()
        return [{**dict(row), 'payload': json.loads(row['payload_json'])} for row in rows]

    # --- decisions and outbox ---------------------------------------
    def add_decision(
        self,
        *,
        conversation_id: int,
        source_revision: int,
        record_json: str,
        versions_json: str,
        status: str = "recorded",
    ) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO decisions (conversation_id, source_revision, record_json, "
                "versions_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (conversation_id, source_revision, record_json, versions_json, status,
                 utcnow()),
            )
            return cursor.lastrowid

    def add_outbox_item(
        self,
        *,
        decision_id: int,
        item_index: int,
        conversation_id: int,
        text: str,
        authority: str,
        revision: int,
        config_digest: str | None = None,
    ) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO outbox (decision_id, item_index, conversation_id, text, "
                "authority, revision, config_digest, state) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (decision_id, item_index, conversation_id, text, authority, revision,
                 config_digest),
            )
            return cursor.lastrowid

    def get_outbox_item(self, item_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM outbox WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def outbox_items(self, conversation_id: int | None = None) -> list[dict]:
        if conversation_id is None:
            rows = self._conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def transition(self, item_id: int, to_state: str, *, evidence: str | None = None) -> None:
        """Move one outbox item, checking the current state inside the same
        transaction. Anything outside ALLOWED raises InvalidTransition.

        `confirmed` is deliberately not reachable here: confirmation is only
        legal through confirm_send(), which writes the sent event in the same
        transaction.
        """
        with self.transaction() as conn:
            row = conn.execute("SELECT state FROM outbox WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise StorageError(f"outbox item {item_id} does not exist")
            current = row["state"]
            if to_state in CONFIRM_ONLY_TARGETS:
                raise InvalidTransition(
                    f"outbox item {item_id}: use confirm_send() to reach "
                    f"{to_state!r} so the sent event is written with it"
                )
            if to_state not in ALLOWED[current]:
                raise InvalidTransition(
                    f"outbox item {item_id}: {current} -> {to_state} is not allowed"
                )
            conn.execute(
                "UPDATE outbox SET state = ?, evidence = COALESCE(?, evidence), "
                "attempted_at = CASE WHEN ? = 'attempting' THEN ? ELSE attempted_at END, "
                "attempts = CASE WHEN ? = 'attempting' THEN attempts + 1 ELSE attempts END "
                "WHERE id = ?",
                (to_state, evidence, to_state, utcnow(), to_state, item_id),
            )

    def set_outbox_evidence(self, item_id: int, evidence: str) -> None:
        """Attach browser evidence to an item after it was confirmed.

        Separate from confirm_send on purpose: the state change and the sent
        event are the atomic pair, and this is a diagnostic written after them.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE outbox SET evidence = COALESCE(evidence, ?) WHERE id = ?",
                (evidence, item_id),
            )

    def set_decision_status(self, decision_id: int, status: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE decisions SET status = ? WHERE id = ?", (status, decision_id)
            )

    def decisions_for(self, conversation_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def cancel_decisions(self, conversation_id: int, statuses) -> int:
        """Mark a thread's outstanding decisions superseded. Only the given
        statuses are touched, so a confirmed decision keeps its outcome."""
        names = tuple(statuses)
        if not names:
            return 0
        marks = ", ".join("?" for _ in names)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE decisions SET status = 'superseded' "
                f"WHERE conversation_id = ? AND status IN ({marks})",
                (conversation_id, *names),
            )
            return cursor.rowcount

    def cancel_outbox_items(
        self, conversation_id: int, reason: str, states=("pending",)
    ) -> int:
        """Cancel the items of one thread that have not been attempted.

        States that cannot reach `cancelled` are filtered out rather than
        forced, so a confirmed or uncertain item is never rewritten here.
        """
        names = tuple(s for s in states if "cancelled" in ALLOWED.get(s, ()))
        if not names:
            return 0
        marks = ", ".join("?" for _ in names)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE outbox SET state = 'cancelled', evidence = COALESCE(?, evidence) "
                f"WHERE conversation_id = ? AND state IN ({marks})",
                (reason, conversation_id, *names),
            )
            return cursor.rowcount

    def _mark_confirmed(self, conn: sqlite3.Connection, item_id: int, event_id: int) -> None:
        conn.execute(
            "UPDATE outbox SET state = 'confirmed', confirmed_at = ?, confirmed_event_id = ? "
            "WHERE id = ?",
            (utcnow(), event_id, item_id),
        )

    def confirm_send(self, item_id: int, event: dict) -> int:
        """Confirm one sent item and record the evidence for it in ONE
        transaction. Allowed only from `attempting` or `uncertain`.

        The event is stored against the item's own conversation, so an
        observation of the wrong thread can never confirm a send (a
        mismatched thread in `event` is refused, not ignored). Its origin
        follows the item's authority: an autonomous send is shadow-generated,
        an approved send is owner-approved, an owner command is owner-authored.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state, conversation_id, authority FROM outbox WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise StorageError(f"outbox item {item_id} does not exist")
            if row["state"] not in ("attempting", "uncertain"):
                raise InvalidTransition(
                    f"outbox item {item_id}: cannot confirm from {row['state']}"
                )
            if event.get("conversation_id", row["conversation_id"]) != row["conversation_id"]:
                raise StorageError(
                    f"outbox item {item_id} belongs to conversation "
                    f"{row['conversation_id']}; the observed event names a different one"
                )
            event_id = self._insert_event(
                conn,
                conversation_id=row["conversation_id"],
                sender="owner",
                kind="text",
                source="confirmed_send",
                origin=AUTHORITY_ORIGIN[row["authority"]],
                **event,
            )
            self._mark_confirmed(conn, item_id, event_id)
            return event_id

    def recover_after_restart(self) -> int:
        """Turn every `attempting` item into `uncertain` and return the count.

        A crash during or after a submit attempt is never a silent success and
        never a safe retry: those items are reconciled (never blindly resent)
        and stay `uncertain` until unique evidence or an owner decision.
        Call this once at startup before anything reads the outbox.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE outbox SET state = 'uncertain' WHERE state = 'attempting'"
            )
            return cursor.rowcount

    # --- imports, quarantine, latency profile -----------------------
    def start_import(self, source_label_hash: str) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO imports (source_label_hash, started_at, status) "
                "VALUES (?, ?, 'running')",
                (source_label_hash, utcnow()),
            )
            return cursor.lastrowid

    def finish_import(
        self,
        import_id: int,
        *,
        status: str = "completed",
        rows_total: int = 0,
        rows_imported: int = 0,
        rows_duplicate: int = 0,
        rows_quarantined: int = 0,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE imports SET finished_at = ?, status = ?, rows_total = ?, "
                "rows_imported = ?, rows_duplicate = ?, rows_quarantined = ? WHERE id = ?",
                (utcnow(), status, rows_total, rows_imported, rows_duplicate,
                 rows_quarantined, import_id),
            )

    def add_quarantine(self, import_id: int, file_hash: str, row_index: int, reason: str) -> None:
        """Record a rejected row by reason only. The row's text is not stored."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO quarantine (import_id, file_hash, row_index, reason) "
                "VALUES (?, ?, ?, ?)",
                (import_id, file_hash, row_index, reason),
            )

    def quarantine_rows(self, import_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT row_index, reason FROM quarantine WHERE import_id = ? ORDER BY row_index",
            (import_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def store_latency_profile(self, profile: dict[str, tuple[int, float, float, float]]) -> None:
        """Replace the stored buckets. Callers compute percentiles."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM owner_latency_profile")
            for bucket, (samples, p25, p50, p75) in profile.items():
                if bucket not in LATENCY_BUCKETS:
                    raise StorageError(f"unknown latency bucket {bucket!r}")
                conn.execute(
                    "INSERT INTO owner_latency_profile (bucket, samples, p25, p50, p75, "
                    "computed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (bucket, samples, float(p25), float(p50), float(p75), utcnow()),
                )

    def latency_profile(self) -> dict[str, dict]:
        rows = self._conn.execute(
            "SELECT * FROM owner_latency_profile ORDER BY bucket"
        ).fetchall()
        return {row["bucket"]: dict(row) for row in rows}

    # --- usage counters and alerts ----------------------------------
    def _window_start(self, window_seconds: int, now: datetime | None = None) -> str:
        """Start of the fixed window `now` falls in.

        A fixed window, not a rolling one: both the cap and the reset are then
        predictable, and a restart cannot reset a budget by moving the clock.
        """
        if window_seconds <= 0:
            raise StorageError(f"usage window must be positive, got {window_seconds}")
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise StorageError(f"{moment!r} has no timezone; pass an aware datetime")
        epoch = int(moment.timestamp())
        return iso(datetime.fromtimestamp(epoch - epoch % window_seconds, timezone.utc))

    def try_consume(self, kind: str, limit: int, window_seconds: int, now=None) -> bool:
        """Take one unit from a `(count, window)` budget, atomically.

        True when the unit was taken. False when the window is already at the
        limit, and nothing is written in that case. The check and the increment
        share one IMMEDIATE transaction, so two callers cannot both see the last
        unit. This is how a cap refuses a send instead of a later sweep noticing.
        """
        start = self._window_start(window_seconds, now)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT count FROM usage_counters WHERE kind = ? AND window_start = ?",
                (kind, start),
            ).fetchone()
            # A limit of 0 refuses every use, including on an empty window, so
            # the count is compared rather than the row's existence.
            if (0 if row is None else row["count"]) >= limit:
                return False
            self._bump(conn, kind, start)
            return True

    def try_consume_rates(self, kind: str, rates, now=None) -> bool:
        """Take one unit from every window in `rates`, or from none.

        A cap is a tuple of windows (an hourly and a daily rate). Every window
        is checked first and consumed afterwards, so exhausting the daily rate
        does not burn the hourly one.
        """
        checked = []
        with self.transaction() as conn:
            for count, window_seconds in rates:
                start = self._window_start(window_seconds, now)
                row = conn.execute(
                    "SELECT count FROM usage_counters WHERE kind = ? AND window_start = ?",
                    (kind, start),
                ).fetchone()
                if (0 if row is None else row["count"]) >= count:
                    return False
                checked.append(start)
            for start in checked:
                self._bump(conn, kind, start)
            return True

    def rates_available(self, kind: str, rates, now=None) -> bool:
        """Whether every window in `rates` would allow one more. Consumes
        nothing.

        This is what a pre-send check needs: the worker calls its check before
        typing, before Enter and between items, and consuming a cap unit on each
        of those would charge one message three times.
        """
        for count, window_seconds in rates:
            row = self._conn.execute(
                "SELECT count FROM usage_counters WHERE kind = ? AND window_start = ?",
                (kind, self._window_start(window_seconds, now)),
            ).fetchone()
            if (0 if row is None else row["count"]) >= count:
                return False
        return True

    @staticmethod
    def _bump(conn: sqlite3.Connection, kind: str, start: str) -> None:
        conn.execute(
            "INSERT INTO usage_counters (kind, window_start, count) VALUES (?, ?, 1) "
            "ON CONFLICT (kind, window_start) DO UPDATE SET count = count + 1",
            (kind, start),
        )

    def usage_count(self, kind: str, window_seconds: int, now=None) -> int:
        start = self._window_start(window_seconds, now)
        row = self._conn.execute(
            "SELECT count FROM usage_counters WHERE kind = ? AND window_start = ?",
            (kind, start),
        ).fetchone()
        return 0 if row is None else row["count"]

    def usage_counters(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM usage_counters ORDER BY kind, window_start"
        ).fetchall()
        return [dict(row) for row in rows]

    def raise_alert(
        self,
        *,
        dedupe_key: str,
        tier: str,
        reason: str,
        conversation_id: int | None = None,
    ) -> int:
        """Record an alert once per `dedupe_key`. Idempotent: the same key
        returns the existing row and does not reset its state, so a repeat
        condition raises one alert, not one per tick. Delivery is P04's job;
        this only records what must be delivered."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM alerts WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if row is not None:
                return row["id"]
            cursor = conn.execute(
                "INSERT INTO alerts (dedupe_key, tier, reason, conversation_id, state, "
                "attempts, created_at, digest_included) VALUES (?, ?, ?, ?, 'pending', 0, ?, 0)",
                (dedupe_key, tier, reason, conversation_id, utcnow()),
            )
            return cursor.lastrowid

    def alerts(
        self, *, state: str | None = None, conversation_id: int | None = None
    ) -> list[dict]:
        clauses, values = [], []
        if state is not None:
            clauses.append("state = ?")
            values.append(state)
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            values.append(conversation_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM alerts{where} ORDER BY id", values
        ).fetchall()
        return [dict(row) for row in rows]

    def last_owner_update_id(self) -> int | None:
        row = self._conn.execute("SELECT MAX(update_id) FROM owner_updates").fetchone()
        return row[0]

    def owner_update(self, update_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM owner_updates WHERE update_id = ?", (update_id,)
        ).fetchone()
        return dict(row) if row else None

    def record_owner_update(self, update_id: int, command: str, outcome: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO owner_updates (update_id, command, outcome, processed_at) "
                "VALUES (?, ?, ?, ?)", (update_id, command, outcome, utcnow())
            )

    def pending_owner_responses(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT update_id, outcome FROM owner_updates "
            "WHERE delivered = 0 AND command != 'ignored' ORDER BY update_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def update_alert_delivery(self, alert_id: int, **fields) -> None:
        allowed = {"state", "attempts", "next_attempt_at", "sent_at", "repeat_index",
                   "escalated_at", "digest_included"}
        if not fields or not set(fields) <= allowed:
            raise StorageError("invalid alert delivery fields")
        with self.transaction() as conn:
            conn.execute("UPDATE alerts SET " + ", ".join(f"{key} = ?" for key in fields)
                         + " WHERE id = ?", (*fields.values(), alert_id))

    def mark_owner_response_delivered(self, update_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE owner_updates SET delivered = 1 WHERE update_id = ?", (update_id,))

    def set_alert_state(
        self,
        alert_id: int,
        state: str,
        *,
        attempts: int | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            if attempts is None:
                conn.execute(
                    "UPDATE alerts SET state = ?, next_attempt_at = ? WHERE id = ?",
                    (state, next_attempt_at, alert_id),
                )
            else:
                conn.execute(
                    "UPDATE alerts SET state = ?, attempts = ?, next_attempt_at = ? "
                    "WHERE id = ?",
                    (state, attempts, next_attempt_at, alert_id),
                )

    # --- backup -----------------------------------------------------
    def table_counts(self) -> dict[str, int]:
        return _row_counts(self._conn)

    def backup(self, dest_path: str | Path) -> Path:
        """Verified copy of the live database, made with the backup API."""
        dest = Path(dest_path)
        _copy_database(self._conn, dest)
        _verify_copy(self._conn, dest)
        return dest

    def restore_check(self, backup_path: str | Path, scratch_dir: str | Path) -> dict:
        """Restore a backup into `scratch_dir` and report what came back.

        The production database is never opened. Returns the quick_check
        result and the row count of every table in the restored copy.
        """
        scratch = Path(scratch_dir)
        scratch.mkdir(parents=True, exist_ok=True)
        dest = scratch / "restored.sqlite3"
        if dest.exists():
            dest.unlink()
        with closing(sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)) as source:
            _copy_database(source, dest)
        with closing(sqlite3.connect(f"file:{dest}?mode=ro", uri=True)) as restored:
            result = _verify_integrity(restored, str(dest))
            counts = _row_counts(restored)
        return {"path": str(dest), "quick_check": result, "row_counts": counts}


def open_storage(
    directory: str | Path | None = None, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> Storage:
    """Open (creating if needed) the canonical database in the private data
    directory, take the process lock, check integrity and migrate."""
    directory = Path(directory) if directory is not None else default_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    lock_fd = _acquire_lock(directory)
    conn = None
    try:
        path = directory / DB_FILENAME
        conn = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        # Integrity is checked before any pragma that writes to the file
        # (journal_mode changes the header), so a corrupt database is refused
        # without a single byte being modified.
        _verify_integrity(conn, str(path))
        if path.exists():
            _chmod_private(path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        _migrate(conn, directory, path)
        return Storage(directory, conn, lock_fd)
    except BaseException:
        if conn is not None:
            conn.close()
        _release_lock(lock_fd)
        raise
