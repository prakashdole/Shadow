"""Applicability, overnight schedules and owner-current-update precedence."""

import unittest
from datetime import datetime, timezone

from shadow.context_memory import profile_context, validate_profile


class RoutineContext(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 10, tzinfo=timezone.utc)  # Monday
        self.profile = {"routine": {"timezone": "UTC", "entries": [
            {"weekdays": [0], "start": "09:00", "end": "13:00", "location": "class"}], "exceptions": []}}

    def test_routine_is_an_estimate_and_no_implicit_home(self):
        location = profile_context(self.profile, self.now)["location_context"]
        self.assertEqual(location["location"], "class")
        self.assertFalse(location["live_verified"])
        self.assertNotIn("location", profile_context(self.profile, self.now.replace(hour=14))["location_context"])

    def test_exception_suppresses_schedule_and_current_update_outranks_it(self):
        self.profile["routine"]["exceptions"] = [{"date": "2026-10-05"}]
        self.assertNotIn("location", profile_context(self.profile, self.now)["location_context"])
        self.profile["current_update"] = {"location": "library", "valid_from": "2026-10-05T09:00:00+00:00",
                                          "valid_until": "2026-10-05T12:00:00+00:00"}
        self.assertEqual(profile_context(self.profile, self.now)["location_context"]["location"], "library")
        self.assertNotIn("location", profile_context(self.profile, self.now.replace(hour=13))["location_context"])

    def test_overnight_uses_previous_weekday_and_overlaps_are_uncertain(self):
        self.profile["routine"]["entries"][0].update(start="22:00", end="02:00")
        tuesday = self.now.replace(day=6, hour=1)
        self.assertEqual(profile_context(self.profile, tuesday)["location_context"]["location"], "class")
        self.profile["routine"]["entries"].append({"weekdays": [1], "start": "00:00", "end": "03:00", "location": "elsewhere"})
        self.assertNotIn("location", profile_context(self.profile, tuesday)["location_context"])

    def test_invalid_profile_is_rejected_before_storage(self):
        for bad in ({"routine": "always home"}, {"current_update": {"location": "home"}},
                    {"routine": {"timezone": "not-a-zone", "entries": [], "exceptions": []}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_profile(bad)
