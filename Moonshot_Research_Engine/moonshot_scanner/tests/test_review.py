"""Independent fail-closed regressions from the scanner reliability review."""
import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moonshot_scanner.pipeline import Scanner, number, readiness, timestamp
from moonshot_scanner.store import Store


def valid_fixture():
    """Completed candles use the actual analyze_features output field names."""
    now = int(datetime(2026, 11, 20, 12, 16, tzinfo=timezone.utc).timestamp() * 1000)
    close4h = now - 16 * 60_000
    close15m = now - 60_000
    at = datetime.fromtimestamp(now / 1000, timezone.utc).isoformat()
    frames = {}
    for name, ended in (("4h", close4h), ("1h", close4h), ("15m", close15m)):
        frames[name] = {
            "last_close": 99.5, "ema13": 99, "ema21": 98,
            "last_close_time": ended, "last_open_time": ended - {"4h": 14_400_000, "1h": 3_600_000, "15m": 900_000}[name],
            "rsi14": 50, "count_closed": 250, "history_status": "COMPLETE", "data_gaps": [],
        }
    plan = {
        "side": "LONG", "setup_type": "PULLBACK", "status": "CONDITIONAL",
        "entry_low": 99, "entry_high": 100, "entry_mid": 99.5, "stop": 98,
        "targets": [107, 110], "fees_bps": 12, "slippage_bps": 10,
        "position_notional_usd": 1000, "position_units": 10, "risk_budget_usd": 22.2,
        "risk_pct": 1, "created_from_close_time": close4h, "expires_at_ms": close4h + 28_800_000,
        "structural_invalidation": 98.1, "invalidation_reason": "closed confirmed 4h support",
        "target_reason": "historical confirmed 4h resistance", "rr_net": 3,
        "reasons": ["CONDITIONAL_PLAN_IS_NOT_ENTRY_CONFIRMATION"],
    }
    market = {
        "symbol": "TESTUSDT", "price": 99.5, "change_24h_pct": 1,
        "ticker_observed_at_ms": now, "spot_available": True, "product": "SPOT",
        "derivatives": {
            "available": True, "product": "USDT-FUTURES",
            "funding_rate": .00001, "funding_interval_hours": 8,
            "funding_observed_at_ms": now, "next_funding_time": now + 3_600_000,
            "oi_snapshot": {"base_units": 100, "unit": "BASE_COIN", "observed_at_ms": now},
            "oi_windows": {
                "unit": "BASE_COIN", "base_units": 100, "observed_at_ms": now,
                "4h": {"status": "AVAILABLE", "change_pct": 1, "baseline_at_ms": now - 14_400_000},
                "24h": {"status": "AVAILABLE", "change_pct": 2, "baseline_at_ms": now - 86_400_000},
            },
            "orderbook": {
                "spread_bps": 2, "observed_at_ms": now,
                "slippage_buy_bps": 5, "slippage_sell_bps": 5,
                "reference_notional_usd": 2000,
            },
        },
    }
    features = {"timeframes": frames, "4h": frames["4h"],
                "turnover_ratio_4h": 1.5, "data_gaps": []}
    enrichment = {
        "identity": {"status": "VERIFIED_PROJECT", "symbol": "TEST"},
        "fundamentals": {"market_cap_usd": 10_000_000, "observed_at": at, "last_updated": at},
    }
    evidence = [{
        "symbol": "TESTUSDT", "kind": "unlock", "verified": True, "fresh": True,
        "observed_at": at, "expires_at": "2026-11-21T12:16:00+00:00",
        "source_url": "https://example.com/test-official-tokenomics",
        "facts": {"circulating_pct_unlock_30d": 0, "lookahead_days": 30, "recipients": ["team"]},
    }]
    return plan, market, features, {"label": "RISK_ON"}, enrichment, evidence, now


