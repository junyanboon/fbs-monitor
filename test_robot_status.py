"""Regression guards for Run Monitor off-hours classification.

Off-hours is a quiet-period label, not forgiveness for a pass that was already
missing when its active window closed.  On 2026-09-04 that ordering hid The
Loop, The Responder and The Custodian as grey ``Off-hours`` lanes.
"""
from datetime import datetime
import unittest

import build


def lane(name, last, *, start=8, end=23, stale=120):
    return {
        "run": name,
        "monitoring": "Live",
        "window_start": start,
        "window_end": end,
        "stale_after": stale,
        "cadence": "Hourly",
        "_self_hb": last,
        "_last_seen": None,
        "_last_completed": None,
        "_last_run": None,
    }


class RobotStatusTests(unittest.TestCase):
    def test_a_lane_that_missed_its_active_window_stays_missing_off_hours(self):
        now = datetime(2026, 9, 4, 23, 30, tzinfo=build.TZ)
        for name in ("The Loop", "The Responder", "The Custodian"):
            with self.subTest(name=name):
                row = lane(name, now.replace(hour=10, minute=0), stale=90)
                self.assertEqual(build.robot_status(row, now),
                                 ("crit", "🔴 MISSING"))

    def test_a_lane_that_finished_near_window_close_is_off_hours(self):
        now = datetime(2026, 9, 4, 23, 30, tzinfo=build.TZ)
        row = lane("Healthy hourly lane", now.replace(hour=22, minute=35), stale=90)
        self.assertEqual(build.robot_status(row, now), ("plain", "Off-hours"))

    def test_the_normal_overnight_gap_does_not_turn_a_healthy_lane_missing(self):
        now = datetime(2026, 9, 5, 6, 30, tzinfo=build.TZ)
        row = lane(
            "Healthy hourly lane",
            datetime(2026, 9, 4, 22, 35, tzinfo=build.TZ),
            stale=90,
        )
        self.assertEqual(build.robot_status(row, now), ("plain", "Off-hours"))

    def test_a_lane_that_missed_yesterdays_window_is_missing_before_open(self):
        now = datetime(2026, 9, 5, 6, 30, tzinfo=build.TZ)
        row = lane(
            "Stale hourly lane",
            datetime(2026, 9, 4, 10, 0, tzinfo=build.TZ),
            stale=90,
        )
        self.assertEqual(build.robot_status(row, now),
                         ("crit", "🔴 MISSING"))


if __name__ == "__main__":
    unittest.main()
