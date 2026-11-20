import math
import unittest

from moonshot_scanner.features import analyze_features
from moonshot_scanner.plans import build_trade_plans, validate_plan
from moonshot_scanner.backtest import chronological_split, run_backtest


def candles(n, *, interval=14_400_000, start=1_700_000_000_000, price=100.0, turnover=1000.0):
    return [{"open_time": start + i * interval, "close_time": start + (i + 1) * interval,
             "open": price, "high": price + 1, "low": price - 1, "close": price,
             "base_volume": 10, "quote_turnover": turnover, "is_closed": True}
            for i in range(n)]


class ClosedFeatureTests(unittest.TestCase):
    def test_developing_candle_excluded(self):
        rows = candles(44)
        rows[-1].update(high=1000, close=999, quote_turnover=1e12, is_closed=False)
        now = rows[-1]["open_time"] + 1000
        output = analyze_features({"4h": rows}, now)
        self.assertEqual(output["4h"]["count_closed"], 43)
        self.assertEqual(output["4h"]["last_close"], 100)
        self.assertEqual(output["turnover_ratio_4h"], 1)
        self.assertTrue(output["current_4h_developing"])

    def test_future_closed_label_cannot_confirm(self):
        rows = candles(44)
        now = rows[-1]["open_time"] + 1000
        output = analyze_features({"4H": rows}, now)
        self.assertEqual(output["4h"]["count_closed"], 43)

    def test_baseline_previous42_median_not_current(self):
        rows = candles(43)
        rows[-1]["quote_turnover"] = 6000
        output = analyze_features({"4h": rows}, rows[-1]["close_time"])
        self.assertEqual(output["baseline_4h_median_7d"], 1000)
        self.assertEqual(output["turnover_ratio_4h"], 6)
        self.assertEqual(output["discovery_status"], "EARLY")
        self.assertIsNone(output["4h"]["ema200"])
        self.assertIsNone(output["probability"])

    def test_gap_invalidates_seven_day_baseline(self):
        rows = candles(44)
        del rows[20]
        output = analyze_features({"4h": rows}, rows[-1]["close_time"])
        self.assertIsNone(output["turnover_ratio_4h"])
        self.assertIn("4h:CANDLE_GAP", output["data_gaps"])

    def test_invalid_ohlc_removed(self):
        rows = candles(44)
        rows[-1]["high"] = 90
        output = analyze_features({"4h": rows}, rows[-1]["close_time"])
        self.assertEqual(output["4h"]["count_closed"], 43)
        self.assertIn("4h:INVALID_OHLC_GEOMETRY", output["data_gaps"])

    def test_short_history_survives_as_watch(self):
        rows = candles(12)
        output = analyze_features({"4h": rows}, rows[-1]["close_time"])
        self.assertEqual(output["discovery_status"], "WATCH")
        self.assertEqual(output["4h"]["history_status"], "HISTORY_INSUFFICIENT")
        self.assertIsNone(output["early_discovery"])

    def test_ema_needs_real_history(self):
        rows = candles(200)
        output = analyze_features({"4h": rows}, rows[-1]["close_time"])
        self.assertEqual(output["4h"]["ema200"], 100)
        self.assertEqual(output["4h"]["rsi14"], 50)


