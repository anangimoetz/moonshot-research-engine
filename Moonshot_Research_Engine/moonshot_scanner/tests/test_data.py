"""Regression coverage for identity pollution and unsafe market-data defaults."""
import copy
import datetime as dt
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from moonshot_scanner.identity import aggregate_dex_pairs, get_identity, normalize_contract
from moonshot_scanner.providers import PublicMarketData, _book_metrics, _flow_metrics, normalize_candles

NOW = 1795146000000
EVAA = "0xaa036928c9c0Df07d525B55ea8EE690Bb5a628C1"


def pool(address=EVAA, chain="bsc", pair="0x" + "a" * 40, price="5.5", liquidity=100000, quote=50000):
    return {"chainId": chain, "pairAddress": pair, "baseToken": {"symbol": "EVAA", "address": address},
            "quoteToken": {"symbol": "USDT", "address": "0x55d398326f99059fF775485246999027B3197955"},
            "priceUsd": price, "liquidity": {"usd": liquidity, "quote": quote, "base": 9090},
            "volume": {"h24": 10000}, "txns": {"h1": {"buys": 30, "sells": 20}},
            "url": "https://dexscreener.com/bsc/test", "dexId": "test"}


def candles(count=12, timeframe=14400000):
    start = NOW // timeframe * timeframe - (count - 1) * timeframe
    return [[str(start + i * timeframe), "5", "6", "4", "5.5", "100", "550"] for i in range(count)]


class FakeBitget:
    def __init__(self, spot=True, perp=True):
        self.spot, self.perp, self.calls = spot, perp, []

    def __call__(self, url, timeout=6):
        self.calls.append((url, timeout))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        path = urllib.parse.urlsplit(url).path
        category = query.get("category", [""])[0]
        data = []
        if path.endswith("instruments"):
            active = self.spot if category == "SPOT" else self.perp
            data = [{"symbol": "EVAAUSDT", "baseCoin": "EVAA", "quoteCoin": "USDT", "status": "online", "symbolType": "spot"}] if active else []
        elif path.endswith("tickers"):
            data = [{"symbol": "EVAAUSDT", "lastPrice": "5.5", "price24hPcnt": ".1", "turnover24h": "900000", "ts": str(NOW - 1000), "openInterest": "3000000", "fundingRate": ".00005"}]
        elif path.endswith("candles"):
            interval = query.get("interval", query.get("granularity"))[0]
            tf = {"15m": 900000, "1H": 3600000, "4H": 14400000, "1D": 86400000, "1Dutc": 86400000}[interval]
            data = candles(timeframe=tf)
        elif path.endswith("orderbook"):
            data = {"b": [[5.49, 1000], [5.48, 1000]], "a": [[5.51, 1000], [5.52, 1000]], "ts": str(NOW - 500)}
        elif path.endswith("fills"):
            data = [{"execId": "1", "price": "5.5", "size": "100", "side": "buy", "ts": str(NOW - 1000)},
                    {"execId": "2", "price": "5.5", "size": "50", "side": "sell", "ts": str(NOW - 2000)}]
        elif path.endswith("current-fund-rate"):
            data = [{"symbol": "EVAAUSDT", "fundingRate": ".00005", "fundingRateInterval": "4", "nextUpdate": str(NOW + 100000)}]
        else:
            raise AssertionError("Unexpected public endpoint " + path)
        return {"code": "00000", "requestTime": NOW, "data": data}


