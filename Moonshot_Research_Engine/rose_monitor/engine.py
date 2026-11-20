"""Read-only market evidence for a Telegram signal; no execution or price forecast.

Public interface: analyze_signal(signal, *, http_get=None, now_ms=None) -> dict.
An injected http_get must accept (url, timeout=8) and return decoded JSON. Requests
are bounded to eight concurrent workers. A failed endpoint becomes visible evidence,
never a zero, bullish default, or an exception that kills a monitoring daemon.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import math
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

SPOT = "https://data-api.binance.vision"
PERP = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"
INTERVALS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
MAX_SYMBOLS = 8
LIMITS = {"spot_turnover_ratio_min": 3.0, "long_price24h_max_pct": 15.0,
          "spread_max_bps": 20.0, "depth_each_side_min_usdt": 5000.0,
          "oi4h_change_max_pct": 20.0, "funding_abs_max_pct": 0.05,
          "spot_perp_basis_abs_max_pct": 1.0, "ticker_age_max_seconds": 120,
          "book_age_max_seconds": 30, "signal_age_max_seconds": 900,
          "explicit_plan_min_reward_risk": 2.0}


def _utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _pct(new, old):
    return (new / old - 1) * 100 if new is not None and old is not None and old > 0 else None


def _timestamp(value):
    if isinstance(value, (int, float)):
        return int(value if value > 10**11 else value * 1000)
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None  # Do not guess a channel's timezone.
        return int(stamp.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return None


def _http_get(url, timeout=8):
    request = urllib.request.Request(url, headers={"User-Agent": "RoseEvidenceMonitor/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(12_000_001)
        if len(body) > 12_000_000:
            raise ValueError("Response exceeds 12 MB limit")
        return json.loads(body)


def _url(base, path, **params):
    return base + path + ("?" + urllib.parse.urlencode(params) if params else "")


class _Fetch:
    def __init__(self, http_get, executor, deadline=None):
        self.get = http_get or _http_get
        self.executor = executor
        self.deadline = deadline
        self.failures = []
        self.sources = {}

    def batch(self, jobs):
        def run(name, url):
            start = time.monotonic()
            try:
                if self.deadline is not None and start >= self.deadline:
                    raise TimeoutError("Overall 40-second network budget exhausted")
                data = self.get(url, timeout=8)
                if isinstance(data, dict):
                    if data.get("retCode", 0) != 0:
                        raise ValueError("Bybit %s: %s" % (data.get("retCode"), data.get("retMsg")))
                    if isinstance(data.get("code"), (int, float)) and data["code"] < 0:
                        raise ValueError("Binance %s: %s" % (data["code"], data.get("msg")))
                return data, None, round(time.monotonic() - start, 3)
            except Exception as exc:
                return None, "%s: %s" % (type(exc).__name__, str(exc)[:240]), round(time.monotonic() - start, 3)
        pending = {self.executor.submit(run, name, url): (name, url) for name, url in jobs.items()}
        result = {}
        for future in concurrent.futures.as_completed(pending):
            name, url = pending[future]
            data, error, elapsed = future.result()
            result[name] = data
            self.sources[name] = {"url": url, "fetched_utc": _utc(int(time.time() * 1000)), "elapsed_seconds": elapsed}
            if error:
                self.failures.append({"request": name, "url": url, "error": error})
        return result


def _bybit_first(data):
    rows = (data or {}).get("result", {}).get("list", [])
    return rows[0] if rows else None


def _ticker(data, venue):
    row = _bybit_first(data) if venue == "bybit" else data
    if not isinstance(row, dict):
        return None
    price = _number(row.get("lastPrice"))
    if price is None or price <= 0:
        return None
    timestamp = _number((data or {}).get("time") if venue == "bybit" else row.get("closeTime"))
    return {"price": price, "exchange_timestamp_ms": int(timestamp) if timestamp is not None and timestamp > 0 else None,
            "exchange_timestamp_utc": _utc(timestamp) if timestamp is not None and timestamp > 0 else None,
            "turnover24h_usdt": _number(row.get("turnover24h" if venue == "bybit" else "quoteVolume")),
            "change24h_pct": (_number(row.get("price24hPcnt")) * 100 if _number(row.get("price24hPcnt")) is not None else None) if venue == "bybit" else _number(row.get("priceChangePercent")),
            "raw": row}


def _resolve(fetch, symbols):
    jobs = {}
    for sym in symbols:
        for kind, base, path in (("spot", SPOT, "/api/v3/ticker/24hr"), ("perp", PERP, "/fapi/v1/ticker/24hr")):
            jobs[sym + ":bn:" + kind] = _url(base, path, symbol=sym + "USDT")
    initial = fetch.batch(jobs)
    resolved = {}
    for sym in symbols:
        resolved[sym] = {}
        for kind in ("spot", "perp"):
            tick = _ticker(initial.get(sym + ":bn:" + kind), "binance")
            if tick:
                resolved[sym][kind] = {"venue": "binance", "pair": sym + "USDT", "unit_multiplier": 1, "ticker": tick}
    # Contract aliases are accepted only from the exchange's instrument catalogue.
    missing_perp = [s for s in symbols if "perp" not in resolved[s]]
    catalogue = fetch.batch({"bn:exchange_info": _url(PERP, "/fapi/v1/exchangeInfo")}) if missing_perp else {}
    aliases = {}
    for row in (catalogue.get("bn:exchange_info") or {}).get("symbols", []):
        if row.get("quoteAsset") != "USDT" or row.get("status") != "TRADING" or row.get("contractType") != "PERPETUAL":
            continue
        base = row.get("baseAsset", "")
        for sym in missing_perp:
            match = re.fullmatch(r"(10+)" + re.escape(sym), base)
            if match:
                aliases[sym] = (row["symbol"], int(match.group(1)))
    alias_data = fetch.batch({s + ":alias": _url(PERP, "/fapi/v1/ticker/24hr", symbol=a[0]) for s, a in aliases.items()})
    for sym, (pair, multiplier) in aliases.items():
        tick = _ticker(alias_data.get(sym + ":alias"), "binance")
        if tick:
            resolved[sym]["perp"] = {"venue": "binance", "pair": pair, "unit_multiplier": multiplier, "ticker": tick}
    jobs = {}
    for sym in symbols:
        for kind, category in (("spot", "spot"), ("perp", "linear")):
            if kind not in resolved[sym]:
                jobs[sym + ":by:" + kind] = _url(BYBIT, "/v5/market/tickers", category=category, symbol=sym + "USDT")
    fallback = fetch.batch(jobs)
    for sym in symbols:
        for kind in ("spot", "perp"):
            tick = _ticker(fallback.get(sym + ":by:" + kind), "bybit")
            if kind not in resolved[sym] and tick:
                resolved[sym][kind] = {"venue": "bybit", "pair": sym + "USDT", "unit_multiplier": 1, "ticker": tick}
    return resolved


def _candle_url(market, interval, limit=80, **params):
    if market["venue"] == "binance":
        base, path = (SPOT, "/api/v3/klines") if market["kind"] == "spot" else (PERP, "/fapi/v1/klines")
        return _url(base, path, symbol=market["pair"], interval=interval, limit=limit, **params)
    translated = {"startTime": "start", "endTime": "end"}
    return _url(BYBIT, "/v5/market/kline", category="spot" if market["kind"] == "spot" else "linear",
                symbol=market["pair"], interval={"1m": "1", "15m": "15", "1h": "60", "4h": "240"}[interval],
                limit=limit, **{translated.get(k, k): v for k, v in params.items()})


def _candles(data, venue, interval_ms, now_ms, closed=True):
    rows = (data or {}).get("result", {}).get("list", []) if venue == "bybit" else data
    if not isinstance(rows, list):
        return []
    result = {}
    for row in rows:
        try:
            start = int(row[0])
            if closed and start + interval_ms > now_ms:
                continue
            values = [_number(v) for v in row[1:6]]
            quote = _number(row[6] if venue == "bybit" else row[7])
            if len(values) != 5 or any(v is None for v in values) or any(v <= 0 for v in values[:4]) or quote is None or quote < 0:
                continue
            op, hi, lo, close, volume = values
            if hi < max(op, close, lo) or lo > min(op, close, hi) or volume < 0:
                continue
            result[start] = {"open_ms": start, "open": op, "high": hi, "low": lo, "close": close, "volume": volume,
                             "turnover_usdt": quote, "taker_buy_quote": _number(row[10]) if venue == "binance" and len(row) > 10 else None}
        except (ValueError, TypeError, IndexError, OverflowError):
            continue
    return [result[k] for k in sorted(result)]


def _ema(values, length):
    value = values[0]
    alpha = 2 / (length + 1)
    for current in values[1:]:
        value += alpha * (current - value)
    return value


def _timeframe(bars, interval, now_ms):
    step = INTERVALS[interval]
    if len(bars) < 42:
        return {"available": False, "closed_bars": len(bars), "reason": "At least 42 closed candles required"}
    recent = bars[-42:]
    contiguous = all(b["open_ms"] - a["open_ms"] == step for a, b in zip(recent, recent[1:]))
    expected = (now_ms // step - 1) * step
    fresh = bars[-1]["open_ms"] == expected
    prices = [b["close"] for b in bars]
    ema13, ema21 = _ema(prices, 13), _ema(prices, 21)
    last, previous = bars[-1], bars[-2]
    trend = "bullish" if last["close"] > ema13 > ema21 else "bearish" if last["close"] < ema13 < ema21 else "mixed"
    breakout = "long" if last["close"] > max(b["high"] for b in bars[-5:-1]) else "short" if last["close"] < min(b["low"] for b in bars[-5:-1]) else None
    return {"available": True, "closed_bars": len(bars), "fresh": fresh, "contiguous_last42": contiguous,
            "last_closed_open_utc": _utc(last["open_ms"]), "last_closed_end_utc": _utc(last["open_ms"] + step),
            "close": last["close"], "ema13": ema13, "ema21": ema21, "trend": trend,
            "closed_change_pct": _pct(last["close"], previous["close"]), "breakout_prior4_closed_bars": breakout}


def _turnover(bars):
    if len(bars) < 43:
        return {"available": False, "reason": "Need latest closed 4h bar plus 42 prior closed bars"}
    sample = bars[-43:]
    contiguous = all(b["open_ms"] - a["open_ms"] == INTERVALS["4h"] for a, b in zip(sample, sample[1:]))
    baseline = statistics.median(b["turnover_usdt"] for b in sample[:-1])
    latest = sample[-1]
    ratio = latest["turnover_usdt"] / baseline if baseline > 0 else None
    return {"available": contiguous and ratio is not None, "prior_bars": 42, "baseline_days": 7,
            "contiguous": contiguous, "latest_closed_turnover_usdt": latest["turnover_usdt"],
            "prior7d_median4h_usdt": baseline, "ratio": ratio,
            "meets_3x": contiguous and ratio is not None and ratio >= 3,
            "latest_taker_buy_share": latest["taker_buy_quote"] / latest["turnover_usdt"] if latest["taker_buy_quote"] is not None and latest["turnover_usdt"] > 0 else None}


def _book(data, venue, now_ms):
    raw = (data or {}).get("result", {}) if venue == "bybit" else (data or {})
    try:
        bids = [(float(p), float(q)) for p, q, *_ in raw.get("b" if venue == "bybit" else "bids", []) if float(p) > 0 and float(q) > 0]
        asks = [(float(p), float(q)) for p, q, *_ in raw.get("a" if venue == "bybit" else "asks", []) if float(p) > 0 and float(q) > 0]
        if any(not math.isfinite(p) or not math.isfinite(q) for p, q in bids + asks):
            return {"available": False, "reason": "Non-finite order-book response"}
        if not bids or not asks:
            return {"available": False, "reason": "Order book unavailable"}
        bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)
        if ask <= bid:
            return {"available": False, "reason": "Crossed or invalid order book"}
        mid = (bid + ask) / 2
        stamp = _number(raw.get("ts", raw.get("T")))
        age = (now_ms - stamp) / 1000 if stamp is not None else None
        bid_depth = sum(p * q for p, q in bids if mid * .995 <= p <= bid)
        ask_depth = sum(p * q for p, q in asks if ask <= p <= mid * 1.005)
        # The fixed-size snapshot may end before the requested 0.5% boundary.
        return {"available": True, "snapshot_utc": _utc(stamp) if stamp is not None else None,
                "exchange_timestamp_available": stamp is not None, "age_seconds": age,
                "fresh": age is not None and -5 <= age <= LIMITS["book_age_max_seconds"], "best_bid": bid, "best_ask": ask,
                "spread_bps": (ask - bid) / mid * 10_000, "depth_band_pct": .5,
                "bid_depth_usdt": bid_depth, "ask_depth_usdt": ask_depth,
                "bid_band_complete": min(p for p, _ in bids) <= mid * .995,
                "ask_band_complete": max(p for p, _ in asks) >= mid * 1.005,
                "note": "Visible snapshot depth; orders can be cancelled and execution is not guaranteed"}
    except (ValueError, TypeError, KeyError, OverflowError):
        return {"available": False, "reason": "Invalid order-book response"}


def _oi(data, venue, multiplier, now_ms):
    raw = (data or {}).get("result", {}).get("list", []) if venue == "bybit" else data
    rows = {}
    for row in raw if isinstance(raw, list) else []:
        try:
            stamp = int(row["timestamp"])
            value = _number(row.get("openInterest" if venue == "bybit" else "sumOpenInterest"))
            if value is not None and value > 0 and stamp <= now_ms:
                rows[stamp] = value * multiplier
        except (ValueError, TypeError, KeyError, OverflowError):
            pass
    if not rows:
        return {"available": False, "reason": "Token-unit OI history unavailable"}
    last = max(rows)
    out = {"available": True, "timestamp_utc": _utc(last), "age_seconds": (now_ms - last) / 1000,
           "token_units": rows[last], "fresh": now_ms - last <= 90 * 60_000,
           "note": "Open interest counts open long and short contracts; growth does not establish buying direction"}
    for hours in (4, 24):
        target = last - hours * INTERVALS["1h"]
        # Exact hourly matching avoids silently measuring 3h/23h after missing data.
        out["change%sh_pct" % hours] = _pct(rows[last], rows.get(target))
        out["baseline%sh_utc" % hours] = _utc(target) if target in rows else None
    return out


def _funding(data, instruments, market):
    if market["venue"] == "bybit":
        row = market["ticker"]["raw"]
        info = _bybit_first(instruments) or {}
        interval = _number(info.get("fundingInterval"))
        return {"rate_pct": _number(row.get("fundingRate")) * 100 if _number(row.get("fundingRate")) is not None else None,
                "interval_hours": interval / 60 if interval is not None else None,
                "next_funding_utc": _utc(int(row["nextFundingTime"])) if str(row.get("nextFundingTime", "")).isdigit() else None,
                "rate_type": "Exchange current displayed funding rate, subject to change"}
    row = data or {}
    match = next((r for r in instruments if r.get("symbol") == market["pair"]), {}) if isinstance(instruments, list) else {}
    interval = _number(match.get("fundingIntervalHours"))
    return {"rate_pct": _number(row.get("lastFundingRate")) * 100 if _number(row.get("lastFundingRate")) is not None else None,
            "interval_hours": interval, "interval_note": None if interval is not None else "Not returned by fundingInfo; no default interval assumed",
            "next_funding_utc": _utc(int(row["nextFundingTime"])) if str(row.get("nextFundingTime", "")).isdigit() else None,
            "rate_type": "Binance premiumIndex lastFundingRate; displayed rate is not a guaranteed future charge"}


def _explicit_plan(text, direction, multiplier=1):
    # Only labelled numeric levels; numbered list labels, leverage and percentages
    # cannot become an entry. Channel prose remains data throughout this module.
    token = r"\$?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?|\.[0-9]+)(?![\w.%])"
    ambiguous = []
    def find(label):
        match = re.search(r"(?im)(?<!\w)(?:" + label + r")\s*(?:price\s*)?[:=@]\s*" + token, text)
        if not match:
            return None
        tail = text[match.end():].split("\n", 1)[0]
        if re.match(r"\s*(?:[-–—~/,]|to\b|and\b|or\b)", tail, re.I):
            ambiguous.append(match.group(0))
            return None
        return _number(match.group(1).replace(",", ""))
    entry = find(r"entry|buy|sell")
    stop = find(r"sl|stop(?:[ -]?loss)?")
    targets = []
    for match in re.finditer(r"(?im)(?<!\w)(?:tp\s*[1-9]?|target\s*[1-9]?)\s*[:=@]\s*" + token, text):
        tail = text[match.end():].split("\n", 1)[0]
        if re.match(r"\s*(?:[-–—~/,]|to\b|and\b|or\b)", tail, re.I):
            ambiguous.append(match.group(0))
            continue
        value = _number(match.group(1).replace(",", ""))
        if value is not None and value > 0 and value not in targets:
            targets.append(value)
    entry = entry if entry is not None and entry > 0 else None
    stop = stop if stop is not None and stop > 0 else None
    valid = bool(entry and stop and direction in ("long", "short") and (stop < entry if direction == "long" else stop > entry))
    reward = [(target - entry if direction == "long" else entry - target) / abs(entry - stop) if valid else None for target in targets]
    return {"entry": entry, "stop": stop, "targets": targets, "target_reward_risk": reward,
            "directional_stop_valid": valid, "levels_are_explicit_channel_text": True,
            "ambiguous_levels": ambiguous,
            "note": "Missing, ranged, shorthand or image levels require review. Units must match the posted instrument; no order is placed."}


def _structural(bars, price, direction):
    if len(bars) < 20 or price is None:
        return {"available": False}
    # Local pivots formed by closed bars on both sides; recent extreme is fallback.
    sample = bars[-40:]
    lows = [b["low"] for i, b in enumerate(sample[1:-1], 1) if b["low"] < sample[i-1]["low"] and b["low"] <= sample[i+1]["low"] and b["low"] < price]
    highs = [b["high"] for i, b in enumerate(sample[1:-1], 1) if b["high"] > sample[i-1]["high"] and b["high"] >= sample[i+1]["high"] and b["high"] > price]
    support, resistance = max(lows, default=None), min(highs, default=None)
    rr = (resistance - price) / (price - support) if direction == "long" and support and resistance else (price - support) / (resistance - price) if direction == "short" and support and resistance else None
    return {"available": True, "timeframe": "15m", "nearest_closed_pivot_below": support,
            "nearest_closed_pivot_above": resistance, "conditional_reward_risk_between_pivots": rr,
            "entry_reference": price, "note": "Observed pivots only; levels are not proposed stops, targets or execution instructions"}


def _analyze_signal(signal: dict, *, http_get=None, now_ms=None) -> dict:
    """Analyze text metadata with anonymous APIs; always return serializable evidence.

    WAIT = incomplete/unresolved evidence or unsafe chase; WATCH = complete evidence
    without all confirmation gates; CONFIRMED = technical/spot gates only. These
    labels are not win probabilities, investment recommendations or trade orders.
    """
    start = time.monotonic()
    live_clock = now_ms is None
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    published = _timestamp(signal.get("published_utc", signal.get("timestamp")))
    max_age = _number(signal.get("max_age_seconds", LIMITS["signal_age_max_seconds"]))
    max_age = max_age if max_age is not None and max_age > 0 else LIMITS["signal_age_max_seconds"]
    signal_age = (now_ms - published) / 1000 if published is not None else None
    direction = signal.get("direction") if signal.get("direction") in ("long", "short") else None
    symbols, rejected = [], []
    for raw in signal.get("symbols", []) if isinstance(signal.get("symbols", []), list) else []:
        symbol = str(raw).upper().strip().lstrip("$#")
        symbol = symbol[:-4] if symbol.endswith("USDT") else symbol
        if re.fullmatch(r"[A-Z0-9]{2,20}", symbol):
            if symbol not in symbols:
                symbols.append(symbol)
        else:
            rejected.append(str(raw)[:80])
    omitted = symbols[MAX_SYMBOLS:]
    symbols = symbols[:MAX_SYMBOLS]
    output = {"schema_version": 1, "post_id": signal.get("post_id", signal.get("message_id")), "signal_url": signal.get("url"),
              "signal_published_utc": _utc(published) if published is not None else None,
              "analyzed_utc": _utc(now_ms), "direction": direction, "status": "WAIT", "candidates": [],
              "signal_age_seconds": signal_age, "signal_max_age_seconds": max_age,
              "rejected_symbols": rejected, "omitted_symbols": omitted, "limits": dict(LIMITS),
              "network_budget": {"per_request_timeout_seconds": 8, "max_concurrent": 8, "new_request_budget_seconds": 40, "note": "Queued requests stop at budget; in-flight requests can finish up to 8 seconds later"},
              "probability": None, "probability_note": "No validated calibrated prediction model; no percentage of future success is estimated",
              "scope": "Public technical/liquidity evidence. Catalysts, token identity, unlocks, manipulation and news are not verified here.",
              "request_failures": [], "sources": {}}
    if not symbols:
        output["reasons"] = ["No explicit valid symbol; chart/image or text review required"]
        output["analysis_seconds"] = round(time.monotonic() - start, 3)
        return output
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        fetch = _Fetch(http_get, pool, deadline=start + 40)
        markets = _resolve(fetch, symbols + [s for s in ("BTC", "ETH") if s not in symbols])
        jobs, contexts = {}, {}
        for sym, resolved in markets.items():
            for kind, market in resolved.items():
                market["kind"] = kind
            chart = resolved.get("spot")
            if chart:
                frames = ("4h",) if sym not in symbols else INTERVALS
                for interval in frames:
                    jobs[sym + ":" + interval] = _candle_url(chart, interval)
            if sym not in symbols:
                continue
            perp, spot = resolved.get("perp"), resolved.get("spot")
            context = {"spot": spot, "perp": perp, "chart": chart}
            contexts[sym] = context
            if spot and published is not None and published <= now_ms:
                minute = (published // 60_000 + 1) * 60_000
                context["benchmark_ms"] = minute
                if minute <= now_ms:
                    jobs[sym + ":post_minute"] = _candle_url(spot, "1m", limit=1, startTime=minute, endTime=minute + 59_999)
            book_market = perp or spot
            if book_market:
                if book_market["venue"] == "binance":
                    jobs[sym + ":book"] = _url(PERP if perp else SPOT, "/fapi/v1/depth" if perp else "/api/v3/depth", symbol=book_market["pair"], limit=100)
                else:
                    jobs[sym + ":book"] = _url(BYBIT, "/v5/market/orderbook", category="linear" if perp else "spot", symbol=book_market["pair"], limit=200)
            if perp:
                if perp["venue"] == "binance":
                    jobs[sym + ":oi"] = _url(PERP, "/futures/data/openInterestHist", symbol=perp["pair"], period="1h", limit=30)
                    jobs[sym + ":funding"] = _url(PERP, "/fapi/v1/premiumIndex", symbol=perp["pair"])
                    jobs["bn:funding_info"] = _url(PERP, "/fapi/v1/fundingInfo")
                else:
                    jobs[sym + ":oi"] = _url(BYBIT, "/v5/market/open-interest", category="linear", symbol=perp["pair"], intervalTime="1h", limit=30)
                    jobs[sym + ":instruments"] = _url(BYBIT, "/v5/market/instruments-info", category="linear", symbol=perp["pair"])
        data = fetch.batch(jobs)
        # Candle completeness uses the start-of-analysis cutoff. Advancing that
        # cutoff after slow I/O could incorrectly call a partially fetched candle
        # closed at an interval boundary. Snapshot ages use completion time.
        evidence_ms = int(time.time() * 1000) if live_clock else now_ms
        output["candle_cutoff_utc"] = _utc(now_ms)
        output["analyzed_utc"] = _utc(evidence_ms)
        signal_age = (evidence_ms - published) / 1000 if published is not None else None
        output["signal_age_seconds"] = signal_age
        regimes = {}
        for sym in ("BTC", "ETH"):
            spot = markets.get(sym, {}).get("spot")
            bars = _candles(data.get(sym + ":4h"), spot["venue"], INTERVALS["4h"], now_ms) if spot else []
            regimes[sym] = _timeframe(bars, "4h", now_ms)
        valid_macro = all(r.get("available") and r.get("fresh") and r.get("contiguous_last42") for r in regimes.values())
        trends = [r.get("trend") for r in regimes.values()]
        regime = "bullish" if valid_macro and trends == ["bullish", "bullish"] else "bearish" if valid_macro and trends == ["bearish", "bearish"] else "mixed" if valid_macro else "unknown"
        output["btc_eth_regime"] = {"regime": regime, "benchmarks": regimes, "note": "4h closed structure, not an independence/correlation claim"}
        for sym in symbols:
            ctx = contexts[sym]
            spot, perp, chart = ctx["spot"], ctx["perp"], ctx["chart"]
            candidate = {"symbol": sym, "status": "WAIT", "reasons": [], "market_resolution": {}, "timeframes": {}}
            for kind, market in (("spot", spot), ("perpetual", perp)):
                candidate["market_resolution"][kind] = {k: market[k] for k in ("venue", "pair", "unit_multiplier")} if market else None
                candidate[kind] = {k: v for k, v in market["ticker"].items() if k != "raw"} if market else None
                if candidate[kind]:
                    stamp = candidate[kind].get("exchange_timestamp_ms")
                    age = (evidence_ms - stamp) / 1000 if stamp is not None else None
                    candidate[kind]["age_seconds"] = age
                    candidate[kind]["fresh"] = age is not None and -5 <= age <= LIMITS["ticker_age_max_seconds"]
            bars_by_tf = {}
            for interval in INTERVALS:
                bars = _candles(data.get(sym + ":" + interval), chart["venue"], INTERVALS[interval], now_ms) if chart else []
                bars_by_tf[interval] = bars
                candidate["timeframes"][interval] = _timeframe(bars, interval, now_ms)
            candidate["spot_turnover4h"] = _turnover(bars_by_tf["4h"])
            book_market = perp or spot
            candidate["order_book"] = _book(data.get(sym + ":book"), book_market["venue"], evidence_ms) if book_market else {"available": False}
            candidate["order_book"]["market"] = "perpetual" if perp else "spot"
            candidate["open_interest"] = _oi(data.get(sym + ":oi"), perp["venue"], perp["unit_multiplier"], evidence_ms) if perp else {"available": False}
            candidate["funding"] = _funding(data.get(sym + ":funding"), data.get("bn:funding_info") if perp and perp["venue"] == "binance" else data.get(sym + ":instruments"), perp) if perp else None
            price = spot["ticker"]["price"] if spot else None
            normalized_perp = perp["ticker"]["price"] / perp["unit_multiplier"] if perp else None
            candidate["spot_perp_basis_pct"] = _pct(normalized_perp, price)
            candidate["publication_benchmark"] = {"price": None, "method": "Open of first full 1-minute spot candle strictly after publication; an executable fill is not assumed"}
            minute = ctx.get("benchmark_ms")
            if minute is not None:
                benchmark_bars = _candles(data.get(sym + ":post_minute"), spot["venue"], 60_000, now_ms, closed=False)
                minute_bar = next((b for b in benchmark_bars if b["open_ms"] == minute), None)
                candidate["publication_benchmark"].update({"requested_utc": _utc(minute), "price": minute_bar["open"] if minute_bar else None,
                    "change_to_latest_spot_pct": _pct(price, minute_bar["open"]) if minute_bar else None,
                    "available": minute_bar is not None})
            candidate["explicit_plan"] = _explicit_plan(str(signal.get("text", "")), direction)
            if len(symbols) > 1:
                candidate["explicit_plan"].update({"entry": None, "stop": None, "targets": [], "target_reward_risk": [], "directional_stop_valid": False,
                                                   "ambiguous_levels": ["Multi-symbol post: cannot assign one textual plan to a token"]})
            candidate["structure"] = _structural(bars_by_tf["15m"], price, direction)
            frames = candidate["timeframes"]
            complete = all(f.get("available") and f.get("fresh") and f.get("contiguous_last42") for f in frames.values())
            oi, book, funding = candidate["open_interest"], candidate["order_book"], candidate["funding"] or {}
            trend = "bullish" if direction == "long" else "bearish" if direction == "short" else None
            change24 = spot["ticker"]["change24h_pct"] if spot else None
            plan = candidate["explicit_plan"]
            plan_pass = plan["directional_stop_valid"] and not plan["ambiguous_levels"] and any(rr is not None and rr >= LIMITS["explicit_plan_min_reward_risk"] for rr in plan["target_reward_risk"])
            gates = {"explicit_direction": direction is not None,
                     "spot_and_perpetual_resolved": bool(spot and perp), "closed_candles_complete_and_fresh": complete,
                     "spot_and_perpetual_tickers_fresh": bool(candidate["spot"] and candidate["perpetual"] and candidate["spot"]["fresh"] and candidate["perpetual"]["fresh"]),
                     "all_timeframes_aligned": bool(trend and complete and all(f["trend"] == trend for f in frames.values())),
                     "15m_closed_breakout": frames["15m"].get("breakout_prior4_closed_bars") == direction if direction else False,
                     "spot4h_turnover_ge_3x": candidate["spot_turnover4h"].get("meets_3x", False),
                     "btc_eth_supportive": regime == trend if trend else False,
                     "not_chasing_24h": change24 is not None and (change24 < LIMITS["long_price24h_max_pct"] if direction == "long" else change24 > -LIMITS["long_price24h_max_pct"] if direction == "short" else False),
                     "book_liquid_snapshot": book.get("available", False) and book.get("fresh", False) and book.get("spread_bps", math.inf) <= LIMITS["spread_max_bps"] and min(book.get("bid_depth_usdt", 0), book.get("ask_depth_usdt", 0)) >= LIMITS["depth_each_side_min_usdt"],
                     "oi_available_not_overheated": oi.get("fresh", False) and oi.get("change4h_pct") is not None and oi["change4h_pct"] <= LIMITS["oi4h_change_max_pct"] and oi.get("change24h_pct") is not None,
                     "funding_known_not_extreme": funding.get("rate_pct") is not None and abs(funding["rate_pct"]) <= LIMITS["funding_abs_max_pct"] and funding.get("interval_hours") is not None,
                     "spot_perp_prices_consistent": candidate["spot_perp_basis_pct"] is not None and abs(candidate["spot_perp_basis_pct"]) <= LIMITS["spot_perp_basis_abs_max_pct"],
                     "signal_readable": not signal.get("image_only", False),
                     "valid_publication_timestamp": published is not None and published <= now_ms,
                     "signal_is_recent": signal_age is not None and 0 <= signal_age <= max_age,
                     "explicit_risk_plan_ge_2r": bool(plan_pass)}
            candidate["confirmation_gates"] = gates
            hard_missing = not (gates["spot_and_perpetual_resolved"] and complete and gates["explicit_direction"] and gates["signal_readable"] and gates["valid_publication_timestamp"] and gates["signal_is_recent"] and gates["spot_and_perpetual_tickers_fresh"])
            candidate["status"] = "CONFIRMED" if all(gates.values()) else "WAIT" if hard_missing or not gates["not_chasing_24h"] else "WATCH"
            candidate["reasons"] = [name for name, passed in gates.items() if not passed]
            candidate["status_scope"] = "Technical candidate confirmation only; not a forecast, permission to trade, or full fundamental review"
            output["candidates"].append(candidate)
        output["request_failures"] = fetch.failures
        output["sources"] = fetch.sources
    statuses = [c["status"] for c in output["candidates"]]
    output["status"] = "CONFIRMED" if "CONFIRMED" in statuses else "WATCH" if "WATCH" in statuses else "WAIT"
    output["analysis_seconds"] = round(time.monotonic() - start, 3)
    return output


def analyze_signal(signal: dict, *, http_get=None, now_ms=None) -> dict:
    """Return read-only evidence; unexpected schema/calculation errors become WAIT.

    http_get(url, timeout=8) is injectable for offline tests. now_ms freezes all
    timestamps for deterministic tests. No credentials or private APIs are used.
    """
    try:
        return _analyze_signal(signal, http_get=http_get, now_ms=now_ms)
    except Exception as exc:
        stamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return {"schema_version": 1, "post_id": signal.get("post_id", signal.get("message_id")) if isinstance(signal, dict) else None,
                "analyzed_utc": _utc(stamp), "status": "WAIT", "candidates": [], "probability": None,
                "reasons": ["Analysis failed safely; malformed or unexpected evidence requires review"],
                "request_failures": [], "analysis_error": "%s: %s" % (type(exc).__name__, str(exc)[:240])}


def format_analysis(result: dict, max_symbols=3) -> str:
    """Compact plain text suitable for a delivery adapter; no order instructions."""
    label = "TECHNICAL CHECKS PASSED" if result.get("status") == "CONFIRMED" else result.get("status", "WAIT")
    lines = ["Rose signal %s | %s" % (result.get("post_id", "?"), label),
             "Analyzed %s | BTC/ETH %s" % (result.get("analyzed_utc", "?"), result.get("btc_eth_regime", {}).get("regime", "unknown"))]
    for coin in result.get("candidates", [])[:max_symbols]:
        spot = coin.get("spot") or {}
        lines.append("%s %s | spot %s | 24h %s%%" % (coin["symbol"], coin["status"], _fmt(spot.get("price")), _fmt(spot.get("change24h_pct"))))
        frames = coin.get("timeframes", {})
        lines.append("Closed trend 4h/1h/15m: " + "/".join(frames.get(t, {}).get("trend", "unknown") for t in ("4h", "1h", "15m")))
        lines.append("Spot 4h turnover: %sx prior7d median | OI 4h: %s%% | funding: %s%%/%sh" % (_fmt(coin.get("spot_turnover4h", {}).get("ratio")), _fmt(coin.get("open_interest", {}).get("change4h_pct")), _fmt((coin.get("funding") or {}).get("rate_pct")), _fmt((coin.get("funding") or {}).get("interval_hours"))))
        lines.append("Unmet checks: " + (", ".join(coin.get("reasons", [])) or "none for technical checks"))
    if result.get("request_failures"):
        lines.append("Public-data failures: %s (see saved evidence)" % len(result["request_failures"]))
    if not result.get("candidates"):
        lines.extend(result.get("reasons", []))
    lines.append("No calibrated surge probability. No trade placed. News/unlocks/token identity require review.")
    return "\n".join(lines)


def _fmt(value):
    return "unknown" if value is None else format(value, ".6g")
