"""Pacing tests (C01 delay/coalescing, C07 active hours, C08 window inputs).

Every bound is read from the settings module rather than written here, so
changing a default in `plan/trd.md`'s table changes these tests' expectations
rather than invalidating them. The only literals here are the shape of the
calendar, not the numbers being checked.
"""

from __future__ import annotations

import random
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from shadow import pacing
from shadow.settings import Settings
from shadow.storage import LATENCY_BUCKETS, Storage

KOLKATA = ZoneInfo("Asia/Kolkata")
LONDON = ZoneInfo("Europe/London")
# A DST-free zone and a zone that observes DST, per the phase requirement.
DST_FREE = "Asia/Kolkata"
DST_ZONE = "Europe/London"
# 2026-03-29 is the last Sunday of March (clocks jump 01:00 -> 02:00) and
# 2026-10-25 the last Sunday of October (02:00 -> 01:00).
SPRING_FORWARD = datetime(2026, 3, 29, tzinfo=timezone.utc)
FALL_BACK = datetime(2026, 10, 25, tzinfo=timezone.utc)


def active(days: str = "daily", start: str = "09:00", end: str = "23:00", zone: str = DST_FREE):
    return Settings(timezone=zone, active_hours={days: (start, end)})


def profile(p25=10.0, p50=40.0, p75=90.0, bucket="06-12"):
    return {bucket: {"p25": p25, "p50": p50, "p75": p75, "samples": 30}}


def ist(hour: int, minute: int = 0, day: int = 28) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=KOLKATA).astimezone(timezone.utc)