class IdentityTests(unittest.TestCase):
    def test_bless_id_does_not_accidentally_select_another_project(self):
        identity = get_identity("BLESSUSDT")
        self.assertEqual(identity["coingecko_id"], "bless-2")
        self.assertEqual(identity["contracts"], [])

    def test_unknown_project_has_no_verified_contract(self):
        identity = get_identity("TOTALLYNEWUSDT")
        got = aggregate_dex_pairs(identity, [pool()], 5.5, NOW)
        self.assertEqual(identity["status"], "UNKNOWN")
        self.assertIsNone(got["dex_liquidity_usd"])
        self.assertEqual(got["liquidity_risk"], "UNKNOWN")

    def test_sol_ticker_copy_is_excluded_even_with_huge_liquidity(self):
        valid = pool()
        fake = pool("FakeSolanaAddress", "solana", "FakePool", "5.5", 32000000000, 10)
        got = aggregate_dex_pairs(get_identity("EVAAUSDT"), [fake, valid], 5.5, NOW)
        self.assertEqual(got["dex_liquidity_usd"], 100000)
        self.assertEqual(got["rejected"][0]["reason"], "UNVERIFIED_TOKEN_CONTRACT")
        self.assertEqual(got["liquidity_risk"], "UNKNOWN")

    def test_verified_pools_dedup_by_chain_and_pair(self):
        a = pool()
        b = copy.deepcopy(a)
        b["pairAddress"] = a["pairAddress"].upper().replace("0X", "0x")
        got = aggregate_dex_pairs(get_identity("EVAA"), [a, b], 5.5, NOW)
        self.assertEqual(len(got["pools"]), 1)
        self.assertEqual(got["rejected"][0]["reason"], "DUPLICATE_POOL")
        self.assertAlmostEqual(got["buy_transaction_share_1h"], .6)

    def test_solana_contract_case_is_not_normalized(self):
        identity = get_identity("SOL")
        mint = identity["contracts"][0]["address"]
        self.assertNotEqual(normalize_contract("solana", mint), normalize_contract("solana", mint.lower()))
        p = pool(mint.lower(), "solana", "mypool", "72", 100000, 50000)
        got = aggregate_dex_pairs(identity, [p], 72, NOW)
        self.assertEqual(got["status"], "UNKNOWN")

    def test_evm_contract_case_is_accepted(self):
        got = aggregate_dex_pairs(get_identity("EVAA"), [pool(EVAA.lower())], 5.5, NOW)
        self.assertEqual(got["status"], "VERIFIED_POOLS")

    def test_counterfeit_quote_is_excluded_even_with_verified_base(self):
        bad = pool()
        bad["quoteToken"]["address"] = "0x" + "b" * 40
        got = aggregate_dex_pairs(get_identity("EVAA"), [bad], 5.5, NOW)
        self.assertEqual(got["status"], "UNKNOWN")
        self.assertEqual(got["rejected"][0]["reason"], "UNVERIFIED_QUOTE_CONTRACT")

    def test_price_divergence_and_one_sided_stable_reserve_are_rejected(self):
        a = pool(price="1.44")
        b = pool(pair="0x" + "c" * 40, liquidity=30000000, quote=7000)
        got = aggregate_dex_pairs(get_identity("EVAA"), [a, b], 5.5, NOW)
        self.assertEqual(got["status"], "UNKNOWN")
        self.assertEqual({x["reason"] for x in got["rejected"]}, {"PRICE_DIVERGENCE_OVER_15_PCT", "QUOTED_STABLE_RESERVE_TOO_SMALL_OR_UNKNOWN"})


