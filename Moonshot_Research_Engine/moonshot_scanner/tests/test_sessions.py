"""Regression cases for report clocks and mismatched UK/US DST weeks."""
import unittest
from datetime import datetime, timezone

from moonshot_scanner.sessions import next_session_runs, session_context


class SessionTests(unittest.TestCase):
    def runs(self, at, key, count=3):
        return next_session_runs(at, count)["schedules"][key]["runs"]

    def test_november_london_conversion(self):
        run = self.runs("2026-11-20T03:00:00Z", "london_preopen")[0]
        self.assertEqual(run["utc"], "2026-11-20T07:45:00+00:00")
        self.assertEqual(run["armenia"], "2026-11-20T11:45:00+04:00")
        self.assertEqual(run["wib"], "2026-11-20T14:45:00+07:00")

    def test_us_dst_changes_before_uk(self):
        at = "2027-03-15T00:00:00Z"
        london = self.runs(at, "london_preopen")[0]
        ny = self.runs(at, "ny_premarket")[0]
        self.assertEqual(london["armenia"][11:16], "11:45")
        self.assertEqual(ny["armenia"][11:16], "16:15")
        self.assertEqual(ny["timezone_abbreviation"], "EDT")
        self.assertEqual(london["timezone_abbreviation"], "GMT")

    def test_both_dst_after_march_28(self):
        at = "2027-03-29T00:00:00Z"
        london = self.runs(at, "london_preopen")[0]
        ny = self.runs(at, "ny_premarket")[0]
        self.assertEqual(london["armenia"][11:16], "10:45")
        self.assertEqual(ny["armenia"][11:16], "16:15")

    def test_weekday_schedule_skips_weekend(self):
        runs = self.runs("2026-11-20T07:46:00Z", "london_preopen")
        self.assertEqual([r["utc"][:10] for r in runs],
                         ["2026-11-23", "2026-11-24", "2026-11-25"])

    def test_daily_boundary_includes_weekend(self):
        runs = self.runs("2026-11-20T03:00:00Z", "daily_utc_close")
        self.assertEqual([r["utc"][:10] for r in runs],
                         ["2026-11-21", "2026-11-22", "2026-11-23"])
        self.assertTrue(all(r["armenia"][11:16] == "04:00" for r in runs))
        self.assertTrue(all(r["wib"][11:16] == "07:00" for r in runs))

    def test_next_run_is_strictly_after_now(self):
        run = self.runs("2026-11-20T07:45:00Z", "london_preopen")[0]
        self.assertEqual(run["utc"][:10], "2026-11-23")

    def test_cash_anchor_is_not_analytical_session_open(self):
        ctx = session_context("2026-11-20T13:15:00Z")
        ny = next(x for x in ctx["windows"] if x["name"] == "New York")
        cash = next(x for x in ctx["anchors_today"] if x["name"] == "NYSE regular cash open")
        self.assertEqual(ny["start"]["armenia"][11:16], "17:00")
        self.assertEqual(cash["armenia"][11:16], "18:30")
        self.assertFalse(cash["event_verified"])
        self.assertIsNone(ctx["session_score"])

    def test_weekend_does_not_claim_equity_open(self):
        ctx = session_context("2026-11-21T15:00:00Z")
        self.assertNotIn("London", ctx["active_windows"])
        self.assertNotIn("New York", ctx["active_windows"])
        self.assertTrue(all(not a["weekday"] for a in ctx["anchors_today"]))
        self.assertTrue(all(not a["event_verified"] for a in ctx["anchors_today"]))

    def test_epoch_and_aware_datetime_inputs(self):
        dt = datetime(2026, 11, 20, 3, tzinfo=timezone.utc)
        self.assertEqual(session_context(dt)["observed_at"],
                         session_context(dt.timestamp() * 1000)["observed_at"])

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            session_context(datetime(2026, 11, 20))

    def test_bounded_count(self):
        for bad in (0, 31, True, 2.5):
            with self.assertRaises(ValueError):
                next_session_runs("2026-11-20T00:00:00Z", bad)


if __name__ == "__main__":
    unittest.main()
