"""Durability and point-in-time evidence regressions; no network or recipients."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from moonshot_scanner.store import Store
from moonshot_scanner.worker import WorkerLock


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="moonshot-store-test-")
        self.directory = Path(self.tmp.name)
        self.path = self.directory / "research.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def outbox_row(self, item_id):
        with self.store.connection() as db:
            return dict(db.execute("SELECT * FROM outbox WHERE id=?", (item_id,)).fetchone())

    def evidence_row(self, **overrides):
        now = datetime.now(timezone.utc)
        row = {"symbol": "EVAAUSDT", "kind": "unlock", "verified": True,
               "observed_at": (now - timedelta(hours=1)).isoformat(),
               "expires_at": (now + timedelta(hours=12)).isoformat(),
               "source_url": "https://example.org/project/vesting",
               "facts": {"circulating_pct_unlock_30d": 0.4,
                         "lookahead_days": 30, "recipients": ["team"]}}
        row.update(overrides)
        return row


class DurableQueueTests(StoreCase):
    def test_enqueue_idempotence_survives_reopening_and_preserves_original_payload(self):
        first = self.store.enqueue("scan", {"symbols": ["EVAAUSDT"]}, "same-event")
        reopened = Store(self.path)
        duplicate = reopened.enqueue("scan", {"symbols": ["SOLUSDT"]}, "same-event")
        self.assertEqual(first, duplicate)
        self.assertEqual(reopened.job(first)["payload"]["symbols"], ["EVAAUSDT"])
        self.assertEqual(reopened.claim()["id"], first)
        self.assertIsNone(reopened.claim())

    def test_recovery_requires_worker_exclusivity_and_reclaims_unfinished_work(self):
        lock1, lock2 = WorkerLock(self.directory / "worker.lock"), WorkerLock(self.directory / "worker.lock")
        try:
            self.assertTrue(lock1.acquire())
            self.assertFalse(lock2.acquire())
            job_id = self.store.enqueue("scan", {"symbols": ["EVAAUSDT"]}, "restart-event")
            self.store.claim(lease_seconds=3600)
            self.assertIsNone(Store(self.path).claim())
            # Recovery is deliberately called only by the exclusive lock owner.
            reopened = Store(self.path)
            reopened.recover_worker()
            self.assertEqual(reopened.claim()["id"], job_id)
            self.assertEqual(reopened.job(job_id)["status"], "RUNNING")
        finally:
            lock1.close()
            lock2.close()
        self.assertTrue(lock2.acquire())
        lock2.close()

    def test_expired_lease_can_be_reclaimed_without_duplicating_job(self):
        job_id = self.store.enqueue("scan", {}, "expired-lease")
        with patch("moonshot_scanner.store.time.time", return_value=1000):
            self.assertEqual(self.store.claim(lease_seconds=10)["id"], job_id)
            self.assertIsNone(self.store.claim())
        with patch("moonshot_scanner.store.time.time", return_value=1011):
            self.assertEqual(Store(self.path).claim()["id"], job_id)

    def test_two_concurrent_claims_receive_distinct_jobs(self):
        expected = {self.store.enqueue("scan", {"n": n}, "job:" + str(n)) for n in range(2)}
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            return self.store.claim()["id"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            claimed = [future.result(timeout=10) for future in futures]
        self.assertEqual(set(claimed), expected)
        self.assertEqual(len(set(claimed)), 2)
        self.assertIsNone(self.store.claim())

    def test_completed_job_not_recovered_after_restart(self):
        job_id = self.store.enqueue("scan", {}, "done")
        self.store.claim()
        self.store.finish(job_id, {"returned": 1})
        reopened = Store(self.path)
        reopened.recover_worker()
        self.assertIsNone(reopened.claim())
        self.assertEqual(reopened.job(job_id)["result"], {"returned": 1})

    def test_snapshot_replay_replaces_asset_without_duplicate_results(self):
        job_id = self.store.enqueue("scan", {}, "replace")
        self.store.add_snapshot(job_id, {"exchange": "bitget", "symbol": "EVAAUSDT", "v": 1})
        Store(self.path).add_snapshot(job_id, {"exchange": "bitget", "symbol": "EVAAUSDT", "v": 2})
        rows = self.store.job(job_id)["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["v"], 2)


class DeliveryOutboxTests(StoreCase):
    def test_failure_is_durable_retryable_and_increments_attempt_count(self):
        self.store.queue_delivery("notice:1", {"channel": "test", "data": {"n": 1}})
        with patch("moonshot_scanner.store.time.time", return_value=1000):
            item = self.store.claim_delivery()
            self.assertEqual(self.outbox_row(item["id"])["attempts"], 1)
            self.store.finish_delivery(item["id"], error="TemporaryFailure")
            row = self.outbox_row(item["id"])
            self.assertEqual(row["status"], "PENDING")
            self.assertGreater(row["next_attempt"], 1000)
            self.assertIsNone(self.store.claim_delivery())
        reopened = Store(self.path)
        with patch("moonshot_scanner.store.time.time", return_value=row["next_attempt"] + 1):
            retry = reopened.claim_delivery()
            self.assertEqual(retry["id"], item["id"])
            self.assertEqual(self.outbox_row(item["id"])["attempts"], 2)
            reopened.finish_delivery(retry["id"])
            self.assertIsNone(reopened.claim_delivery())
        self.assertEqual(reopened.outbox_status(), {"DELIVERED": 1})

    def test_delivery_dedup_keeps_original_payload(self):
        self.store.queue_delivery("same-notice", {"data": "original"})
        self.store.queue_delivery("same-notice", {"data": "replacement"})
        item = self.store.claim_delivery()
        self.assertEqual(item["payload"]["data"], "original")
        self.assertIsNone(self.store.claim_delivery())

    def test_recovery_requeues_interrupted_delivery(self):
        self.store.queue_delivery("crashed-send", {"data": "research"})
        item = self.store.claim_delivery(lease_seconds=3600)
        reopened = Store(self.path)
        self.assertIsNone(reopened.claim_delivery())
        reopened.recover_worker()
        replay = reopened.claim_delivery()
        self.assertEqual(replay["id"], item["id"])
        self.assertEqual(self.outbox_row(item["id"])["attempts"], 2)

    def test_two_concurrent_senders_claim_distinct_notices(self):
        for n in range(2):
            self.store.queue_delivery("out:" + str(n), {"n": n})
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            return self.store.claim_delivery()["id"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            ids = [f.result(timeout=10) for f in futures]
        self.assertEqual(len(set(ids)), 2)


class OpenInterestWindowTests(StoreCase):
    def test_first_oi_observation_does_not_invent_change(self):
        observed = 1_790_000_000_000
        self.store.add_oi("bitget", "EVAAUSDT", observed, 220)
        output = self.store.oi_windows("bitget", "EVAAUSDT", observed, 220)
        self.assertEqual(output["unit"], "BASE_COIN")
        self.assertIsNone(output["4h"]["change_pct"])
        self.assertIsNone(output["24h"]["change_pct"])

    def test_four_hour_tolerance_uses_time_window_not_last_run(self):
        observed = 1_790_000_000_000
        baseline = observed - 4 * 3_600_000 + 9 * 60_000
        self.store.add_oi("bitget", "EVAAUSDT", baseline, 200)
        self.store.add_oi("bitget", "EVAAUSDT", observed - 60_000, 9000)
        output = Store(self.path).oi_windows("bitget", "EVAAUSDT", observed, 220)
        self.assertAlmostEqual(output["4h"]["change_pct"], 10)
        self.assertEqual(output["4h"]["baseline_at_ms"], baseline)
        self.assertIsNone(output["24h"]["change_pct"])

    def test_ten_minute_boundary_inclusive_and_eleven_minutes_excluded(self):
        observed = 1_790_000_000_000
        for offset in (-600_000, 600_000):
            with self.subTest(offset=offset):
                symbol = "PLUSUSDT" if offset > 0 else "MINUSUSDT"
                self.store.add_oi("bitget", symbol, observed - 14_400_000 + offset, 100)
                output = self.store.oi_windows("bitget", symbol, observed, 110)
                self.assertAlmostEqual(output["4h"]["change_pct"], 10)
        self.store.add_oi("bitget", "OUTSIDEUSDT", observed - 14_400_000 + 660_000, 100)
        output = self.store.oi_windows("bitget", "OUTSIDEUSDT", observed, 110)
        self.assertIsNone(output["4h"]["change_pct"])

    def test_nearest_in_window_sample_wins_over_more_distant_sample(self):
        observed = 1_790_000_000_000
        target = observed - 14_400_000
        self.store.add_oi("bitget", "EVAAUSDT", target - 500_000, 100)
        self.store.add_oi("bitget", "EVAAUSDT", target + 60_000, 200)
        output = self.store.oi_windows("bitget", "EVAAUSDT", observed, 220)
        self.assertEqual(output["4h"]["baseline_at_ms"], target + 60_000)
        self.assertAlmostEqual(output["4h"]["change_pct"], 10)

    def test_usd_notional_or_other_venue_cannot_contaminate_token_oi(self):
        observed = 1_790_000_000_000
        target = observed - 14_400_000
        self.store.add_oi("bitget", "EVAAUSDT", target, 1000, unit="USD_NOTIONAL")
        self.store.add_oi("bybit", "EVAAUSDT", target, 100)
        self.store.add_oi("bitget", "SOLUSDT", target, 100)
        output = self.store.oi_windows("bitget", "EVAAUSDT", observed, 220)
        self.assertIsNone(output["4h"]["change_pct"])


class EvidenceGuardTests(StoreCase):
    def test_url_credentials_and_non_https_sources_rejected(self):
        for url in ("https://operator:synthetic-password@example.org/doc", "http://example.org/doc",
                    "ftp://example.org/doc", "javascript:alert(1)", "https:///missing-host"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.store.add_evidence(self.evidence_row(source_url=url))

    def test_asset_specific_evidence_cannot_use_wildcard_scope(self):
        for kind in ("unlock", "fundamental", "catalyst", "whale_transfer", "security"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.store.add_evidence(self.evidence_row(symbol="*", kind=kind))

    def test_global_macro_scope_does_not_bypass_asset_unlock_review(self):
        self.store.add_evidence(self.evidence_row(symbol="*", kind="macro", facts={"event": "CPI"}))
        self.store.add_evidence(self.evidence_row())
        evaa = self.store.evidence("EVAAUSDT")
        sol = self.store.evidence("SOLUSDT")
        self.assertEqual({e["kind"] for e in evaa}, {"unlock", "macro"})
        self.assertEqual([e["kind"] for e in sol], ["macro"])

    def test_expired_and_future_evidence_not_fresh(self):
        now = datetime.now(timezone.utc)
        self.store.add_evidence(self.evidence_row(observed_at=(now - timedelta(days=3)).isoformat(),
                                                 expires_at=(now - timedelta(days=1)).isoformat()))
        self.store.add_evidence(self.evidence_row(observed_at=(now + timedelta(hours=1)).isoformat(),
                                                 expires_at=(now + timedelta(hours=2)).isoformat()))
        rows = self.store.evidence("EVAAUSDT", now)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(e["fresh"] is False for e in rows))

    def test_operator_verification_requires_boolean_and_facts_object(self):
        for overrides in ({"verified": "true"}, {"verified": 1}, {"facts": ["assertion"]}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.store.add_evidence(self.evidence_row(**overrides))


class ImmutableWatchTests(StoreCase):
    def test_watch_keeps_original_plan_across_mutation_status_update_and_restart(self):
        plan = {"side": "LONG", "entry_low": 99, "entry_high": 100, "stop": 95,
                "targets": [115, 130], "status": "CONDITIONAL"}
        original = copy.deepcopy(plan)
        key = self.store.add_watch("bitget", "EVAAUSDT", plan)
        plan["stop"] = 85
        plan["targets"][0] = 999
        fetched = self.store.watches()[0]
        fetched["plan"]["stop"] = 70
        self.store.update_watch(key, "WAITING", {"ready": False, "new_candidate_stop": 60})
        reopened = Store(self.path)
        row = reopened.watches()[0]
        self.assertEqual(row["plan"], original)
        self.assertEqual(row["result"]["new_candidate_stop"], 60)

    def test_readding_same_watch_does_not_extend_or_rewrite_it(self):
        plan = {"side": "LONG", "entry_low": 99, "entry_high": 100, "stop": 95,
                "targets": [115, 130], "status": "CONDITIONAL"}
        key = self.store.add_watch("bitget", "EVAAUSDT", plan, lifetime_hours=2)
        before = self.store.watches()[0]
        self.assertEqual(self.store.add_watch("bitget", "EVAAUSDT", plan, lifetime_hours=48), key)
        after = self.store.watches()[0]
        self.assertEqual(before["created_at"], after["created_at"])
        self.assertEqual(before["expires_at"], after["expires_at"])


if __name__ == "__main__":
    unittest.main()
