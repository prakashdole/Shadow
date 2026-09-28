"""Pacing — when a reply is due, and how long the owner usually takes (D20, D41).

Pure functions plus an injected clock. Nothing here reads the wall clock on its
own and nothing sleeps, so a test can assert exact due times and a dry run cannot
be surprised by real time passing.

Four decisions worth stating, because they are judgement calls rather than
transcriptions of the settings table:

* Momentum is decided by the thread's own last outbound, not by the last
  anything happened. A contact writing five messages in a row does not make the
  conversation fast; the owner having just replied does.
* Without a latency profile the cold delay is right-skewed, not uniform: a
  triangular distribution with its mode near the low end. A uniform delay would
  put as many replies at eight minutes as at two, which reads as a machine.
* With a profile the delay is the owner's own p25/p75 for that time of day,
  clipped to `reply_delay_cold_clip`. The clip is what stops an extreme
  measurement from producing an extreme behaviour.
* A due time outside `active_hours` moves to the next window start plus a FRESH
  cold sample, and the caller is told to reassess. It is never simply delayed by
  a constant and never sent early: the conversation will be re-read first.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from shadow.settings import Settings
from shadow.storage import LATENCY_BUCKETS, iso, parse_iso

# Fraction of the cold fallback span at which the no-profile mode sits. Low, so
# most sampled delays land near the fast end of the band.
COLD_MODE_FRACTION = 0.25


class Clock(Protocol):
    """The only source of time in the coordinator. Injected everywhere so tests
    can move time deliberately and a dry run cannot be surprised by it."""

    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock. The default for a live run; never used by the tests,
    which pass a ManualClock instead."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ManualClock:
    """A clock the test drives. `sleep` advances time instead of waiting, so a
    test never spends real seconds and never flakes on a loaded machine."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.current = self.current + timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


def bucket_for_hour(hour: int) -> str:
    """The owner's latency bucket for a local hour.

    The boundaries are the ones `Storage.store_latency_profile` validates, which
    is why the tuple is read from storage rather than written here; a test pins
    the two together so they cannot drift.
    """
    if hour < 0 or hour > 23:
        raise ValueError(f"hour must be 0-23, got {hour}")
    for index, name in enumerate(LATENCY_BUCKETS):
        low = index * 6
        if low <= hour < low + 6:
            return name
    return LATENCY_BUCKETS[-1]


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sample_delay(
    settings: Settings,
    *,
    momentum: bool,
    profile: dict | None,
    local_hour: int,
    rng: random.Random,
) -> float:
    """Seconds to wait before replying to one pending batch.

    Sampled ONCE per pending batch, not per message, so a burst does not get a
    longer and longer delay as more messages arrive.
    """
    if momentum:
        low, high = settings.reply_delay_momentum
        return float(rng.uniform(low, high))
    cold_low, cold_high = settings.reply_delay_cold
    if not profile:
        mode = cold_low + COLD_MODE_FRACTION * (cold_high - cold_low)
        return float(rng.triangular(cold_low, cold_high, mode))
    bucket = profile.get(bucket_for_hour(local_hour))
    if not bucket:
        # A profile with no row for this hour is not a licence to guess: fall
        # back to the band rather than treating a missing bucket as zero.
        mode = cold_low + COLD_MODE_FRACTION * (cold_high - cold_low)
        return float(rng.triangular(cold_low, cold_high, mode))
    clip_low, clip_high = settings.reply_delay_cold_clip
    return float(
        _clip(
            rng.triangular(
                float(bucket["p25"]), float(bucket["p75"]), float(bucket["p50"])
            ),
            clip_low,
            clip_high,
        )
    )


def is_momentum(
    now: datetime, last_outbound_at: datetime | str | None, settings: Settings
) -> bool:
    """True when the owner replied in this thread within `momentum_window`.

    A missing or unparseable last outbound is not momentum: assuming momentum
    from absent data is how a quiet thread starts replying at fifteen seconds.
    """
    if last_outbound_at is None:
        return False
    last = (
        parse_iso(last_outbound_at)
        if isinstance(last_outbound_at, str)
        else last_outbound_at
    )
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    return timedelta(0) <= (now - last) <= timedelta(seconds=settings.momentum_window)


