"""Settings — the single source of truth for every runtime number.

Every field name below is a name from the TRD settings table
(plan/trd.md, "Settings (single source of truth)"). tests/test_settings.py
parses that table and fails if a name is missing here, so the two cannot
drift. No other module may hard-code a number that belongs to a setting
here; refer to the field or the setting name instead.

Shapes:

* durations are seconds (``int``)
* ranges are ``(min, max)`` seconds
* rates are ``(count, window_seconds)``; a field with an hourly and a daily
  cap holds a tuple of such rates
* ``urgent_repeat_schedule`` is a strictly increasing tuple of seconds

``active_hours`` and ``timezone`` have no default: owner setup must supply
them, and any mode other than ``observe`` refuses to start without them.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILENAME = "settings.toml"
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class SettingsError(Exception):
    """Invalid, unknown or out-of-range setting. Names the setting."""


# --- value checkers ---------------------------------------------------
# One checker per kind. Each takes (name, value) and raises SettingsError
# naming the setting, so a bad file can never load half-applied.


def _fail(name: str, value: object, why: str) -> None:
    raise SettingsError(f"{name}: {why} (got {value!r})")


def _ints(name, value, lo=0, hi=None):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(name, value, f"expected an integer >= {lo}")
    if value < lo or (hi is not None and value > hi):
        _fail(name, value, f"must be between {lo} and {hi}")


def _int(name, value):
    _ints(name, value)


def _pair(name, value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        _fail(name, value, "expected a (min, max) pair of seconds")
    for part in value:
        _ints(name, part)
    if value[0] > value[1]:
        _fail(name, value, "min must not exceed max")


def _rates(name, value):
    if not isinstance(value, (list, tuple)) or not value:
        _fail(name, value, "expected one or more (count, window_seconds) rates")
    for rate in value:
        if not isinstance(rate, (list, tuple)) or len(rate) != 2:
            _fail(name, value, "expected one or more (count, window_seconds) rates")
        _ints(name, rate[0])
        _ints(name, rate[1], hi=None)
        if rate[1] == 0:
            _fail(name, rate, "window_seconds must be positive")


def _schedule(name, value):
    if not isinstance(value, (list, tuple)) or not value:
        _fail(name, value, "expected a non-empty list of offsets in seconds")
    previous = None
    for offset in value:
        _ints(name, offset)
        if previous is not None and offset <= previous:
            _fail(name, value, "offsets must strictly increase")
        previous = offset


def _enum(name, value, *allowed):
    if value not in allowed:
        _fail(name, value, "expected one of " + ", ".join(allowed))


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        _fail(name, value, "expected a non-empty string")


def _mode(name, value):
    _enum(name, value, "observe", "assisted", "autonomous")


def _scope(name, value):
    """`none`, `known`, or the owner's explicit list of contacts."""
    if isinstance(value, str):
        _enum(name, value, "none", "known")
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, str) or not item.strip():
                _fail(name, value, "contact list must hold non-empty strings")
    else:
        _fail(name, value, "expected 'none', 'known' or a list of contacts")


def _clock(value):
    """'HH:MM' as (hour, minute) — raises ValueError for anything else."""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("not HH:MM")
    hour, minute = value[:2], value[3:]
    if not (hour.isdigit() and minute.isdigit()):
        raise ValueError("not digits")
    h, m = int(hour), int(minute)
    if h > 23 or m > 59:
        raise ValueError("out of range")
    return h, m


def _active_hours(name, value):
    if value is None:  # no default: owner setup supplies it
        return
    if not isinstance(value, dict) or not value:
        _fail(name, value, 'expected {"daily": ["09:00","23:00"]} or per weekday')
    if "daily" in value and len(value) > 1:
        _fail(name, value, '"daily" cannot be combined with weekday keys')
    unknown = set(value) - {"daily", *_WEEKDAYS}
    if unknown:
        _fail(name, sorted(unknown), "unknown day key(s)")
    for day, window in value.items():
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            _fail(name, window, "expected [start, end] as 'HH:MM'")
        try:
            start, end = _clock(window[0]), _clock(window[1])
        except ValueError:
            _fail(name, window, "expected 'HH:MM' strings")
        if start >= end:
            _fail(name, window, "start must be earlier than end")


def _timezone(name, value):
    if value is None:
        return
    _text(name, value)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        _fail(name, value, "not a known IANA timezone")