class CandleAndBookTests(unittest.TestCase):
    def test_partial_candle_is_retained_but_never_marked_closed(self):
        got = normalize_candles(candles(), "4h", NOW)
        self.assertEqual(len(got), 12)
        self.assertTrue(got[-2]["is_closed"])
        self.assertFalse(got[-1]["is_closed"])
        self.assertEqual(got[-1]["close_time"] - got[-1]["open_time"], 14400000)
        self.assertEqual(got[-1]["quote_turnover"], 550)

    def test_invalid_ohlc_and_missing_quote_turnover_are_not_filled(self):
        rows = candles(4)
        rows[0][2] = "1"
        rows[1][6] = "nan"
        got = normalize_candles(rows, "4h", NOW)
        self.assertEqual(len(got), 2)

    def test_daily_with_utc_plus_eight_boundary_is_rejected(self):
        rows = candles(3, 86400000)
        for row in rows:
            row[0] = str(int(row[0]) - 8 * 3600000)
        self.assertEqual(normalize_candles(rows, "1d", NOW), [])

    def test_book_does_not_claim_full_execution_if_depth_insufficient(self):
        book = _book_metrics({"b": [[99, 1]], "a": [[101, 1]]}, reference_notional_usd=1000)
        self.assertLess(book["buy_fill_fraction"], 1)
        self.assertIsNone(book["buy_slippage_bps_from_mid"])
        self.assertIsNone(book["observed_at_ms"])

    def test_crossed_book_is_unknown(self):
        book = _book_metrics({"b": [[101, 100]], "a": [[100, 100]]})
        self.assertEqual(book["status"], "UNKNOWN")

    def test_zero_reference_notional_does_not_divide_by_zero(self):
        self.assertEqual(_book_metrics({"b": [[99, 10]], "a": [[101, 10]]}, reference_notional_usd=0)["status"], "UNKNOWN")

    def test_trade_sample_is_deduplicated_and_not_called_cvd(self):
        trade = {"execId": "1", "price": "10", "size": "2", "side": "buy", "ts": str(NOW)}
        sample = _flow_metrics([trade, trade], "bitget")
        self.assertEqual(sample["sample_count"], 1)
        self.assertEqual(sample["buy_notional_usd"], 20)
        self.assertIsNone(sample["cvd"])