class ReviewReadinessTests(unittest.TestCase):
    def setUp(self):
        self.args = valid_fixture()

    def decision(self):
        return readiness(*self.args)

    def assert_wait(self):
        result = self.decision()
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["status"], "WAIT")
        self.assertTrue(result["waiting_for"])

    def test_actual_feature_schema_can_be_ready_when_all_checks_pass(self):
        result = self.decision()
        self.assertTrue(result["ready"], result)

    def test_builder_wait_cannot_be_promoted(self):
        self.args[0].update(status="WAIT", reasons=["MANUAL_STRUCTURAL_BLOCK"])
        self.assert_wait()

    def test_expired_immutable_plan_cannot_be_ready(self):
        self.args[0]["expires_at_ms"] = self.args[-1] - 1
        self.assert_wait()

    def test_gapped_confirmation_cannot_be_ready(self):
        self.args[2]["timeframes"]["15m"]["data_gaps"] = ["CANDLE_GAP"]
        self.assert_wait()

    def test_negative_marketcap_is_not_coverage(self):
        self.args[4]["fundamentals"]["market_cap_usd"] = -10
        self.assert_wait()

    def test_zero_marketcap_is_not_coverage(self):
        self.args[4]["fundamentals"]["market_cap_usd"] = 0
        self.assert_wait()

    def test_negative_slippage_is_invalid(self):
        self.args[1]["derivatives"]["orderbook"]["slippage_buy_bps"] = -100
        self.assert_wait()

    def test_negative_spread_is_invalid(self):
        self.args[1]["derivatives"]["orderbook"]["spread_bps"] = -1
        self.assert_wait()

    def test_negative_notional_is_invalid(self):
        self.args[0]["position_notional_usd"] = -100
        self.assert_wait()

    def test_stale_funding_not_saved_by_fresh_ticker(self):
        self.args[1]["derivatives"]["funding_observed_at_ms"] = self.args[-1] - 86_400_000
        self.assert_wait()

    def test_missing_oi_windows_does_not_confirm_positioning(self):
        self.args[1]["derivatives"].pop("oi_windows")
        self.assert_wait()

    def test_insufficient_confirmation_history_is_not_ready(self):
        self.args[2]["timeframes"]["15m"].update(count_closed=13, history_status="HISTORY_INSUFFICIENT")
        self.assert_wait()

    def test_stale_benchmark_is_not_risk_on(self):
        stale = copy.deepcopy(self.args[2])
        stale["timeframes"]["4h"]["last_close_time"] -= 7 * 86_400_000
        class Provider:
            def __init__(self, **kwargs):
                pass
            def collect(self, symbol, **kwargs):
                return {"ticker_observed_at_ms": self_args[-1] - 7 * 86_400_000,
                        "candles": {}, "errors": []}
        self_args = self.args
        with patch("moonshot_scanner.pipeline.analyze_features", return_value=stale):
            context = Scanner(None, provider_factory=Provider).market_context("bitget", self.args[-1])
        self.assertNotEqual(context["label"], "RISK_ON")


class ReviewEvidenceAndNumericTests(unittest.TestCase):
    def test_nonfinite_timestamps_are_missing_not_exceptions(self):
        self.assertIsNone(timestamp(float("nan")))
        self.assertIsNone(timestamp(float("inf")))
        self.assertIsNone(timestamp(-float("inf")))

    def test_boolean_is_not_a_numeric_measurement(self):
        self.assertIsNone(number(True))
        self.assertIsNone(number(False))

    def test_asset_unlock_may_not_use_global_wildcard(self):
        row = valid_fixture()[5][0]
        row.pop("fresh")
        row["symbol"] = "*"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "review.sqlite3")
            with self.assertRaises(ValueError):
                store.add_evidence(row)

    def test_global_macro_context_is_still_supported(self):
        row = valid_fixture()[5][0]
        row.pop("fresh")
        row.update(symbol="*", kind="macro", facts={"event": "example reviewed macro event"})
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "review.sqlite3")
            evidence_id = store.add_evidence(row)
            self.assertTrue(evidence_id)


if __name__ == "__main__":
    unittest.main()
