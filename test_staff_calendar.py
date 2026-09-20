"""Staff calendar via the Calendar API must produce the rows the ICS export
did — same fields, same skips, same window rule — and the shadow report must
never carry event text. Network-free."""
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import build

TZ = ZoneInfo("America/Toronto")
WS = datetime(2026, 9, 20, 5, tzinfo=TZ)
WE = WS + timedelta(days=1) - timedelta(minutes=1)


def item(summary, start, end, **extra):
    d = {"summary": summary, "status": "confirmed",
         "start": {"dateTime": start}, "end": {"dateTime": end}}
    d.update(extra)
    return d


class RowMapping(unittest.TestCase):
    def test_fields_match_the_export_shape(self):
        rows = build.api_rows([item("Ela FBS", "2026-09-20T12:45:00-04:00", "2026-09-20T13:15:00-04:00",
                                    description="Studio 901", recurringEventId="abc")], WS, WE)
        self.assertEqual(rows, [{
            "summary": "Ela FBS", "description": "Studio 901", "cancelled": False,
            "recurring": False,            # the export strips RRULE on copies; mirrored
            "dtstart": datetime(2026, 9, 20, 12, 45, tzinfo=TZ),
            "dtend": datetime(2026, 9, 20, 13, 15, tzinfo=TZ)}])
        self.assertEqual(set(rows[0]), {"summary", "description", "cancelled", "recurring", "dtstart", "dtend"})

    def test_utc_and_offset_forms_land_in_toronto(self):
        rows = build.api_rows([item("x", "2026-09-20T16:45:00Z", "2026-09-20T17:15:00Z")], WS, WE)
        self.assertEqual(rows[0]["dtstart"].isoformat(), "2026-09-20T12:45:00-04:00")
        self.assertEqual(rows[0]["dtstart"].tzinfo, TZ)

    def test_all_day_events_are_skipped_like_the_export(self):
        rows = build.api_rows([{"summary": "Holiday", "start": {"date": "2026-09-20"}, "end": {"date": "2026-09-21"}}], WS, WE)
        self.assertEqual(rows, [])

    def test_missing_summary_and_description_are_empty_strings(self):
        rows = build.api_rows([{"start": {"dateTime": "2026-09-20T12:00:00-04:00"},
                                "end": {"dateTime": "2026-09-20T12:30:00-04:00"}}], WS, WE)
        self.assertEqual((rows[0]["summary"], rows[0]["description"]), ("", ""))

    def test_cancelled_status_maps(self):
        rows = build.api_rows([item("x", "2026-09-20T12:00:00-04:00", "2026-09-20T12:30:00-04:00", status="cancelled")], WS, WE)
        self.assertTrue(rows[0]["cancelled"])

    def test_html_description_reads_as_the_exports_plain_text(self):
        rows = build.api_rows([item("x", "2026-09-20T12:00:00-04:00", "2026-09-20T12:30:00-04:00",
                                    description="<b>Studio 527</b><br>Claimed &amp; noted")], WS, WE)
        self.assertEqual(rows[0]["description"], "Studio 527\nClaimed & noted")

    def test_window_rule_is_the_exports(self):
        before = item("ends at window start", "2026-09-20T04:00:00-04:00", "2026-09-20T05:00:00-04:00")
        straddle = item("straddles start", "2026-09-20T04:30:00-04:00", "2026-09-20T05:30:00-04:00")
        at_end = item("starts at window end", "2026-09-21T04:59:00-04:00", "2026-09-21T05:30:00-04:00")
        zero = item("zero-length at start", "2026-09-20T05:00:00-04:00", "2026-09-20T05:00:00-04:00")
        rows = build.api_rows([before, straddle, at_end, zero], WS, WE)
        self.assertEqual([r["summary"] for r in rows], ["straddles start", "zero-length at start"])

    def test_order_is_deterministic_by_time_then_text(self):
        a = item("Need Monitoring", "2026-09-20T22:45:00-04:00", "2026-09-20T23:15:00-04:00")
        b = item("Need FBS", "2026-09-20T22:45:00-04:00", "2026-09-20T23:15:00-04:00")
        self.assertEqual([r["summary"] for r in build.api_rows([a, b], WS, WE)],
                         [r["summary"] for r in build.api_rows([b, a], WS, WE)])


def response(status=200, payload=None, text=""):
    return Mock(status_code=status, text=text, json=Mock(return_value=payload or {}))