def _confusion(name, value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        _fail(name, value, "expected (rate_percent, window_seconds, min_turns)")
    _ints(name, value[0], hi=100)
    _ints(name, value[1], hi=None)
    _ints(name, value[2], hi=None)
    for part in value[1:]:
        if part == 0:
            _fail(name, value, "window and turn count must be positive")


CHECKS = {
    "mode": _mode,
    "autonomy_scope": _scope,
    "timezone": _timezone,
    "active_hours": _active_hours,
    "reply_delay_momentum": _pair,
    "momentum_window": _int,
    "reply_delay_cold": _pair,
    "reply_delay_cold_clip": _pair,
    "coalesce_extension_max": _int,
    "quick_coalesce": _int,
    "takeover_window": _int,
    "stale_reeval": _int,
    "draft_ttl": _int,
    "burst_max_items": _int,
    "bubble_max_chars": _int,
    "reply_max_chars": _int,
    "send_cap_account": _rates,
    "send_cap_thread": _rates,
    "pane_check_interval": _int,
    "inbox_check_interval": _int,
    "quiet_poll_interval": _int,
    "idle_backoff_max": _int,
    "navigation_budget": _rates,
    "risk_call_budget": _rates,
    "reply_call_budget": _rates,
    "extraction_call_budget": _rates,
    "provider_concurrency": _int,
    "telegram_outage_grace": _int,
    "urgent_repeat_schedule": _schedule,
    "secondary_alert_after": _int,
    "digest_schedule": _text,
    "confusion_downgrade": _confusion,
}

@dataclass(frozen=True)
class Settings:
    """Every TRD setting. Immutable: changing one means replacing the whole
    object, which is how a mode switch invalidates queued work."""

    # Operating mode and scope.
    mode: str = "observe"
    autonomy_scope: str | list[str] = "none"
    active_hours: dict[str, tuple[str, str]] | None = None
    timezone: str | None = None  # IANA zone; no default, owner setup supplies it

    # Pacing.
    reply_delay_momentum: tuple[int, int] = (15, 90)
    momentum_window: int = 600
    reply_delay_cold: tuple[int, int] = (120, 480)  # with no latency profile
    reply_delay_cold_clip: tuple[int, int] = (60, 1200)
    coalesce_extension_max: int = 300
    quick_coalesce: int = 3
    takeover_window: int = 1800
    stale_reeval: int = 1800
    draft_ttl: int = 3600

    # Transport ceilings (safety, not style targets).
    burst_max_items: int = 4
    bubble_max_chars: int = 500
    reply_max_chars: int = 1200

    # Caps and budgets: (count, window_seconds).
    send_cap_account: tuple[tuple[int, int], ...] = ((20, 3600), (100, 86400))
    send_cap_thread: tuple[tuple[int, int], ...] = ((6, 3600),)

    # Pacing of reads and backoff.
    pane_check_interval: int = 15
    inbox_check_interval: int = 60
    quiet_poll_interval: int = 300
    idle_backoff_max: int = 300
    navigation_budget: tuple[tuple[int, int], ...] = ((60, 3600), (600, 86400))

    # Paid-call budgets, one per role: (count, window_seconds).
    risk_call_budget: tuple[tuple[int, int], ...] = ((120, 3600),)
    reply_call_budget: tuple[tuple[int, int], ...] = ((60, 3600),)
    extraction_call_budget: tuple[tuple[int, int], ...] = ((60, 3600),)
    provider_concurrency: int = 2

    # Owner control surface.
    telegram_outage_grace: int = 900
    urgent_repeat_schedule: tuple[int, ...] = (300, 900, 1800, 3600)
    secondary_alert_after: int = 900
    digest_schedule: str = "end_of_active_hours"
    confusion_downgrade: tuple[int, int, int] = (10, 604800, 30)


FIELD_NAMES = tuple(f.name for f in fields(Settings))
assert set(FIELD_NAMES) == set(CHECKS), (
    sorted(set(FIELD_NAMES) ^ set(CHECKS))
)


def _tuplify(value):
    if isinstance(value, list):
        return tuple(_tuplify(v) for v in value)
    return value


def validate(settings: Settings) -> Settings:
    """Check every field. Returns the settings with list values normalised to
    tuples so the frozen object stays comparable and hashable."""
    values = {name: _tuplify(getattr(settings, name)) for name in FIELD_NAMES}
    for name, check in CHECKS.items():
        check(name, values[name])
    hours = values["active_hours"]
    if hours is not None:
        values["active_hours"] = {day: tuple(w) for day, w in hours.items()}
    return replace(settings, **values)


def data_dir() -> Path:
    """The private data directory. Never inside the repository.

    `SHADOW_DATA_DIR` must be absolute when set; otherwise the OS user data
    location is used. A path inside this checkout raises instead of quietly
    writing owner data into Git-tracked territory.
    """
    raw = os.environ.get("SHADOW_DATA_DIR")
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise SettingsError(
                f"SHADOW_DATA_DIR must be an absolute path, got {raw!r}"
            )
    elif sys.platform == "darwin":
        candidate = Path.home() / "Library/Application Support/Shadow"
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
        candidate = Path(base) / "Shadow"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        candidate = (Path(xdg) / "shadow") if xdg else (
            Path.home() / ".local/share/shadow"
        )
    resolved = candidate.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise SettingsError(
            f"data directory {resolved} is inside the repository; "
            "point SHADOW_DATA_DIR outside the checkout"
        )
    return resolved


def settings_path(directory: Path | None = None) -> Path:
    return (directory if directory is not None else data_dir()) / SETTINGS_FILENAME


def load_settings(directory: Path | None = None) -> Settings:
    """Load `<data_dir>/settings.toml`, or defaults when it does not exist.

    An unknown key, a wrong type or an out-of-range value raises
    SettingsError naming the setting; a partially applied file never loads.
    """
    path = settings_path(directory)
    if not path.exists():
        return Settings()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"{path}: cannot read settings: {exc}") from exc

    known = set(FIELD_NAMES)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise SettingsError(f"{path}: unknown setting(s): {', '.join(unknown)}")

    settings = Settings()
    for name, value in raw.items():
        CHECKS[name](name, value)  # report the bad value before merging
        settings = replace(settings, **{name: value})
    return validate(settings)


def validate_for_mode(settings: Settings) -> Settings:
    """Refuse any mode that could send unless the owner's schedule is set."""
    settings = validate(settings)
    if settings.mode == "observe":
        return settings
    missing = [
        name
        for name in ("timezone", "active_hours")
        if getattr(settings, name) is None
    ]
    if missing:
        raise SettingsError(
            f"mode {settings.mode!r} requires {', '.join(missing)}; "
            "set them in settings.toml during setup"
        )
    _timezone("timezone", settings.timezone)
    return settings
