"""Offline regression checks: python -m rose_monitor.engine_selfcheck."""
import copy
import datetime as dt
import json
import unittest
from urllib.parse import urlparse, parse_qs

from rose_monitor.engine import (INTERVALS, _book, _candles, _explicit_plan, _oi,
                                 _timeframe, _turnover, analyze_signal, format_analysis)

NOW = int(dt.datetime(2026, 11, 18, 12, 5, tzinfo=dt.timezone.utc).timestamp() * 1000)


def candles(interval, rising=True, open_bar=True):
    step = INTERVALS[interval]
    first = (NOW // step - 80) * step
    rows = []
    for i in range(80):
        price = 100 + i * .05 if rising else 110 - i * .05
        stamp = first + i * step
        turnover = 400 if i == 79 else 100
        rows.append([stamp, str(price), str(price + .01), str(price - .01), str(price), "20", stamp + step - 1, str(turnover), 100, "10", str(turnover * .6), "0"])
    if open_bar:
        stamp = NOW // step * step
        rows.append([stamp, "2000", "3000", "1", "2000", "100000", stamp + step - 1, "100000000", 100, "50000", "50000000", "0"])
    return rows


class FakePublic:
    def __init__(self, rising=True, fail_oi=False, missing_funding_interval=False):
        self.rising = rising
        self.fail_oi = fail_oi
        self.missing_funding_interval = missing_funding_interval
        self.calls = []

    def __call__(self, url, timeout=8):
        self.calls.append((url, timeout))
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path
        if path.endswith("ticker/24hr"):
            return {"lastPrice": "103.95" if self.rising else "106.05", "quoteVolume": "10000000", "priceChangePercent": "5" if self.rising else "-5", "closeTime": NOW}
        if path.endswith("klines"):
            if params["interval"] == "1m":
                start = int(params["startTime"])
                return [[start, "100", "101", "99", "100", "10", start + 59999, "1000", 10, "5", "500", "0"]]
            return candles(params["interval"], self.rising)
        if path.endswith("depth"):
            price = 103.95 if self.rising else 106.05
            return {"T": NOW, "bids": [[str(price * .9999), "2000"], [str(price * .997), "2000"], [str(price * .99), "2000"]],
                    "asks": [[str(price * 1.0001), "2000"], [str(price * 1.003), "2000"], [str(price * 1.01), "2000"]]}
        if path.endswith("openInterestHist"):
            if self.fail_oi:
                raise TimeoutError("Injected history outage")
            last = NOW // INTERVALS["1h"] * INTERVALS["1h"]
            return [{"timestamp": last - h * INTERVALS["1h"], "sumOpenInterest": "1000", "sumOpenInterestValue": str(10**h)} for h in range(30)]
        if path.endswith("premiumIndex"):
            return {"lastFundingRate": "0.00001", "nextFundingTime": NOW + 100000}
        if path.endswith("fundingInfo"):
            return [] if self.missing_funding_interval else [{"symbol": s + "USDT", "fundingIntervalHours": 8} for s in ("APT", "BTC", "ETH")]
        raise AssertionError("Unexpected public endpoint: " + url)


def signal(**updates):
    value = {"post_id": 123, "published_utc": "2026-11-18T11:55:12Z", "text": "#APT LONG\nEntry: 100\nSL: 95\nTP1: 110", "symbols": ["APT"], "direction": "long", "url": "https://t.me/RoseSignalsPremium/123", "image_only": False}
    value.update(updates)
    return value


class CalculationChecks(unittest.TestCase):
    def test_current_candle_never_confirms(self):
        bars = _candles(candles("4h"), "binance", INTERVALS["4h"], NOW)
        self.assertEqual(len(bars), 80)
        self.assertAlmostEqual(bars[-1]["close"], 103.95)
        frame = _timeframe(bars, "4h", NOW)
        self.assertTrue(frame["fresh"])
        self.assertLess(frame["ema13"], 104)
        self.assertEqual(frame["trend"], "bullish")
        turnover = _turnover(bars)
        self.assertEqual(turnover["prior_bars"], 42)
        self.assertEqual(turnover["ratio"], 4)
        self.assertTrue(turnover["meets_3x"])

    def test_missing_candle_breaks_confirmation(self):
        bars = _candles(candles("4h"), "binance", INTERVALS["4h"], NOW)
        del bars[-10]
        self.assertFalse(_timeframe(bars, "4h", NOW)["contiguous_last42"])
        self.assertFalse(_turnover(bars)["meets_3x"])

    def test_oi_uses_tokens_and_exact_timestamps(self):
        last = NOW // INTERVALS["1h"] * INTERVALS["1h"]
        rows = [{"timestamp": last - h * INTERVALS["1h"], "sumOpenInterest": 120 if h == 0 else 100, "sumOpenInterestValue": 999999999 if h == 0 else 1} for h in range(25)]
        evidence = _oi(rows, "binance", 1000, NOW)
        self.assertEqual(evidence["token_units"], 120000)
        self.assertAlmostEqual(evidence["change4h_pct"], 20)
        self.assertAlmostEqual(evidence["change24h_pct"], 20)
        rows = [row for row in rows if row["timestamp"] != last - 4 * INTERVALS["1h"]]
        self.assertIsNone(_oi(rows, "binance", 1, NOW)["change4h_pct"])

    def test_depth_has_price_times_quantity_and_boundary(self):
        result = _book({"bids": [[99.99, 2], [99.6, 3], [99, 100]], "asks": [[100.01, 2], [100.4, 3], [101, 100]]}, "binance", NOW)
        self.assertAlmostEqual(result["bid_depth_usdt"], 99.99 * 2 + 99.6 * 3)
        self.assertAlmostEqual(result["ask_depth_usdt"], 100.01 * 2 + 100.4 * 3)
        self.assertAlmostEqual(result["spread_bps"], 2)

    def test_explicit_only_numeric_plans_and_directional_rr(self):
        plan = _explicit_plan("Entry: 100\nSL: 95\nTP1: 110", "long")
        self.assertEqual(plan["target_reward_risk"], [2])
        plan = _explicit_plan("Entry: 100\nSL: 105\nTP1: 90", "short")
        self.assertEqual(plan["target_reward_risk"], [2])
        plan = _explicit_plan("TP: 10%\nBuy before it moons 10x. Use 20x leverage", "long")
        self.assertIsNone(plan["entry"])
        self.assertEqual(plan["targets"], [])

    def test_end_to_end_fresh_spot_confirmation(self):
        fake = FakePublic()
        result = analyze_signal(signal(), http_get=fake, now_ms=NOW)
        self.assertEqual(result["status"], "CONFIRMED")
        coin = result["candidates"][0]
        self.assertEqual(coin["publication_benchmark"]["requested_utc"], "2026-11-18T11:56:00Z")
        self.assertAlmostEqual(coin["publication_benchmark"]["change_to_latest_spot_pct"], 3.95)
        self.assertIsNone(result["probability"])
        self.assertTrue(all(timeout <= 8 for _, timeout in fake.calls))
        json.dumps(result, allow_nan=False)
        self.assertIn("TECHNICAL CHECKS PASSED", format_analysis(result))

    def test_shorts_invert_trend_macro_and_stop(self):
        result = analyze_signal(signal(direction="short", text="#APT SHORT\nEntry: 100\nSL: 105\nTP: 90"), http_get=FakePublic(rising=False), now_ms=NOW)
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["btc_eth_regime"]["regime"], "bearish")

    def test_image_only_and_data_failures_never_confirm(self):
        for post, fake in ((signal(image_only=True), FakePublic()), (signal(), FakePublic(fail_oi=True)), (signal(), FakePublic(missing_funding_interval=True))):
            with self.subTest(post=post["image_only"], fake=fake.__dict__):
                result = analyze_signal(post, http_get=fake, now_ms=NOW)
                self.assertNotEqual(result["status"], "CONFIRMED")
                if fake.fail_oi:
                    self.assertTrue(any("Injected history outage" in fail["error"] for fail in result["request_failures"]))

    def test_future_or_timezone_free_timestamp_waits(self):
        for timestamp in ("2026-11-18T12:06:00Z", "2026-11-18T11:55:00", None):
            result = analyze_signal(signal(published_utc=timestamp), http_get=FakePublic(), now_ms=NOW)
            self.assertEqual(result["status"], "WAIT")

    def test_bybit_closed_candle_sort_and_turnover_position(self):
        raw = {"result": {"list": [[str(row[0])] + row[1:6] + [row[7]] for row in reversed(candles("4h"))]}}
        bars = _candles(raw, "bybit", INTERVALS["4h"], NOW)
        self.assertEqual(len(bars), 80)
        self.assertEqual(_turnover(bars)["ratio"], 4)

    def test_stale_book_and_ticker_cannot_confirm(self):
        for stale in ("book", "ticker"):
            fake = FakePublic()
            def get(url, timeout=8):
                data = fake(url, timeout)
                if stale == "book" and urlparse(url).path.endswith("depth"):
                    data["T"] = NOW - 31_000
                if stale == "ticker" and urlparse(url).path.endswith("ticker/24hr"):
                    data["closeTime"] = NOW - 121_000
                return data
            result = analyze_signal(signal(), http_get=get, now_ms=NOW)
            self.assertNotEqual(result["status"], "CONFIRMED")

    def test_old_publication_is_not_live_confirmation(self):
        result = analyze_signal(signal(published_utc="2026-11-18T11:00:00Z"), http_get=FakePublic(), now_ms=NOW)
        self.assertEqual(result["status"], "WAIT")
        self.assertIn("signal_is_recent", result["candidates"][0]["reasons"])

    def test_ranges_missing_levels_and_multicoin_need_review(self):
        for text in ("Entry: 1.2–1.3\nSL: 1.1\nTP: 2", "Entry: 100\nSL: 95\nTP: 110, 120", "#APT LONG"):
            result = analyze_signal(signal(text=text), http_get=FakePublic(), now_ms=NOW)
            self.assertEqual(result["status"], "WATCH")
            self.assertIn("explicit_risk_plan_ge_2r", result["candidates"][0]["reasons"])
        result = analyze_signal(signal(symbols=["APT", "BTC"]), http_get=FakePublic(), now_ms=NOW)
        self.assertTrue(all(not c["explicit_plan"]["directional_stop_valid"] for c in result["candidates"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
