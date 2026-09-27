"""S00/TRD settings: one source of truth, validated on load, never in the repo."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shadow import settings as S

PLAN = S.REPO_ROOT / "plan" / "trd.md"


def _trd_setting_names() -> set[str]:
    """Backticked names in the first column of the TRD settings table."""
    text = PLAN.read_text(encoding="utf-8")
    section = text.split("## Settings (single source of truth)", 1)[1]
    section = section.split("\n## ", 1)[0]
    names = set()
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        first_cell = line.split("|")[1]
        for name in re.findall(r"`([^`]+)`", first_cell):
            for part in name.split(" / "):
                names.add(part.strip().strip("`"))
    return names


class TestSettings(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, body: str) -> Path:
        path = self.dir / S.SETTINGS_FILENAME
        path.write_text(body, encoding="utf-8")
        return path

    # --- defaults ----------------------------------------------------
    def test_defaults_load_without_a_file(self) -> None:
        loaded = S.load_settings(self.dir)
        self.assertEqual(loaded, S.Settings())
        self.assertEqual(loaded.mode, "observe")
        self.assertEqual(loaded.autonomy_scope, "none")
        self.assertIsNone(loaded.active_hours)
        self.assertIsNone(loaded.timezone)
        for name in S.FIELD_NAMES:
            if name in ("active_hours", "timezone"):
                continue  # no default: owner setup supplies these
            self.assertIsNotNone(getattr(loaded, name), name)

    @unittest.skipUnless(PLAN.exists(), "plan/trd.md not present in this checkout")
    def test_every_trd_table_name_is_a_field(self) -> None:
        names = _trd_setting_names()
        self.assertIn("reply_delay_momentum", names)  # parser sanity
        missing = sorted(names - set(S.FIELD_NAMES))
        self.assertEqual(missing, [], f"TRD settings missing from module: {missing}")

    @unittest.skipUnless(PLAN.exists(), "plan/trd.md not present in this checkout")
    def test_every_field_is_in_the_trd_table(self) -> None:
        extra = sorted(set(S.FIELD_NAMES) - _trd_setting_names())
        # Not extra settings, just extra fields on names the table already has:
        # timezone (no default, required at setup) and the clip window that
        # reply_delay_cold's "clipped 60 s-20 min" range needs as data.
        self.assertEqual(extra, ["reply_delay_cold_clip", "timezone"])

    def test_units_are_seconds_and_ranges(self) -> None:
        s = S.Settings()
        self.assertEqual(s.momentum_window, 10 * 60)
        self.assertEqual(s.quiet_poll_interval, 5 * 60)
        self.assertEqual(s.reply_delay_cold, (2 * 60, 8 * 60))
        self.assertEqual(s.reply_delay_cold_clip, (60, 20 * 60))
        self.assertEqual(s.urgent_repeat_schedule, (5 * 60, 15 * 60, 30 * 60, 60 * 60))
        for name in ("send_cap_account", "send_cap_thread", "navigation_budget",
                     "risk_call_budget", "reply_call_budget", "extraction_call_budget"):
            for count, window in getattr(s, name):
                self.assertGreater(count, 0, name)
                self.assertGreater(window, 0, name)

    # --- load-time validation ----------------------------------------
    def test_unknown_key_is_rejected_by_name(self) -> None:
        self.write('momentum_windwo = 10\n')
        with self.assertRaises(S.SettingsError) as ctx:
            S.load_settings(self.dir)
        self.assertIn("momentum_windwo", str(ctx.exception))

    def test_wrong_type_is_rejected_by_name(self) -> None:
        for body in (
            'momentum_window = "ten"\n',
            'momentum_window = true\n',
            'momentum_window = 1.5\n',
            'mode = 3\n',
            'active_hours = ["09:00", "23:00"]\n',
            'send_cap_account = 20\n',
        ):
            self.write(body)
            with self.assertRaises(S.SettingsError, msg=body) as ctx:
                S.load_settings(self.dir)
            self.assertIn(ctx.exception.args[0].split(":")[0], set(S.FIELD_NAMES))

    def test_negative_duration_is_rejected(self) -> None:
        self.write("momentum_window = -1\n")
        with self.assertRaises(S.SettingsError) as ctx:
            S.load_settings(self.dir)
        self.assertIn("momentum_window", str(ctx.exception))

    def test_out_of_range_and_malformed_values_are_rejected(self) -> None:
        cases = {
            "reply_delay_momentum = [90, 15]\n": "reply_delay_momentum",
            'digest_schedule = ""\n': "digest_schedule",
            'mode = "yolo"\n': "mode",
            "urgent_repeat_schedule = [300, 60]\n": "urgent_repeat_schedule",
            "send_cap_account = [[20, 0]]\n": "send_cap_account",
            "confusion_downgrade = [10, 604800]\n": "confusion_downgrade",
        }
        for body, name in cases.items():
            self.write(body)
            with self.assertRaises(S.SettingsError, msg=body) as ctx:
                S.load_settings(self.dir)
            self.assertIn(name, str(ctx.exception))

    def test_active_hours_forms(self) -> None:
        self.write('[active_hours]\ndaily = ["09:00", "23:00"]\n')
        self.assertEqual(
            S.load_settings(self.dir).active_hours, {"daily": ("09:00", "23:00")}
        )
        self.write('[active_hours]\nmon = ["09:00", "18:00"]\nsun = ["10:00", "12:00"]\n')
        self.assertEqual(
            S.load_settings(self.dir).active_hours,
            {"mon": ("09:00", "18:00"), "sun": ("10:00", "12:00")},
        )
        for body in (
            '[active_hours]\ndaily = ["23:00", "09:00"]\n',
            '[active_hours]\nmon = ["9:00", "18:00"]\n',
            '[active_hours]\nfunday = ["09:00", "18:00"]\n',
            '[active_hours]\ndaily = ["09:00", "18:00"]\nmon = ["09:00", "18:00"]\n',
        ):
            self.write(body)
            with self.assertRaises(S.SettingsError, msg=body):
                S.load_settings(self.dir)

    def test_toml_syntax_error_names_the_file(self) -> None:
        self.write("mode = \n")
        with self.assertRaises(S.SettingsError) as ctx:
            S.load_settings(self.dir)
        self.assertIn(S.SETTINGS_FILENAME, str(ctx.exception))

    # --- timezone and mode -------------------------------------------
    def test_invalid_timezone_is_rejected(self) -> None:
        self.write('timezone = "Mars/Olympus"\n')
        with self.assertRaises(S.SettingsError) as ctx:
            S.load_settings(self.dir)
        self.assertIn("timezone", str(ctx.exception))

    def test_valid_timezone_loads(self) -> None:
        self.write('timezone = "Asia/Kolkata"\n')
        self.assertEqual(S.load_settings(self.dir).timezone, "Asia/Kolkata")

    def test_non_observe_mode_requires_timezone_and_active_hours(self) -> None:
        for mode in ("assisted", "autonomous"):
            with self.assertRaises(S.SettingsError) as ctx:
                S.validate_for_mode(S.Settings(mode=mode))
            self.assertIn("timezone", str(ctx.exception))
            self.assertIn("active_hours", str(ctx.exception))

            with self.assertRaises(S.SettingsError):
                S.validate_for_mode(S.Settings(mode=mode, timezone="Asia/Kolkata"))

            good = S.Settings(
                mode=mode,
                timezone="Asia/Kolkata",
                active_hours={"daily": ("09:00", "23:00")},
            )
            self.assertEqual(S.validate_for_mode(good).mode, mode)

    def test_observe_mode_needs_no_schedule(self) -> None:
        self.assertEqual(S.validate_for_mode(S.Settings()).mode, "observe")

    def test_validate_rejects_a_bad_timezone_in_a_hand_built_object(self) -> None:
        with self.assertRaises(S.SettingsError):
            S.validate_for_mode(S.Settings(timezone="Nowhere/Special"))


class TestDataDir(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_env_override_must_be_absolute(self) -> None:
        with mock.patch.dict(os.environ, {"SHADOW_DATA_DIR": "relative/place"}):
            with self.assertRaises(S.SettingsError) as ctx:
                S.data_dir()
        self.assertIn("absolute", str(ctx.exception))

    def test_env_override_outside_the_repo_is_accepted(self) -> None:
        with mock.patch.dict(os.environ, {"SHADOW_DATA_DIR": str(Path(self.tmp.name))}):
            self.assertEqual(S.data_dir(), Path(self.tmp.name).resolve())

    def test_data_dir_inside_the_repo_is_refused(self) -> None:
        inside = S.REPO_ROOT / "not-a-real-private-dir"
        with mock.patch.dict(os.environ, {"SHADOW_DATA_DIR": str(inside)}):
            with self.assertRaises(S.SettingsError) as ctx:
                S.data_dir()
        self.assertIn("inside the repository", str(ctx.exception))
        self.assertFalse(inside.exists(), "the check must not create anything")

    def test_repository_data_folder_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"SHADOW_DATA_DIR": str(S.REPO_ROOT / "data")}):
            with self.assertRaises(S.SettingsError):
                S.data_dir()

    def test_default_location_is_outside_the_repo(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHADOW_DATA_DIR", None)
            resolved = S.data_dir()
        self.assertNotEqual(resolved, S.REPO_ROOT)
        self.assertNotIn(S.REPO_ROOT, resolved.parents)


if __name__ == "__main__":
    unittest.main()