class Fetch(unittest.TestCase):
    def test_pages_are_followed_and_rows_windowed(self):
        pages = [response(payload={"items": [item("p1", "2026-09-20T10:00:00-04:00", "2026-09-20T11:00:00-04:00")],
                                   "nextPageToken": "t2"}),
                 response(payload={"items": [item("p2", "2026-09-20T12:00:00-04:00", "2026-09-20T13:00:00-04:00")]})]
        with patch.dict(os.environ, {"STAFF_CALENDAR_ID": "cal@group.calendar.google.com"}), \
             patch.object(build.requests, "get", side_effect=pages) as get:
            rows = build.fetch_staff_calendar_api(WS, WE, token="tok")
        self.assertEqual([r["summary"] for r in rows], ["p1", "p2"])
        self.assertEqual(get.call_count, 2)
        first = get.call_args_list[0]
        self.assertIn("/cal@group.calendar.google.com/events", first.args[0])
        self.assertEqual(first.kwargs["headers"], {"Authorization": "Bearer tok"})
        p = first.kwargs["params"]
        self.assertEqual((p["singleEvents"], p["orderBy"], p["timeMax"]), ("true", "startTime", "2026-09-21T08:59:00Z"))
        self.assertEqual(p["timeMin"], "2026-09-20T08:59:00Z")     # a minute early, filtered after
        self.assertEqual(get.call_args_list[1].kwargs["params"]["pageToken"], "t2")

    def test_http_failure_raises_never_returns_partial(self):
        with patch.dict(os.environ, {"STAFF_CALENDAR_ID": "c"}), \
             patch.object(build.requests, "get", return_value=response(403, text="forbidden")):
            with self.assertRaisesRegex(RuntimeError, "403"):
                build.fetch_staff_calendar_api(WS, WE, token="tok")

    def test_token_exchange_posts_a_jwt_bearer_grant(self):
        info = {"client_email": "sa@p.iam.gserviceaccount.com", "private_key": "k"}
        fake_jwt = Mock(encode=Mock(return_value=b"signed"))
        fake_crypt = Mock(RSASigner=Mock(from_service_account_info=Mock(return_value="signer")))
        with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(info)}), \
             patch.dict("sys.modules", {"google": Mock(auth=Mock(crypt=fake_crypt, jwt=fake_jwt)),
                                        "google.auth": Mock(crypt=fake_crypt, jwt=fake_jwt)}), \
             patch.object(build.requests, "post", return_value=response(payload={"access_token": "AT"})) as post:
            self.assertEqual(build.service_account_token(build.CALENDAR_SCOPE), "AT")
        claims = fake_jwt.encode.call_args.args[1]
        self.assertEqual((claims["iss"], claims["scope"], claims["aud"]),
                         (info["client_email"], build.CALENDAR_SCOPE, build.TOKEN_URL))
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "urn:ietf:params:oauth:grant-type:jwt-bearer")
        self.assertEqual(post.call_args.kwargs["data"]["assertion"], "signed")


class Mode(unittest.TestCase):
    def test_unconfigured_is_ics_whatever_is_asked(self):
        with patch.dict(os.environ, {"STAFF_SOURCE": "api"}, clear=False):
            os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
            os.environ.pop("STAFF_CALENDAR_ID", None)
            self.assertEqual(build.staff_source(), "ics")

    def test_configured_defaults_to_shadow_and_honours_the_switch(self):
        env = {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}", "STAFF_CALENDAR_ID": "c"}
        with patch.dict(os.environ, env):
            os.environ.pop("STAFF_SOURCE", None)
            self.assertEqual(build.staff_source(), "shadow")
        with patch.dict(os.environ, dict(env, STAFF_SOURCE="api")):
            self.assertEqual(build.staff_source(), "api")
        with patch.dict(os.environ, dict(env, STAFF_SOURCE="ics")):
            self.assertEqual(build.staff_source(), "ics")


def row(summary, h, m, mins=30, desc="Studio 901", cancelled=False):
    s = WS.replace(hour=h, minute=m)
    return {"summary": summary, "description": desc, "cancelled": cancelled, "recurring": False,
            "dtstart": s, "dtend": s + timedelta(minutes=mins)}


class Shadow(unittest.TestCase):
    def test_report_counts_and_names_fields_but_never_text(self):
        ics = [row("Ela FBS", 12, 45), row("Need Monitoring", 14, 0), row("Gone", 16, 0, cancelled=True),
               row("Only here", 18, 0)]
        api = [row("Ela FBS", 12, 45), row("Need Monitoring", 14, 0, desc="Studio 527"),
               row("Only there", 20, 0)]
        rep = build.compare_staff_rows(ics, api)
        self.assertEqual((rep["ics"], rep["api"], rep["match"]), (3, 3, 1))
        self.assertEqual(rep["notes"], ["differs Sun 14:00-14:30 in description",
                                        "only-ics Sun 18:00-18:30", "only-api Sun 20:00-20:30"])
        blob = json.dumps(rep)
        for word in ("Ela", "Need", "Only", "Studio", "Gone"):
            self.assertNotIn(word, blob)

    def test_identical_rows_report_clean(self):
        rows = [row("Ela FBS", 12, 45), row("Need FBS", 14, 0)]
        rep = build.compare_staff_rows(rows, list(reversed(rows)))
        self.assertEqual((rep["match"], rep["notes"]), (2, []))


if __name__ == "__main__":
    unittest.main()