class PlanTests(unittest.TestCase):
    def fixture(self):
        return {"4h": {"atr14": 2, "last_close_time": 100_000,
                       "support_levels": [80, 90, 95, 100],
                       "resistance_levels": [115, 150, 170],
                       "structural_support": 95, "structural_resistance": 115}}

    def test_bless_breakout_target_equal_entry_rejected(self):
        bad = {"side": "LONG", "entry_low": .0193847, "entry_high": .0193847,
               "stop": .0097595, "targets": [.0193847, .0161146]}
        errors = validate_plan(bad)
        self.assertIn("LONG_LEVEL_GEOMETRY_INVALID", errors)
        self.assertIn("LONG_TARGETS_NOT_INCREASING", errors)

    def test_short_cannot_use_long_geometry(self):
        bad = {"side": "SHORT", "entry_low": 3.015, "entry_high": 3.089,
               "stop": 2.811, "targets": [3.477, 3.775]}
        self.assertIn("SHORT_LEVEL_GEOMETRY_INVALID", validate_plan(bad))

    def test_rr_below_two_blocked(self):
        plan = {"side": "LONG", "entry_low": 100, "entry_high": 100,
                "stop": 90, "targets": [115]}
        self.assertIn("NET_RR_BELOW_MINIMUM", validate_plan(plan))

    def test_costs_and_worst_entry_in_zone(self):
        plan = {"side": "LONG", "entry_low": 99, "entry_high": 100,
                "stop": 90, "targets": [120], "fees_bps": 10, "slippage_bps": 10}
        self.assertIn("NET_RR_BELOW_MINIMUM", validate_plan(plan))

    def test_long_plans_separate_entries_and_targets(self):
        plans = build_trade_plans(side="LONG", features=self.fixture(), price=105,
                                  account_usd=1000, risk_pct=1)
        self.assertEqual([p["setup_type"] for p in plans], ["PULLBACK", "BREAKOUT"])
        self.assertNotEqual(plans[0]["entry_mid"], plans[1]["entry_mid"])
        for plan in plans:
            self.assertEqual(validate_plan(plan), [])
            self.assertGreater(plan["tp1"], plan["entry_high"])
            self.assertEqual(plan["risk_budget_usd"], 10)
            self.assertEqual(plan["status"], "CONDITIONAL")

    def test_short_plans_mirror_levels(self):
        features = {"4h": {"atr14": 2, "last_close_time": 100_000,
                            "support_levels": [30, 50, 85],
                            "resistance_levels": [100, 105, 110, 120],
                            "structural_support": 85, "structural_resistance": 105}}
        plans = build_trade_plans(side="SHORT", features=features, price=95,
                                  account_usd=1000, risk_pct=1)
        for plan in plans:
            self.assertEqual(validate_plan(plan), [])
            self.assertLess(plan["tp1"], plan["entry_low"])
            self.assertGreater(plan["stop"], plan["entry_high"])

    def test_near_obstacle_not_skipped_to_inflate_rr(self):
        f = self.fixture()
        f["4h"]["resistance_levels"] = [105, 150]
        p = build_trade_plans(side="LONG", features=f, price=104,
                              account_usd=1000, risk_pct=1)[0]
        self.assertEqual(p["tp1"], 105)
        self.assertEqual(p["status"], "WAIT")
        self.assertIsNone(p["position_units"])

    def test_no_target_no_invented_runner(self):
        f = self.fixture()
        f["4h"]["resistance_levels"] = [115]
        p = build_trade_plans(side="LONG", features=f, price=105,
                              account_usd=1000, risk_pct=1)[1]
        self.assertEqual(p["status"], "WAIT")
        self.assertEqual(p["targets"], [])

    def test_risk_above_rulebook_rejected(self):
        plans = build_trade_plans(side="LONG", features=self.fixture(), price=105,
                                  account_usd=1000, risk_pct=2)
        self.assertTrue(all(p["status"] == "WAIT" for p in plans))

    def test_invalid_costs_do_not_create_nan_plans(self):
        for value in (float("nan"), float("inf"), -1, None, True):
            plans = build_trade_plans(side="LONG", features=self.fixture(), price=105,
                                      account_usd=1000, risk_pct=1, fees_bps=value)
            self.assertTrue(all(p["status"] == "WAIT" for p in plans))

    def test_missing_costs_never_silently_assumed_free(self):
        plan = {"side": "LONG", "entry_low": 100, "entry_high": 100,
                "stop": 90, "targets": [130]}
        self.assertIn("MISSING_COST_ASSUMPTIONS", validate_plan(plan))

    def test_boolean_plan_levels_and_costs_are_not_numbers(self):
        plan = {"side": "LONG", "entry_low": 1, "entry_high": 1,
                "stop": .5, "targets": [3], "fees_bps": 0, "slippage_bps": 0}
        plan["entry_low"] = True
        self.assertIn("MISSING_OR_NONFINITE_PLAN_LEVELS", validate_plan(plan))
        plan["entry_low"] = 1
        plan["fees_bps"] = True
        self.assertIn("INVALID_COST_ASSUMPTIONS", validate_plan(plan))

    def test_rr_threshold_cannot_be_disabled_or_nan(self):
        plan = {"side": "LONG", "entry_low": 100, "entry_high": 100,
                "stop": 90, "targets": [130], "fees_bps": 0, "slippage_bps": 0}
        for minimum in (0, 1, float("nan"), True):
            with self.subTest(minimum=minimum):
                self.assertIn("INVALID_RR_THRESHOLD", validate_plan(plan, minimum))

    def test_position_notional_does_not_silently_assume_leverage(self):
        f = self.fixture()
        f["4h"].update(support_levels=[99.8, 100], atr14=.01)
        plan = build_trade_plans(side="LONG", features=f, price=101,
                                 account_usd=1000, risk_pct=1)[0]
        self.assertEqual(plan["status"], "CONDITIONAL")
        self.assertLessEqual(plan["position_notional_usd"], 1000)


