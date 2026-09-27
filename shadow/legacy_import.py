"""Legacy import: read-only toward the originals, idempotent, conservative.

The old runtime stored one folder per person under `data/people/`, with
`profile.json` and `chat_log.jsonl`. This module copies that history into the
canonical database without ever changing, moving or reformatting a source
file, and without guessing at anything it cannot prove:

* every input file is opened `"rb"` and hashed with sha256 before and after
  reading; a changed hash aborts the import, which is the evidence that the
  originals were not touched;
* a row is identified by `sha256(folder + row index + raw line bytes)`, so
  re-running imports nothing and appending one line imports exactly one row;
* a malformed row is quarantined by reason code only — its text is never
  stored;
* authorship is never invented: rows the old runtime itself could have written
  stay `unknown` (D33);
* each folder becomes one `legacy_contacts` row, unbound. Nothing here maps a
  folder to a conversation; that is `bind_legacy_contact`, an owner action.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import statistics
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from shadow.memory import is_junk
from shadow.settings import Settings, load_settings
from shadow.storage import LATENCY_BUCKETS, Storage, open_storage

CHAT_LOG = "chat_log.jsonl"
PROFILE = "profile.json"

# The key set shadow/memory.py append() writes. A row shaped like this may
# have been written by the old bot rather than by the owner, so its origin
# cannot be asserted (D33).
RUNTIME_KEYS = frozenset({"ts", "sender", "content", "type", "platform"})
RUNTIME_PLATFORM = "instagram"

QUARANTINE_REASONS = ("bad_json", "missing_sender", "bad_sender", "missing_content")

# Latency profile: enough samples to mean something, and a gap a human
# conversation plausibly contains.
MIN_PROFILE_SAMPLES = 30
MIN_BUCKET_SAMPLES = 10
MAX_GAP_SECONDS = 24 * 60 * 60
BATCH_RUN_LENGTH = 3


class ImportAborted(Exception):
    """A source file changed while it was being read, or the source layout is
    not readable. Nothing is left half-imported."""


@dataclass(frozen=True)
class ImportReport:
    """Counts only. No folder names, no names, no message content."""

    folders: int = 0  # folders that held a chat log
    rows_total: int = 0
    rows_imported: int = 0
    rows_duplicate: int = 0
    quarantined: dict = field(default_factory=dict)
    origins: dict = field(default_factory=dict)
    kinds: dict = field(default_factory=dict)
    reliable_timestamp_rows: int = 0
    unreliable_timestamp_rows: int = 0
    latency_samples: int = 0
    latency_profile_present: bool = False
    latency_timezone: str = "UTC"
    latency_profile_note: str = ""
    inputs_unchanged: bool = True
    dry_run: bool = False

    def as_text(self) -> str:
        lines = [
            f"folders:                 {self.folders}",
            f"rows total:              {self.rows_total}",
            f"rows imported:           {self.rows_imported}",
            f"rows already present:    {self.rows_duplicate}",
            f"rows quarantined:        {sum(self.quarantined.values())}",
        ]
        for reason in QUARANTINE_REASONS:
            lines.append(f"  quarantined {reason:<17}{self.quarantined.get(reason, 0)}")
        lines.append("origin counts:")
        for origin in ("owner_authored", "owner_approved", "shadow_generated", "unknown"):
            lines.append(f"  {origin:<22}{self.origins.get(origin, 0)}")
        lines.append("kind counts:")
        for kind in ("text", "media", "reaction", "system", "unknown"):
            lines.append(f"  {kind:<22}{self.kinds.get(kind, 0)}")
        lines.append(f"reliable timestamps:     {self.reliable_timestamp_rows}")
        lines.append(f"unreliable timestamps:   {self.unreliable_timestamp_rows}")
        lines.append(f"latency samples:         {self.latency_samples}")
        lines.append(f"latency timezone:        {self.latency_timezone}")
        lines.append(f"latency profile:         {'present' if self.latency_profile_present else 'absent'}")
        if self.latency_profile_note:
            lines.append(f"  {self.latency_profile_note}")
        lines.append(f"input files unchanged:   {'yes' if self.inputs_unchanged else 'NO'}")
        lines.append(f"mode:                    {'dry run' if self.dry_run else 'live import'}")
        return "\n".join(lines)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _people_root(source_root: Path) -> Path:
    for candidate in (source_root / "people", source_root):
        if candidate.is_dir():
            return candidate
    raise ImportAborted(f"{source_root} contains no people directory")


def _classify(text: str) -> str:
    """kind for one row, using the shared placeholder rules in shadow.memory."""
    stripped = text.strip()
    if not stripped:
        return "unknown"
    if not is_junk(stripped):
        return "text"
    if stripped.startswith("["):
        return "media"
    if stripped.lower().startswith(("reacted", "liked a message", "you reacted")):
        return "reaction"
    return "system"  # unsent / attachment notices


def _origin(row: dict) -> str:
    """Authorship, asserted only where the row's shape proves it (D33).

    A row the old runtime's own writer could have produced stays `unknown`,
    whatever its sender: the archive cannot tell a sent reply from a typed one.
    """
    if row.get("sender") == "them":
        return "unknown"
    if frozenset(row.keys()) == RUNTIME_KEYS:
        return "unknown"
    return "owner_authored"


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_unreliable(timestamps: list[str | None]) -> list[bool]:
    """A run of BATCH_RUN_LENGTH or more adjacent rows sharing one timestamp
    was written in a batch, so the time is not when the message was sent."""
    unreliable = [False] * len(timestamps)
    run_start = 0
    for index in range(1, len(timestamps) + 1):
        same = (
            index < len(timestamps)
            and timestamps[index] == timestamps[run_start]
            and timestamps[index] is not None
        )
        if not same:
            if timestamps[run_start] is not None and index - run_start >= BATCH_RUN_LENGTH:
                for position in range(run_start, index):
                    unreliable[position] = True
            run_start = index
    return unreliable


def _bucket_of(hour: int) -> str:
    start = (hour // 6) * 6
    return f"{start:02d}-{start + 6:02d}"


def _percentile_profile(samples: dict[str, list[int]]) -> dict:
    return {
        bucket: (len(values), *statistics.quantiles(values, n=4, method="inclusive"))
        for bucket, values in samples.items()
        if len(values) >= MIN_BUCKET_SAMPLES
    }


class _Counters:
    def __init__(self) -> None:
        self.rows_total = 0
        self.rows_imported = 0
        self.rows_duplicate = 0
        self.quarantined: dict[str, int] = {}
        self.origins: dict[str, int] = {}
        self.kinds: dict[str, int] = {}
        self.reliable = 0
        self.unreliable = 0

    def bump(self, table: dict[str, int], key: str) -> None:
        table[key] = table.get(key, 0) + 1


def run_import(
    source_root: str | Path,
    storage: Storage | None = None,
    *,
    dry_run: bool = False,
    scratch_dir: str | Path | None = None,
    settings: Settings | None = None,
) -> ImportReport:
    """Import every folder under `<source_root>/people/`.

    With `dry_run`, work happens in a throwaway database under `scratch_dir`
    (a temporary directory if none is given) which is deleted before this
    returns; nothing reaches the caller's real database.
    """
    source_root = Path(source_root)
    people = _people_root(source_root)

    input_files: dict[Path, str] = {}
    for folder in sorted(path for path in people.iterdir() if path.is_dir()):
        for name in (CHAT_LOG, PROFILE):
            candidate = folder / name
            if candidate.is_file():
                input_files[candidate] = _sha256(candidate)
    # A folder counts once it has a chat log; a stray profile.json is not an
    # imported history.
    folders = sorted({path.parent for path in input_files if path.name == CHAT_LOG})
    # Read every input before writing anything, then hash again: a file that
    # changed under us aborts the import with the destination untouched.
    contents = {path: path.read_bytes() for path in input_files}
    _verify_unchanged(input_files)

    owned_dir = None
    if storage is None:
        if not dry_run:
            raise ImportAborted("a live import needs a destination database")
        owned_dir = Path(scratch_dir) if scratch_dir else Path(
            tempfile.mkdtemp(prefix="shadow-import-")
        )
        owned_dir.mkdir(parents=True, exist_ok=True)
        storage = open_storage(owned_dir)
    settings = settings or load_settings()

    counters = _Counters()
    try:
        for folder in folders:
            _import_folder(folder, storage, counters, input_files, contents)
        profile, samples, note = _latency_profile(storage, settings)
        if profile:
            storage.store_latency_profile(profile)
        report = ImportReport(
            folders=len(folders),
            rows_total=counters.rows_total,
            rows_imported=counters.rows_imported,
            rows_duplicate=counters.rows_duplicate,
            quarantined=dict(counters.quarantined),
            origins=dict(counters.origins),
            kinds=dict(counters.kinds),
            reliable_timestamp_rows=counters.reliable,
            unreliable_timestamp_rows=counters.unreliable,
            latency_samples=samples,
            latency_profile_present=bool(profile),
            latency_timezone=settings.timezone or "UTC",
            latency_profile_note=note,
            inputs_unchanged=True,
            dry_run=dry_run,
        )
    finally:
        if owned_dir is not None:
            storage.close()
            shutil.rmtree(owned_dir, ignore_errors=True)
    return report


def _import_folder(
    folder: Path,
    storage: Storage,
    counters: _Counters,
    input_files: dict[Path, str],
    contents: dict[Path, bytes],
) -> None:
    chat_log = folder / CHAT_LOG
    if chat_log not in contents:
        return
    closeness, relationship = _profile(folder, contents)
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    lines = contents[chat_log].split(b"\n")
    legacy_id = storage.add_legacy_contact(folder.name, closeness, relationship)
    file_hash = input_files.get(chat_log, _sha256(chat_log))
    import_id = storage.start_import(file_hash)

    parsed = [_parse_row(line) for line in lines]
    timestamps = [row.get("ts") if row else None for row in parsed]
    unreliable = _is_unreliable(timestamps)
    folder_counts = {"total": 0, "imported": 0, "duplicate": 0, "quarantined": 0}

    with storage.transaction():
        for index, (line, row) in enumerate(zip(lines, parsed)):
            if not line.strip():
                continue
            counters.rows_total += 1
            folder_counts["total"] += 1
            reason = _reject_reason(row)
            if reason is not None:
                counters.bump(counters.quarantined, reason)
                folder_counts["quarantined"] += 1
                storage.add_quarantine(import_id, file_hash, index, reason)
                continue
            origin = _origin(row)
            kind = _classify(row["content"])
            counters.bump(counters.origins, origin)
            counters.bump(counters.kinds, kind)
            if unreliable[index]:
                counters.unreliable += 1
            else:
                counters.reliable += 1
            event_id = storage.add_event(
                ignore_conflict=True,
                legacy_contact_id=legacy_id,
                sender={"me": "owner", "them": "contact"}[row["sender"]],
                kind=kind,
                text=row["content"],
                observed_at=observed_at,
                platform_ts=row["ts"] if _parse_ts(row.get("ts")) else None,
                source="import",
                origin=origin,
                seq=index,
                import_key=_import_key(folder.name, index, line),
            )
            if event_id:
                counters.rows_imported += 1
                folder_counts["imported"] += 1
            else:
                counters.rows_duplicate += 1
                folder_counts["duplicate"] += 1

    storage.finish_import(
        import_id,
        rows_total=folder_counts["total"],
        rows_imported=folder_counts["imported"],
        rows_duplicate=folder_counts["duplicate"],
        rows_quarantined=folder_counts["quarantined"],
    )


def _profile(folder: Path, contents: dict[Path, bytes]) -> tuple[int | None, str | None]:
    path = folder / PROFILE
    if path not in contents:
        return None, None
    try:
        data = json.loads(contents[path].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    closeness = data.get("closeness")
    relationship = data.get("relationship")
    return (
        closeness if isinstance(closeness, int) else None,
        relationship if isinstance(relationship, str) else None,
    )


def _parse_row(line: bytes) -> dict | None:
    try:
        row = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return row if isinstance(row, dict) else None


def _reject_reason(row: dict | None) -> str | None:
    if row is None:
        return "bad_json"
    if "sender" not in row:
        return "missing_sender"
    if row["sender"] not in ("me", "them"):
        return "bad_sender"
    if not isinstance(row.get("content"), str):
        return "missing_content"
    return None


def _import_key(folder_name: str, row_index: int, raw_line: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(folder_name.encode("utf-8"))
    digest.update(b"\n")
    digest.update(str(row_index).encode("ascii"))
    digest.update(b"\n")
    digest.update(raw_line)
    return digest.hexdigest()


def _verify_unchanged(input_files: dict[Path, str]) -> None:
    for path, before in input_files.items():
        if _sha256(path) != before:
            raise ImportAborted(
                f"{path.name} changed while it was being read; import aborted"
            )


def _latency_profile(storage: Storage, settings: Settings) -> tuple[dict, int, str]:
    """Derive the owner reply-latency profile from imported history.

    A sample is the gap from a contact message to the owner's next reply in
    the same folder, where both rows carry a timestamp that was not
    batch-written. Only `owner_authored` rows count as the owner's reply:
    an import never asserts authorship it cannot prove, so folders whose rows
    could have come from the old bot contribute nothing (D33).
    """
    zone = ZoneInfo(settings.timezone) if settings.timezone else timezone.utc
    samples: dict[str, list[int]] = {bucket: [] for bucket in LATENCY_BUCKETS}
    total = 0
    contacts = [
        row[0]
        for row in storage.conn.execute(
            "SELECT DISTINCT legacy_contact_id FROM events WHERE source = 'import' "
            "AND legacy_contact_id IS NOT NULL"
        )
    ]
    for legacy_id in contacts:
        awaiting_reply = None
        for event in storage.get_legacy_events(legacy_id):
            moment = _parse_ts(event["platform_ts"])
            if moment is None:
                continue  # unparseable or batch-written time: not a sample
            if event["sender"] == "contact":
                awaiting_reply = moment
            elif event["origin"] == "owner_authored" and awaiting_reply is not None:
                gap = (moment - awaiting_reply).total_seconds()
                if 0 < gap <= MAX_GAP_SECONDS:
                    samples[_bucket_of(moment.astimezone(zone).hour)].append(int(gap))
                    total += 1
                awaiting_reply = None
    if total < MIN_PROFILE_SAMPLES:
        note = (
            f"only {total} reliable owner reply samples, need {MIN_PROFILE_SAMPLES}; "
            "reply_delay_cold keeps its fallback range"
        )
        return {}, total, note
    return _percentile_profile(samples), total, ""