def due_time(
    *,
    first_pending_at: datetime,
    last_inbound_at: datetime,
    sampled_delay: float,
    settings: Settings,
) -> datetime:
    """Latest inbound plus the sampled delay, capped by `coalesce_extension_max`.

    The cap is measured from the ORIGINAL due time (first pending + delay), not
    from the latest event, so a long stream of inbound messages cannot push a
    reply arbitrarily far out. Hitting the cap is the caller's signal to re-read
    and form a new bounded batch (D41).
    """
    delay = timedelta(seconds=float(sampled_delay))
    unbounded = last_inbound_at + delay
    ceiling = first_pending_at + delay + timedelta(seconds=settings.coalesce_extension_max)
    return min(unbounded, ceiling)


def _zone(settings: Settings) -> ZoneInfo:
    if not settings.timezone:
        raise ValueError(
            "timezone is not configured; active hours cannot be evaluated "
            "(settings.validate_for_mode refuses any sending mode without it)"
        )
    return ZoneInfo(settings.timezone)


_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _window_for(settings: Settings, local: datetime) -> tuple[timedelta, timedelta] | None:
    hours = settings.active_hours
    if not hours:
        return None
    window = hours.get(_WEEKDAY_KEYS[local.weekday()])
    if window is None:
        window = hours.get("daily")
    if window is None:
        return None
    return (
        timedelta(hours=int(window[0][:2]), minutes=int(window[0][3:])),
        timedelta(hours=int(window[1][:2]), minutes=int(window[1][3:])),
    )


def in_active_hours(now: datetime, settings: Settings) -> bool:
    """True inside the owner's waking window in the configured timezone.

    No timezone or no schedule means False, not True: with no schedule the
    conservative answer is that Shadow does not know when the owner is awake.
    """
    if not settings.timezone or not settings.active_hours:
        return False
    local = now.astimezone(_zone(settings))
    window = _window_for(settings, local)
    if window is None:
        return False
    start, end = window
    since_midnight = timedelta(
        hours=local.hour, minutes=local.minute, seconds=local.second
    )
    return start <= since_midnight < end


def next_window_start(now: datetime, settings: Settings) -> datetime:
    """The next moment inside active hours, in UTC.

    Scans forward day by day so a per-weekday schedule (active on weekdays only)
    still resolves, and so a window that has already closed today lands on
    tomorrow's opening rather than looping forever.
    """
    if not settings.timezone or not settings.active_hours:
        raise ValueError("active_hours and timezone must both be configured")
    zone = _zone(settings)
    local = now.astimezone(zone)
    for day_offset in range(0, 8):
        day = (local + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        window = _window_for(settings, day)
        if window is None:
            continue
        start, _ = window
        candidate = day + start
        if candidate > local:
            return candidate.astimezone(now.tzinfo)
    raise ValueError(
        "no active window in the next 8 days; the schedule has no days defined"
    )


def apply_active_hours(
    due: datetime,
    settings: Settings,
    *,
    rng: random.Random,
    profile: dict | None = None,
) -> tuple[datetime, bool]:
    """Move a due time that falls outside active hours. Returns (due, reassess).

    `reassess` is True whenever the time moved, because a reply queued for the
    next morning is decided against a conversation nobody has re-read since. The
    caller must re-read and re-evaluate; it must not send the stale draft.
    """
    if in_active_hours(due, settings):
        return due, False
    start = next_window_start(due, settings)
    local_hour = start.astimezone(_zone(settings)).hour
    delay = sample_delay(
        settings, momentum=False, profile=profile, local_hour=local_hour, rng=rng
    )
    return start + timedelta(seconds=delay), True


def local_hour(now: datetime, settings: Settings) -> int:
    """The owner's local hour, for looking up a latency bucket. Falls back to the
    UTC hour when no zone is configured, which is only reachable in observe."""
    if settings.timezone:
        return now.astimezone(_zone(settings)).hour
    return now.hour


def poll_interval(now: datetime, settings: Settings) -> int:
    """Seconds until the next inbox poll: `quiet_poll_interval` outside active
    hours, `inbox_check_interval` inside. Urgent detection is the only reason to
    poll outside the window."""
    if in_active_hours(now, settings):
        return settings.inbox_check_interval
    return settings.quiet_poll_interval


def iso_or_none(when: datetime | None) -> str | None:
    return None if when is None else iso(when)