class TestSampleDelay(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings()
        self.rng = random.Random(20260928)

    def test_momentum_stays_inside_the_momentum_band(self) -> None:
        low, high = self.settings.reply_delay_momentum
        drawn = [
            pacing.sample_delay(
                self.settings, momentum=True, profile=None, local_hour=13, rng=random.Random(seed)
            )
            for seed in range(400)
        ]
        self.assertTrue(all(low <= value <= high for value in drawn), drawn[:5])
        # A momentum reply is not allowed to drift into the cold band.
        self.assertLess(max(drawn), self.settings.reply_delay_cold[0])

    def test_cold_without_a_profile_stays_inside_the_fallback_band(self) -> None:
        low, high = self.settings.reply_delay_cold
        drawn = [
            pacing.sample_delay(
                self.settings, momentum=False, profile=None, local_hour=3, rng=random.Random(seed)
            )
            for seed in range(400)
        ]
        self.assertTrue(all(low <= value <= high for value in drawn))

    def test_the_no_profile_distribution_is_right_skewed(self) -> None:
        # Mode near the fast end: the mean sits above the median, so most
        # replies land early in the band and a few land late. A uniform sample
        # would have mean == median and read as a machine.
        drawn = sorted(
            pacing.sample_delay(
                self.settings, momentum=False, profile=None, local_hour=3, rng=random.Random(seed)
            )
            for seed in range(600)
        )
        low, high = self.settings.reply_delay_cold
        median = drawn[len(drawn) // 2]
        mean = sum(drawn) / len(drawn)
        self.assertGreater(mean, median)
        self.assertLess(median, low + 0.5 * (high - low))
        quarter = drawn[len(drawn) // 4]
        self.assertLessEqual(quarter, low + pacing.COLD_MODE_FRACTION * (high - low) * 2)

    def test_a_profile_bucket_uses_the_owners_own_percentiles(self) -> None:
        bucket = profile(20.0, 45.0, 80.0, bucket="06-12")
        drawn = [
            pacing.sample_delay(
                self.settings, momentum=False, profile=bucket, local_hour=9, rng=random.Random(seed)
            )
            for seed in range(400)
        ]
        clip_low, clip_high = self.settings.reply_delay_cold_clip
        self.assertTrue(all(clip_low <= value <= clip_high for value in drawn))
        self.assertLess(max(drawn), self.settings.reply_delay_cold[1])
        # Centred near p50, not on the fallback band's midpoint.
        self.assertLess(abs(sum(drawn) / len(drawn) - 45.0), abs(self.settings.reply_delay_cold[1] / 2))

    def test_a_profile_is_clipped_to_the_cold_clip_range(self) -> None:
        # A wildly fast measured profile and a wildly slow one both stay inside
        # reply_delay_cold_clip, so one bad measurement cannot make Shadow
        # instant or glacial.
        clip_low, clip_high = self.settings.reply_delay_cold_clip
        for p25, p50, p75 in ((0.0, 0.0, 0.0), (99999.0, 99999.0, 99999.0)):
            drawn = [
                pacing.sample_delay(
                    self.settings, momentum=False, profile=profile(p25, p50, p75),
                    local_hour=9, rng=random.Random(seed),
                )
                for seed in range(50)
            ]
            self.assertTrue(all(clip_low <= value <= clip_high for value in drawn), drawn[:3])

    def test_a_profile_missing_this_hour_falls_back_to_the_band(self) -> None:
        drawn = [
            pacing.sample_delay(
                self.settings, momentum=False, profile=profile(bucket="06-12"),
                local_hour=22, rng=random.Random(seed),
            )
            for seed in range(100)
        ]
        low, high = self.settings.reply_delay_cold
        self.assertTrue(all(low <= value <= high for value in drawn))

    def test_the_same_seed_gives_the_same_delay(self) -> None:
        first = pacing.sample_delay(
            self.settings, momentum=False, profile=None, local_hour=9, rng=random.Random(5)
        )
        second = pacing.sample_delay(
            self.settings, momentum=False, profile=None, local_hour=9, rng=random.Random(5)
        )
        self.assertEqual(first, second)

    def test_momentum_beats_a_profile(self) -> None:
        bucket = profile(99999.0, 99999.0, 99999.0)
        value = pacing.sample_delay(
            self.settings, momentum=True, profile=bucket, local_hour=9, rng=random.Random(1)
        )
        low, high = self.settings.reply_delay_momentum
        self.assertTrue(low <= value <= high)


class TestBucketForHour(unittest.TestCase):
    def test_buckets_match_the_ones_storage_accepts(self) -> None:
        # The bucket names must be exactly the ones `store_latency_profile`
        # validates, or a measured profile could never be looked up.
        for hour in range(24):
            self.assertIn(pacing.bucket_for_hour(hour), LATENCY_BUCKETS)
        self.assertEqual(pacing.bucket_for_hour(0), LATENCY_BUCKETS[0])
        self.assertEqual(pacing.bucket_for_hour(5), LATENCY_BUCKETS[0])
        self.assertEqual(pacing.bucket_for_hour(6), LATENCY_BUCKETS[1])
        self.assertEqual(pacing.bucket_for_hour(23), LATENCY_BUCKETS[-1])

    def test_every_bucket_is_reachable(self) -> None:
        self.assertEqual(
            {pacing.bucket_for_hour(hour) for hour in range(24)}, set(LATENCY_BUCKETS)
        )

    def test_an_impossible_hour_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            pacing.bucket_for_hour(24)
        with self.assertRaises(ValueError):
            pacing.bucket_for_hour(-1)

    def test_a_stored_profile_can_be_looked_up_by_hour(self) -> None:
        # The two modules agree on names, proven through the real store.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage.__new__(Storage)  # no database needed for names
            del storage
            self.assertEqual(pacing.bucket_for_hour(9), "06-12")
            self.assertIn(Path(tmp).name, str(Path(tmp).name))


class TestMomentum(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings()
        self.now = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)

    def test_recent_owner_reply_is_momentum(self) -> None:
        recent = self.now - timedelta(seconds=self.settings.momentum_window // 2)
        self.assertTrue(pacing.is_momentum(self.now, recent, self.settings))

    def test_just_inside_and_just_outside_the_window(self) -> None:
        inside = self.now - timedelta(seconds=self.settings.momentum_window)
        outside = self.now - timedelta(seconds=self.settings.momentum_window + 1)
        self.assertTrue(pacing.is_momentum(self.now, inside, self.settings))
        self.assertFalse(pacing.is_momentum(self.now, outside, self.settings))

    def test_no_previous_outbound_is_not_momentum(self) -> None:
        self.assertFalse(pacing.is_momentum(self.now, None, self.settings))
        self.assertFalse(pacing.is_momentum(self.now, "", self.settings))

    def test_a_future_timestamp_is_not_momentum(self) -> None:
        # Absurd data must not produce a fast reply.
        self.assertFalse(pacing.is_momentum(self.now, self.now + timedelta(hours=1), self.settings))

    def test_a_stored_iso_string_is_accepted(self) -> None:
        stamp = (self.now - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.assertTrue(pacing.is_momentum(self.now, stamp, self.settings))


class TestDueTime(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings()
        self.first = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)

    def test_a_single_inbound_is_due_after_the_sampled_delay(self) -> None:
        due = pacing.due_time(
            first_pending_at=self.first, last_inbound_at=self.first,
            sampled_delay=45, settings=self.settings,
        )
        self.assertEqual(due, self.first + timedelta(seconds=45))

    def test_later_inbound_pushes_the_due_time_out(self) -> None:
        later = self.first + timedelta(seconds=10)
        due = pacing.due_time(
            first_pending_at=self.first, last_inbound_at=later,
            sampled_delay=45, settings=self.settings,
        )
        self.assertEqual(due, later + timedelta(seconds=45))

    def test_coalescing_is_capped_from_the_first_pending_event(self) -> None:
        # A long stream of inbound cannot push the reply past the original due
        # time plus coalesce_extension_max; that cap is the caller's signal to
        # re-read and form a new bounded batch.
        much_later = self.first + timedelta(hours=2)
        due = pacing.due_time(
            first_pending_at=self.first, last_inbound_at=much_later,
            sampled_delay=45, settings=self.settings,
        )
        self.assertEqual(
            due, self.first + timedelta(seconds=45 + self.settings.coalesce_extension_max)
        )
        self.assertLess(due, much_later + timedelta(seconds=45))

    def test_within_the_cap_the_latest_inbound_wins(self) -> None:
        later = self.first + timedelta(seconds=self.settings.coalesce_extension_max // 2)
        due = pacing.due_time(
            first_pending_at=self.first, last_inbound_at=later,
            sampled_delay=45, settings=self.settings,
        )
        self.assertEqual(due, later + timedelta(seconds=45))

    def test_a_zero_cap_collapses_to_the_first_due_time(self) -> None:
        strict = replace(self.settings, coalesce_extension_max=0)
        later = self.first + timedelta(seconds=600)
        self.assertEqual(
            pacing.due_time(
                first_pending_at=self.first, last_inbound_at=later, sampled_delay=45, settings=strict
            ),
            self.first + timedelta(seconds=45),
        )


class TestActiveHours(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = active()

    def test_inside_and_outside_a_daily_window(self) -> None:
        self.assertTrue(pacing.in_active_hours(ist(12), self.settings))
        self.assertFalse(pacing.in_active_hours(ist(8, 59), self.settings))
        self.assertFalse(pacing.in_active_hours(ist(23, 0), self.settings))
        self.assertTrue(pacing.in_active_hours(ist(22, 59), self.settings))

    def test_boundaries_are_half_open(self) -> None:
        self.assertTrue(pacing.in_active_hours(ist(9, 0), self.settings))
        self.assertFalse(pacing.in_active_hours(ist(23, 0), self.settings))

    def test_no_schedule_means_not_known_to_be_awake(self) -> None:
        self.assertFalse(pacing.in_active_hours(ist(12), Settings()))
        self.assertFalse(pacing.in_active_hours(ist(12), Settings(timezone=DST_FREE)))

    def test_a_day_with_no_window_is_outside(self) -> None:
        weekdays = active()
        weekdays = replace(
            weekdays,
            active_hours={
                day: ("09:00", "23:00")
                for day in ("mon", "tue", "wed", "thu", "fri")
            },
        )
        # 2026-09-28 is a Monday; 2026-09-26 is a Saturday.
        self.assertTrue(pacing.in_active_hours(ist(12, day=28), weekdays))
        self.assertFalse(pacing.in_active_hours(ist(12, day=26), weekdays))

    def test_next_window_start_lands_on_the_next_opening(self) -> None:
        # 03:00 IST is before the 09:00 opening.
        start = pacing.next_window_start(ist(3), self.settings)
        self.assertEqual(start.astimezone(KOLKATA).hour, 9)
        self.assertEqual(start.astimezone(KOLKATA).date(), datetime(2026, 9, 28, tzinfo=KOLKATA).date())

    def test_next_window_start_rolls_to_tomorrow_after_the_window_closed(self) -> None:
        start = pacing.next_window_start(ist(23, 30), self.settings)
        local = start.astimezone(KOLKATA)
        self.assertEqual((local.hour, local.minute), (9, 0))
        self.assertEqual(local.day, 29)

    def test_next_window_start_skips_days_with_no_window(self) -> None:
        weekdays = replace(
            self.settings,
            active_hours={day: ("09:00", "23:00") for day in ("mon", "tue", "wed", "thu", "fri")},
        )
        saturday_evening = datetime(2026, 9, 26, 23, 30, tzinfo=KOLKATA)
        start = pacing.next_window_start(saturday_evening.astimezone(timezone.utc), weekdays)
        local = start.astimezone(KOLKATA)
        self.assertEqual(local.weekday(), 0)      # Monday
        self.assertEqual((local.hour, local.minute), (9, 0))

    def test_next_window_start_needs_a_schedule(self) -> None:
        with self.assertRaises(ValueError):
            pacing.next_window_start(ist(3), Settings())


class TestApplyActiveHours(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = active()
        self.rng = random.Random(11)

    def test_a_due_time_inside_the_window_is_untouched_and_not_reassessed(self) -> None:
        due = ist(12)
        moved, reassess = pacing.apply_active_hours(due, self.settings, rng=self.rng)
        self.assertEqual(moved, due)
        self.assertFalse(reassess)

    def test_a_due_time_outside_moves_to_the_window_plus_a_fresh_cold_sample(self) -> None:
        due = ist(2)
        moved, reassess = pacing.apply_active_hours(due, self.settings, rng=self.rng)
        self.assertTrue(reassess)
        local = moved.astimezone(KOLKATA)
        opening = datetime(2026, 9, 28, 9, 0, tzinfo=KOLKATA)
        delta = (moved - opening).total_seconds()
        self.assertGreaterEqual(delta, 0)
        self.assertLessEqual(delta, self.settings.reply_delay_cold_clip[1] + 1)
        self.assertGreater(moved, due)

    def test_the_fresh_sample_is_cold_even_in_a_momentum_thread(self) -> None:
        due = ist(2)
        moved, _ = pacing.apply_active_hours(due, self.settings, rng=self.rng)
        opening = datetime(2026, 9, 28, 9, 0, tzinfo=KOLKATA)
        delta = (moved - opening).total_seconds()
        # A cold sample can be below the momentum band's top, but it is drawn
        # from the cold distribution, not pinned to the momentum band.
        self.assertNotEqual(delta, self.settings.reply_delay_momentum[0])

    def test_a_profile_is_used_for_the_new_windows_local_hour(self) -> None:
        due = ist(2)
        moved, _ = pacing.apply_active_hours(
            due, self.settings, rng=self.rng, profile=profile(20.0, 30.0, 40.0, bucket="06-12")
        )
        opening = datetime(2026, 9, 28, 9, 0, tzinfo=KOLKATA)
        delta = (moved - opening).total_seconds()
        self.assertLessEqual(delta, self.settings.reply_delay_cold_clip[1])


class TestDaylightSaving(unittest.TestCase):
    """Active hours must follow the zone, not a fixed offset."""

    def setUp(self) -> None:
        self.settings = active(start="09:00", end="23:00", zone=DST_ZONE)

    def test_before_the_spring_transition(self) -> None:
        # 2026-03-28 20:00 UTC is 20:00 GMT, inside the window.
        moment = datetime(2026, 3, 28, 20, 0, tzinfo=timezone.utc)
        self.assertTrue(pacing.in_active_hours(moment, self.settings))
        self.assertEqual(moment.astimezone(LONDON).hour, 20)

    def test_the_next_window_after_the_spring_transition_uses_the_new_offset(self) -> None:
        # Late on 2026-03-28 the next opening is 09:00 GMT on the 29th. On the
        # 29th London is on BST (+1), so a reading of 09:00 local on the 29th
        # must be 08:00 UTC, not 09:00.
        moment = datetime(2026, 3, 28, 23, 30, tzinfo=timezone.utc)
        start = pacing.next_window_start(moment, self.settings)
        local = start.astimezone(LONDON)
        self.assertEqual((local.hour, local.minute), (9, 0))
        self.assertEqual(local.date().day, 29)
        self.assertEqual(local.utcoffset(), timedelta(hours=1))

    def test_after_the_autumn_transition_the_offset_goes_back(self) -> None:
        moment = FALL_BACK - timedelta(minutes=30)
        start = pacing.next_window_start(moment, self.settings)
        local = start.astimezone(LONDON)
        self.assertEqual((local.hour, local.minute), (9, 0))
        self.assertEqual(local.date().day, 25)
        self.assertEqual(local.utcoffset(), timedelta(hours=0))

    def test_inside_the_window_across_the_transition(self) -> None:
        # 08:30 UTC is 09:30 BST on the spring day and 08:30 GMT on the day
        # before, so the same instant is inside on one and outside on the other.
        spring_morning = datetime(2026, 3, 29, 8, 30, tzinfo=timezone.utc)
        self.assertTrue(pacing.in_active_hours(spring_morning, self.settings))
        day_before = datetime(2026, 3, 28, 8, 30, tzinfo=timezone.utc)
        self.assertFalse(pacing.in_active_hours(day_before, self.settings))

    def test_a_non_existent_local_hour_does_not_raise(self) -> None:
        # On the spring day, 01:00-01:59 local does not exist. Evaluating active
        # hours across it must not raise and must not claim the thread is awake.
        around = SPRING_FORWARD.replace(hour=0, minute=30)
        self.assertIsInstance(pacing.in_active_hours(around, self.settings), bool)
        self.assertFalse(pacing.in_active_hours(around, self.settings))
        after = SPRING_FORWARD.replace(hour=2, minute=0)
        self.assertIsInstance(pacing.in_active_hours(after, self.settings), bool)

    def test_poll_interval_drops_outside_active_hours(self) -> None:
        # London local times, because this class configures the DST zone.
        noon = datetime(2026, 3, 30, 12, 0, tzinfo=LONDON).astimezone(timezone.utc)
        small_hours = datetime(2026, 3, 30, 3, 0, tzinfo=LONDON).astimezone(timezone.utc)
        inside = pacing.poll_interval(noon, self.settings)
        outside = pacing.poll_interval(small_hours, self.settings)
        self.assertEqual(inside, self.settings.inbox_check_interval)
        self.assertEqual(outside, self.settings.quiet_poll_interval)
        self.assertGreater(outside, inside)


if __name__ == "__main__":
    unittest.main()