class ProviderTests(unittest.TestCase):
    def test_collect_preserves_short_history_with_correct_metadata(self):
        fake = FakeBitget()
        result = PublicMarketData(http_get=fake).collect("EVAA", NOW)
        self.assertEqual(result["price"], 5.5)
        self.assertEqual(result["change_24h_pct"], 10)
        self.assertEqual(len(result["candles"]["1d"]), 12)
        self.assertEqual(result["candles"]["1d"][0]["open_time"] % 86400000, 0)
        self.assertTrue(any("granularity=1Dutc" in url for url, _ in fake.calls))
        self.assertTrue(result["spot_available"])
        self.assertFalse(result["errors"])
        self.assertEqual(result["ticker_observed_at_ms"], NOW - 1000)
        self.assertEqual(result["derivatives"]["open_interest_base_units"], 3000000)
        self.assertEqual(result["derivatives"]["funding_interval_hours"], 4)
        self.assertEqual(result["derivatives"]["funding_rate_pct"], .005)
        self.assertIsNone(result["derivatives"]["funding_observed_at_ms"])
        funding_source = next(s for s in result["sources"] if s["name"] == "funding")
        self.assertIsNone(funding_source["observed_at_ms"])
        self.assertEqual(funding_source["server_response_time_ms"], NOW)

    def test_perpetual_only_is_not_disguised_as_spot_demand(self):
        result = PublicMarketData(http_get=FakeBitget(spot=False)).collect("EVAA", NOW)
        self.assertEqual(result["product"], "USDT-FUTURES")
        self.assertFalse(result["spot_available"])
        self.assertEqual(result["candles"]["4h"][0]["product"], "USDT-FUTURES")
        self.assertIn("SPOT_DEMAND_UNAVAILABLE", [x["code"] for x in result["errors"]])

    def test_discovery_excludes_stock_but_keeps_new_crypto(self):
        def fake(url, timeout=6):
            if "instruments" in url:
                return {"code": "00000", "data": [
                    {"symbol": "NEWUSDT", "baseCoin": "NEW", "quoteCoin": "USDT", "status": "online", "symbolType": "spot"},
                    {"symbol": "RDKILYUSDT", "baseCoin": "rDKILY", "quoteCoin": "USDT", "status": "online", "instrumentType": "stock", "isReality": "yes"}]}
            return {"code": "00000", "data": [
                {"symbol": "NEWUSDT", "lastPrice": "1", "turnover24h": "1000000", "price24hPcnt": ".01"},
                {"symbol": "RDKILYUSDT", "lastPrice": "10", "turnover24h": "2000000", "price24hPcnt": ".1"}]}
        data = PublicMarketData(http_get=fake).discover()
        self.assertEqual([x["symbol"] for x in data], ["NEWUSDT"])
        self.assertTrue(data[0]["universe_only"])

    def test_failed_metadata_does_not_guess_instrument(self):
        def fail(url, timeout=6):
            raise ValueError("failure token=must-hide")
        result = PublicMarketData(http_get=fail).collect("EVAA", NOW)
        self.assertIsNone(result["price"])
        self.assertTrue(result["errors"])
        self.assertNotIn("must-hide", str(result))

    def test_malformed_success_payload_is_missing_data_not_crash(self):
        result = PublicMarketData(http_get=lambda url, timeout=6: {"code": "00000", "data": "bad shape"}).collect("EVAA", NOW)
        self.assertIsNone(result["price"])
        self.assertIn("SYMBOL_UNAVAILABLE", [x["code"] for x in result["errors"]])

    def test_429_is_retried_but_401_is_not(self):
        calls = []
        def retry(url, timeout=6):
            calls.append(url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 429, "Too many requests", {}, None)
            return []
        with patch("moonshot_scanner.providers.time.sleep"):
            data, source, error = PublicMarketData(http_get=retry)._get("test", "https://api.bitget.com/public")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(error)
        calls.clear()
        def deny(url, timeout=6):
            calls.append(url)
            raise urllib.error.HTTPError(url, 401, "No access", {}, None)
        data, source, error = PublicMarketData(http_get=deny)._get("test", "https://api.bitget.com/public")
        self.assertEqual(len(calls), 1)
        self.assertEqual(error["code"], "HTTP_401")

    def test_unknown_identity_never_makes_network_ticker_search(self):
        def fail(url, timeout=6):
            raise AssertionError("Unknown ID must not trigger symbol lookup")
        result = PublicMarketData(http_get=fail).enrich_identity("TINYNEW", {"price": 1}, NOW)
        self.assertEqual(result["fundamentals"]["status"], "UNKNOWN")
        self.assertEqual(result["onchain"]["liquidity_risk"], "UNKNOWN")

    def test_fundamentals_use_curated_id_and_aggregator_timestamp(self):
        def fake(url, timeout=6):
            if "/coins/markets" in url:
                return [{"id": "solana", "symbol": "sol", "name": "Solana", "market_cap": 1000,
                         "fully_diluted_valuation": 2000, "circulating_supply": 100, "total_supply": 200,
                         "max_supply": None, "last_updated": "2026-11-20T03:00:00Z"}]
            if "/token-pairs/v1/solana/" in url:
                return []
            raise AssertionError(url)
        result = PublicMarketData(http_get=fake).enrich_identity("SOL", {"price": 72}, NOW)
        fundamental = result["fundamentals"]
        self.assertEqual(fundamental["market_cap_usd"], 1000)
        self.assertEqual(fundamental["fdv_mcap_ratio"], 2)
        self.assertIsNone(fundamental["circulating_pct"])
        self.assertEqual(fundamental["venue_scope"], "AGGREGATE_NOT_EXCHANGE_CANDLES")
        self.assertIsNotNone(fundamental["observed_at_ms"])
        self.assertEqual(fundamental["unlocks"]["status"], "UNKNOWN")

    def test_cache_keeps_original_fetch_timestamp(self):
        fake = FakeBitget()
        provider = PublicMarketData(http_get=fake)
        url = "https://api.bitget.com/api/v3/market/tickers?category=SPOT"
        _, first, _ = provider._get("ticker", url, 10)
        _, second, _ = provider._get("ticker", url, 10)
        self.assertEqual(first["fetched_at_ms"], second["fetched_at_ms"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(fake.calls), 1)


if __name__ == "__main__":
    unittest.main()