class BacktestTests(unittest.TestCase):
    def test_initial_loss_is_drawdown(self):
        rows = candles(3)
        rows[1].update(low=94, close=95)
        out = run_backtest(rows, [{"signal_index": 0, "side": "LONG", "stop": 95, "target": 120}],
                           fees_bps=0, slippage_bps=0)
        self.assertEqual(out["trade_count"], 1)
        self.assertEqual(out["expectancy_r"], -1)
        self.assertEqual(out["max_drawdown_r"], 1)
        self.assertEqual(out["equity_curve_r"], [0, -1])

    def test_three_losses_include_all_three(self):
        rows = candles(7)
        for i in (1, 3, 5):
            rows[i].update(low=94, close=95)
        signals = [{"signal_index": i, "side": "LONG", "stop": 95, "target": 120} for i in (0, 2, 4)]
        out = run_backtest(rows, signals, fees_bps=0, slippage_bps=0)
        self.assertEqual(out["max_drawdown_r"], 3)

    def test_samebar_stop_first(self):
        rows = candles(3)
        rows[1].update(low=90, high=125)
        out = run_backtest(rows, [{"signal_index": 0, "side": "LONG", "stop": 95, "target": 120}],
                           fees_bps=0, slippage_bps=0)
        self.assertEqual(out["trades"][0]["exit_reason"], "STOP")
        self.assertTrue(out["trades"][0]["same_bar_path_ambiguous"])

    def test_fill_cannot_use_signal_bar_high(self):
        rows = candles(4)
        rows[0].update(high=120)
        sig = {"signal_index": 0, "side": "LONG", "entry_mode": "BREAKOUT",
               "entry": 110, "stop": 95, "target": 130}
        out = run_backtest(rows, [sig], fees_bps=0, slippage_bps=0)
        self.assertEqual(out["trade_count"], 0)
        self.assertEqual(out["rejected_signals"][0]["reason"], "ENTRY_NOT_FILLED_BEFORE_EXPIRY")

    def test_market_fill_next_open_with_costs(self):
        rows = candles(3)
        rows[1].update(open=102, high=103, low=94, close=95)
        out = run_backtest(rows, [{"signal_index": 0, "side": "LONG", "stop": 95, "target": 120}])
        self.assertGreater(out["trades"][0]["entry"], 102)
        self.assertLess(out["expectancy_r"], -1)
        self.assertGreater(out["max_drawdown_r"], 1)

    def test_gap_through_stop_not_filled_at_stop(self):
        rows = candles(3)
        rows[2].update(open=90, low=89, high=92, close=91)
        out = run_backtest(rows, [{"signal_index": 0, "side": "LONG", "stop": 95, "target": 120}],
                           fees_bps=0, slippage_bps=0)
        self.assertEqual(out["trades"][0]["exit"], 90)
        self.assertEqual(out["expectancy_r"], -2)

    def test_developing_signal_rejected(self):
        rows = candles(3)
        rows[1]["is_closed"] = False
        out = run_backtest(rows, [{"signal_index": 1, "side": "LONG", "stop": 95, "target": 120}])
        self.assertEqual(out["trade_count"], 0)

    def test_no_signal_no_fake_backtest(self):
        out = run_backtest(candles(30))
        self.assertEqual(out["status"], "NOT_RUN")
        self.assertIsNone(out["win_rate"])
        self.assertIsNone(out["probability_next_trade"])

    def test_breakout_cancelled_if_invalidation_breaks_before_trigger(self):
        rows = candles(4)
        rows[1].update(low=94)
        rows[2].update(high=115)
        sig = {"signal_index": 0, "side": "LONG", "entry_mode": "BREAKOUT",
               "entry": 110, "stop": 95, "target": 130}
        out = run_backtest(rows, [sig], fees_bps=0, slippage_bps=0)
        self.assertEqual(out["trade_count"], 0)
        self.assertEqual(out["rejected_signals"][0]["reason"], "INVALIDATION_BEFORE_ENTRY")

    def test_purged_time_split(self):
        out = chronological_split(candles(100), .7, 18)
        self.assertEqual(len(out["train"]), 70)
        self.assertEqual(len(out["test"]), 12)
        self.assertEqual(out["test_start_index"], 88)


if __name__ == "__main__":
    unittest.main()
