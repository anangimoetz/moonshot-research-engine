"""Worker regressions use a local temp database and mocked delivery only."""
import copy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from moonshot_scanner.config import Settings
from moonshot_scanner.pipeline import timestamp
from moonshot_scanner.store import Store
from moonshot_scanner.worker import Worker, WorkerLock
from moonshot_scanner.tests.test_review import valid_fixture


class FakeScanner:
    def __init__(self, store):
        self.store = store
        plan, market, features, regime, enrichment, evidence, now = valid_fixture()
        self.asset = {"market": market, "features": features, "regime": regime,
                      "enrichment": enrichment, "evidence": evidence}
        self.row = {"symbol": "TESTUSDT", "exchange": "bitget", "status": "EARLY",
                    "regime": {"label": "RISK_ON"}, "activity_score": 70,
                    "data_coverage_pct": 75, "data_gaps": [], "features": features}
        self.calls = 0

    def execute_scan(self, job, progress=None):
        self.calls += 1
        self.store.add_snapshot(job["id"], copy.deepcopy(self.row))
        return {"returned": 1, "errors": 0, "ready": 0, "early": 1}

    def analyze_asset(self, symbol, exchange):
        return copy.deepcopy(self.asset)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(state_dir=Path(self.temp.name), auto_scan=False,
            session_reports=False, exchanges=("bitget",), bot_token="", chat_id="", webhook_url="")
        self.store = Store(Path(self.temp.name) / "worker.sqlite3")
        self.scanner = FakeScanner(self.store)
        self.worker = Worker(self.settings, self.store, self.scanner)
        self.now = datetime(2026, 11, 20, 12, 16, tzinfo=timezone.utc)
        self.worker.clock = lambda: self.now

    def configure_recipient(self):
        self.settings.bot_token, self.settings.chat_id = "unit-test-not-a-real-token", "123"

    def job(self, kind="scan", payload=None, old=False):
        payload = payload or {"exchanges": ["bitget"], "symbols": ["TESTUSDT"], "scheduled": True}
        jid = self.store.enqueue(kind, payload)
        at = self.now - timedelta(hours=1) if old else self.now
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET created_at=? WHERE id=?", (at.isoformat(), jid))
        return self.store.job(jid)

    def watch(self):
        plan = valid_fixture()[0]
        key = self.store.add_watch("bitget", "TESTUSDT", plan)
        with self.store.connection() as db:
            db.execute("UPDATE watches SET created_at=?,expires_at=? WHERE id=?",
                       ((self.now - timedelta(seconds=30)).isoformat(),
                        (self.now + timedelta(hours=1)).isoformat(), key))
        return key

    def test_auto_scan_notices_are_enabled_by_the_real_scheduled_payload(self):
        self.configure_recipient()
        self.settings.auto_scan = True
        self.worker.schedule()
        job = self.store.claim()
        self.worker.process_job(job)
        self.assertEqual(self.store.outbox_status().get("PENDING"), 1)

    def test_auto_pending_jobs_are_coalesced(self):
        self.settings.auto_scan, self.settings.scan_interval_seconds = True, 60
        for _ in range(30):
            self.worker.schedule()
            self.now += timedelta(minutes=1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE status='PENDING'").fetchone()[0], 1)
            payload = db.execute("SELECT payload FROM jobs WHERE status='PENDING'").fetchone()[0]
        self.assertIn('"scheduled":true', payload)

    def test_auto_keeps_only_one_pending_while_one_is_running(self):
        self.settings.auto_scan, self.settings.scan_interval_seconds = True, 60
        self.worker.schedule()
        self.store.claim()
        for _ in range(10):
            self.now += timedelta(minutes=1)
            self.worker.schedule()
        with self.store.connection() as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
        self.assertEqual(counts.get("RUNNING"), 1)
        self.assertEqual(counts.get("PENDING"), 1)

    def test_discovery_notices_dedup_same_bar_but_not_next_bar(self):
        self.configure_recipient()
        frame = self.scanner.row["features"]["timeframes"]["4h"]
        frame["last_close_time"] -= 14_400_000
        self.worker.process_job(self.job())
        self.worker.process_job(self.job())
        self.assertEqual(self.store.outbox_status().get("PENDING"), 1)
        frame["last_close_time"] += 14_400_000
        self.worker.process_job(self.job())
        self.assertEqual(self.store.outbox_status().get("PENDING"), 2)

    def test_outbox_is_persisted_before_job_done(self):
        self.configure_recipient()
        original = self.store.finish
        def finish(*args, **kwargs):
            self.assertGreater(self.store.outbox_status().get("PENDING", 0), 0)
            return original(*args, **kwargs)
        with patch.object(self.store, "finish", side_effect=finish):
            self.worker.process_job(self.job())

    def test_stale_live_job_is_skipped_without_scanning(self):
        job = self.job(old=True)
        self.worker.process_job(job)
        self.assertEqual(self.scanner.calls, 0)
        self.assertEqual(self.store.job(job["id"])["result"]["status"], "EXPIRED")

    def test_session_cursor_recovers_recent_due_anchor_once(self):
        self.settings.session_reports = True
        self.now = datetime(2026, 11, 20, 7, 50, tzinfo=timezone.utc)
        self.store.set_state("session_scheduler_cursor", "2026-11-20T07:44:00+00:00")
        self.worker.schedule()
        self.worker.schedule()
        with self.store.connection() as db:
            rows = db.execute("SELECT payload FROM jobs WHERE kind='session'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("london_preopen", rows[0][0])

    def test_too_old_session_is_durably_missed_not_backfilled_live(self):
        self.settings.session_reports = True
        self.now = datetime(2026, 11, 20, 8, 20, tzinfo=timezone.utc)
        self.store.set_state("session_scheduler_cursor", "2026-11-20T07:44:00+00:00")
        self.worker.schedule()
        self.assertEqual(self.store.get_state("session_scheduler")["missed_count"], 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.store.get_state("session_scheduler")["delivery_status"], "UNCONFIGURED")

    def test_long_outage_has_visible_recovery_gap(self):
        self.settings.session_reports = True
        self.store.set_state("session_scheduler_cursor", "2026-10-01T00:00:00+00:00")
        self.worker.schedule()
        self.assertEqual(self.store.get_state("session_recovery_gap")["status"], "MISSED_RANGE")

    def test_missing_channel_never_claims_delivery(self):
        self.assertEqual(self.worker.queue_notice("unit", "status", {"status": "TEST"}), 0)
        self.assertEqual(self.store.outbox_status(), {})
        self.assertEqual(self.store.get_state("last_notice")["status"], "UNCONFIGURED")
        self.worker.heartbeat()
        self.assertEqual(self.store.get_state("worker")["delivery_status"], "UNCONFIGURED")

    def test_stale_ticker_cannot_invalidate_watch(self):
        key = self.watch()
        self.scanner.asset["market"].update(price=97, ticker_observed_at_ms=int(self.now.timestamp()*1000)-300000)
        self.worker.check_watches()
        row = self.store.watches()[0]
        self.assertEqual(row["id"], key)
        self.assertEqual(row["status"], "WAITING")
        self.assertIn("stale", row["result"]["waiting_for"][0])

    def test_pre_watch_ticker_cannot_invalidate_even_if_recent(self):
        self.watch()
        self.scanner.asset["market"].update(price=97, ticker_observed_at_ms=int(self.now.timestamp()*1000)-40000)
        self.worker.check_watches()
        self.assertEqual(self.store.watches()[0]["status"], "WAITING")

    def test_future_ticker_cannot_invalidate_watch(self):
        self.watch()
        self.scanner.asset["market"].update(price=97, ticker_observed_at_ms=int(self.now.timestamp()*1000)+1000)
        self.worker.check_watches()
        self.assertEqual(self.store.watches()[0]["status"], "WAITING")

    def test_fresh_stop_cross_invalidates_and_queues_once(self):
        self.configure_recipient()
        self.watch()
        self.scanner.asset["market"]["price"] = 97
        self.worker.check_watches()
        self.worker.check_watches()
        self.assertEqual(self.store.watches()[0]["status"], "INVALIDATED")
        self.assertEqual(self.store.outbox_status().get("PENDING"), 1)

    def test_incomplete_plan_stays_waiting(self):
        self.watch()
        with self.store.connection() as db:
            db.execute("UPDATE watches SET plan=?", ('{"side":"LONG","stop":null}',))
        self.worker.check_watches()
        self.assertEqual(self.store.watches()[0]["status"], "WAITING")

    def test_plan_expiry_ends_watch_before_longer_watch_lifetime(self):
        self.watch()
        self.now += timedelta(hours=9)
        self.worker.check_watches()
        self.assertEqual(self.store.watches()[0]["status"], "EXPIRED")

    def test_delivery_success_is_explicit_and_mocked(self):
        self.configure_recipient()
        self.worker.queue_notice("unit", "status", {"status": "TEST"})
        response = SimpleNamespace(status_code=200, json=lambda: {"ok": True})
        with patch("moonshot_scanner.worker.requests.post", return_value=response) as post:
            self.assertTrue(self.worker.deliver_one())
            post.assert_called_once()
        self.assertEqual(self.store.outbox_status().get("DELIVERED"), 1)
        self.assertEqual(self.store.get_state("delivery")["status"], "LAST_DELIVERY_OK")

    def test_failed_delivery_stays_pending_without_secret_in_status(self):
        self.configure_recipient()
        self.worker.queue_notice("unit", "status", {"status": "TEST"})
        with patch("moonshot_scanner.worker.requests.post", side_effect=requests.RequestException("SECRET_TOKEN_IN_URL")):
            self.worker.deliver_one()
        self.assertEqual(self.store.outbox_status().get("PENDING"), 1)
        self.assertNotIn("SECRET_TOKEN_IN_URL", str(self.store.get_state("delivery")))

    def test_component_crash_is_failed_not_running(self):
        self.worker._active = 1
        self.worker._component_state = {"scan": {"status": "STARTING"}}
        def crash():
            raise ValueError("SECRET_SHOULD_NOT_BE_LOGGED")
        self.worker._guard("scan", crash)
        self.assertEqual(self.store.get_state("worker")["status"], "FAILED")
        self.assertNotIn("SECRET_SHOULD_NOT_BE_LOGGED", str(self.store.get_state("worker")))

    def test_long_scan_cannot_starve_other_loops(self):
        entered, release, scheduled, watched, delivered = [threading.Event() for _ in range(5)]
        self.worker.clock = lambda: datetime.now(timezone.utc)
        self.store.enqueue("scan", {"symbols": ["TESTUSDT"]})
        def scan(job, progress=None):
            entered.set()
            release.wait(3)
            return {"returned": 0, "errors": 0, "ready": 0, "early": 0}
        self.scanner.execute_scan = scan
        self.worker.schedule = lambda: scheduled.set()
        self.worker.check_watches = lambda: watched.set()
        self.worker.deliver_one = lambda: delivered.set() or False
        try:
            self.assertTrue(self.worker.start())
            self.assertTrue(entered.wait(1))
            self.assertTrue(scheduled.wait(1))
            self.assertTrue(watched.wait(1))
            self.assertTrue(delivered.wait(1))
        finally:
            release.set()
            self.worker.close()

    def test_exclusive_worker_lock(self):
        first = WorkerLock(Path(self.temp.name) / "exclusive.lock")
        second = WorkerLock(Path(self.temp.name) / "exclusive.lock")
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
        finally:
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()
