"""Regression: overnight silence is not an outage two minutes after opening."""
import ast
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import unittest

# Load the actual pure status functions without importing credentialled feed adapters.
source = ast.parse(Path(__file__).with_name('build.py').read_text())
functions = [n for n in source.body if isinstance(n, ast.FunctionDef)
             and n.name in {'robot_status', '_last_checkin'}]
namespace = {}
exec(compile(ast.Module(body=functions, type_ignores=[]), 'build.py', 'exec'), namespace)
status = namespace['robot_status']
TZ = ZoneInfo('America/Toronto')


def row(cadence='15-min', stale=45):
    return {'monitoring': 'Live', 'window_start': 8, 'window_end': 23,
            'cadence': cadence, 'stale_after': stale,
            '_self_hb': datetime(2026, 9, 9, 23, 14, tzinfo=TZ)}


class OpeningWindow(unittest.TestCase):
    def test_watchlist_at_0802_is_not_broken(self):
        self.assertNotEqual(status(row(), datetime(2026, 9, 10, 8, 2, tzinfo=TZ))[0], 'crit')

    def test_concierge_before_first_pass_is_not_broken(self):
        r = row('Hourly', 140)
        r['_self_hb'] = datetime(2026, 9, 9, 22, 20, tzinfo=TZ)
        self.assertNotEqual(status(r, datetime(2026, 9, 10, 8, 2, tzinfo=TZ))[0], 'crit')

    def test_still_missing_after_morning_allowance_is_broken(self):
        self.assertEqual(status(row(), datetime(2026, 9, 10, 8, 46, tzinfo=TZ))[0], 'crit')

    def test_new_heartbeat_resets_age(self):
        r = row(); r['_self_hb'] = datetime(2026, 9, 10, 8, 4, tzinfo=TZ)
        self.assertEqual(status(r, datetime(2026, 9, 10, 8, 46, tzinfo=TZ))[0], 'watch')

    def test_off_hours_remain_off_hours(self):
        self.assertEqual(status(row(), datetime(2026, 9, 10, 7, 59, tzinfo=TZ)), ('plain', 'Off-hours'))

    def test_daily_and_unbounded_rows_keep_elapsed_time(self):
        self.assertEqual(status(row('Daily'), datetime(2026, 9, 10, 8, 2, tzinfo=TZ))[0], 'crit')
        r = row(); r['window_start'] = r['window_end'] = None
        self.assertEqual(status(r, datetime(2026, 9, 10, 8, 2, tzinfo=TZ))[0], 'crit')


if __name__ == '__main__':
    unittest.main()
