"""Bounded, anonymous market-data adapters; no account or execution methods.

PublicMarketData.collect returns one venue/product's candles, not a blended
history. A missing or short history is evidence, never a fatal minimum-length
exception. Exchange event time, server response time and local fetch time are
kept separate. CoinGecko project IDs and DEX contracts come only from the
reviewed identity registry, never a ticker-search result.

An injected http_get accepts (url, timeout=6) and returns decoded JSON or an
object with .json()/.raise_for_status(). The default transport has no credentials
and a 4 MB response ceiling. Every endpoint used here is public and read-only.
"""
from __future__ import annotations

import concurrent.futures
import copy
import datetime as dt
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .identity import aggregate_dex_pairs, get_identity, registry_ids

INTERVALS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
BITGET = "https://api.bitget.com"
BINANCE = "https://data-api.binance.vision"
BINANCE_PERP = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"
COINGECKO = "https://api.coingecko.com/api/v3"
DEXSCREENER = "https://api.dexscreener.com"
_ALLOWED_HOSTS = {urllib.parse.urlsplit(x).netloc for x in (BITGET, BINANCE, BINANCE_PERP, BYBIT, COINGECKO, DEXSCREENER)}
_HOST_LOCK = threading.Lock()
_HOST_NEXT = {}
_HOST_GAPS = {"api.bitget.com": .07, "api.coingecko.com": 2.2, "api.dexscreener.com": .22,
              "api.bybit.com": .08, "data-api.binance.vision": .08, "fapi.binance.com": .08}


def number(value):
    if isinstance(value,bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError, OverflowError):
        return None


def utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms(value):
    n = number(value)
    return int(n) if n is not None and n > 0 else None


def clean_symbol(symbol):
    s = str(symbol).upper().strip().replace("/", "").replace("-", "")
    if not re.fullmatch(r"[A-Z0-9]{2,36}", s):
        raise ValueError("Symbol must contain 2-36 alphanumeric characters")
    return s if s.endswith("USDT") else s + "USDT"


def _url(base, path, **params):
    return base + path + ("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}) if params else "")


def _redact(text):
    return re.sub(r"(?i)(token|api[_-]?key|secret|password|authorization)([=:\s]+)[^&\s,;]+", r"\1\2[REDACTED]", str(text))[:240]


def _throttle(url):
    host = urllib.parse.urlsplit(url).netloc
    if host not in _ALLOWED_HOSTS:
        raise ValueError("Host is not an approved public-data endpoint")
    with _HOST_LOCK:
        now = time.monotonic()
        delay = max(0.0, _HOST_NEXT.get(host, 0) - now)
        _HOST_NEXT[host] = now + delay + _HOST_GAPS.get(host, .1)
    if delay:
        time.sleep(delay)


def _http_get(url, timeout=6):
    _throttle(url)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "MoonshotResearch/3.0 public-read-only"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError("Response exceeds 4 MB public-data limit")
        return json.loads(raw)


def _validate_envelope(data):
    if not isinstance(data, (dict, list)):
        raise ValueError("Public endpoint returned an invalid JSON root")
    if not isinstance(data, dict):
        return
    if "code" in data and str(data["code"]) not in ("00000", "0", "200"):
        raise ValueError("Exchange response code %s: %s" % (data["code"], _redact(data.get("msg", ""))))
    if "retCode" in data and data["retCode"] != 0:
        raise ValueError("Exchange response code %s: %s" % (data["retCode"], _redact(data.get("retMsg", ""))))
    if data.get("error"):
        raise ValueError("Public API error: " + _redact(data["error"]))
    if isinstance(data.get("status"), dict) and data["status"].get("error_code"):
        raise ValueError("Public API error code " + str(data["status"]["error_code"]))


def normalize_candles(rows, timeframe, now_ms, *, exchange="bitget", product="SPOT"):
    """Normalize raw klines, preserving partial latest candle and its close flag.

    Bad OHLC, nonfinite prices, negative volumes and future start times are
    rejected. A missing interval stays a visible gap rather than an interpolated
    candle. close_time is exclusive for every venue.
    """
    duration = INTERVALS[timeframe]
    valid = {}
    for raw in rows or []:
        try:
            if isinstance(raw, dict):
                start = _ms(raw.get("open_time", raw.get("time")))
                o, h, l, c = [number(raw.get(k)) for k in ("open", "high", "low", "close")]
                bv = number(raw.get("base_volume", raw.get("volume")))
                qv = number(raw.get("quote_turnover"))
            else:
                start = _ms(raw[0])
                o, h, l, c = [number(x) for x in raw[1:5]]
                bv = number(raw[5])
                qv = number(raw[7] if exchange == "binance" else raw[6])
            if start is None or start > now_ms or start % duration != 0 or any(x is None or x <= 0 for x in (o, h, l, c)):
                continue
            if h < max(o, c) or l > min(o, c) or h < l or bv is None or bv < 0 or qv is None or qv < 0:
                continue
            close = start + duration
            valid[start] = {"open_time": start, "close_time": close, "open": o, "high": h, "low": l,
                            "close": c, "base_volume": bv, "quote_turnover": qv,
                            "is_closed": close <= now_ms, "exchange": exchange, "product": product,
                            "timeframe": timeframe, "boundary_timezone": "UTC"}
        except (IndexError, TypeError, ValueError):
            continue
    return [valid[key] for key in sorted(valid)]


def _book_metrics(raw, *, observed_at_ms=None, reference_notional_usd=1000):
    raw = raw or {}
    bids = raw.get("b", raw.get("bids", []))
    asks = raw.get("a", raw.get("asks", []))
    def levels(items, reverse):
        answer = []
        for row in items or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price, qty = number(row[0]), number(row[1])
            if price is not None and qty is not None and price > 0 and qty > 0:
                answer.append((price, qty))
        return sorted(answer, reverse=reverse)
    bids, asks = levels(bids, True), levels(asks, False)
    empty = {"status": "UNKNOWN", "spread_bps": None, "bid": None, "ask": None,
             "observed_at_ms": observed_at_ms, "reference_notional_usd": reference_notional_usd,
             "buy_slippage_bps_from_mid": None, "sell_slippage_bps_from_mid": None,
             "buy_fill_fraction": None, "sell_fill_fraction": None,
             "bid_depth_50bps_usd": None, "ask_depth_50bps_usd": None,
             "imbalance_50bps": None, "levels_available": {"bids": len(bids), "asks": len(asks)}}
    if number(reference_notional_usd) is None or reference_notional_usd <= 0:
        empty["reason"] = "Reference notional must be finite and positive"
        return empty
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        empty["reason"] = "Missing, invalid or crossed public order book"
        return empty
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2
    bid_depth = sum(p * q for p, q in bids if p >= mid * .995)
    ask_depth = sum(p * q for p, q in asks if p <= mid * 1.005)
    def walk(items):
        target_units = reference_notional_usd / mid
        remaining = target_units
        spent = units = 0.0
        for price, qty in items:
            take = min(remaining, qty)
            spent += price * take
            units += take
            remaining -= take
            if remaining <= 1e-8:
                break
        return spent / units if units else None, min(1.0, units / target_units)
    buy, buy_fill = walk(asks)
    sell, sell_fill = walk(bids)
    empty.update({"status": "OBSERVED_BOOK", "bid": bid, "ask": ask, "mid": mid,
                  "spread_bps": (ask - bid) / mid * 10_000,
                  "bid_depth_50bps_usd": bid_depth, "ask_depth_50bps_usd": ask_depth,
                  "imbalance_50bps": (bid_depth - ask_depth) / (bid_depth + ask_depth) if bid_depth + ask_depth else None,
                  "buy_fill_fraction": buy_fill, "sell_fill_fraction": sell_fill,
                  "buy_slippage_bps_from_mid": (buy / mid - 1) * 10_000 if buy is not None and buy_fill >= 1 else None,
                  "sell_slippage_bps_from_mid": (1 - sell / mid) * 10_000 if sell is not None and sell_fill >= 1 else None,
                  "reason": "Indicative visible-depth snapshot; hidden orders and future execution are not known"})
    return empty


def _flow_metrics(rows, venue):
    buys = sells = 0.0
    stamps, seen = [], set()
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        trade_id = r.get("execId", r.get("a", r.get("i")))
        if trade_id is not None and not isinstance(trade_id, (str, int, float)):
            continue
        if trade_id is not None and trade_id in seen:
            continue
        if trade_id is not None:
            seen.add(trade_id)
        if venue == "binance":
            price, qty, stamp = number(r.get("p")), number(r.get("q")), _ms(r.get("T"))
            side = "sell" if r.get("m") is True else "buy" if r.get("m") is False else None
        elif venue == "bybit":
            price, qty, stamp = number(r.get("price")), number(r.get("size")), _ms(r.get("time"))
            side = str(r.get("side", "")).lower()
        else:
            price, qty, stamp = number(r.get("price")), number(r.get("size")), _ms(r.get("ts"))
            side = str(r.get("side", "")).lower()
        if price is None or qty is None or price <= 0 or qty <= 0 or stamp is None or side not in ("buy", "sell"):
            continue
        if side == "buy":
            buys += price * qty
        else:
            sells += price * qty
        stamps.append(stamp)
    total = buys + sells
    return {"status": "RECENT_SAMPLE" if total else "UNKNOWN", "sample_count": len(stamps),
            "buy_notional_usd": buys if total else None, "sell_notional_usd": sells if total else None,
            "taker_buy_ratio": buys / total if total else None,
            "imbalance": (buys - sells) / total if total else None,
            "window_start_ms": min(stamps) if stamps else None, "window_end_ms": max(stamps) if stamps else None,
            "sample_window_seconds": (max(stamps) - min(stamps)) / 1000 if len(stamps) > 1 else None,
            "cvd": None, "note": "Recent API trade sample, not full-market or persistent cumulative volume delta"}


class PublicMarketData:
    def __init__(self, exchange="bitget", http_get=None, *, include_rwa=False, max_workers=4, max_attempts=2, timeout=6):
        exchange = str(exchange).lower()
        if exchange not in {"bitget", "binance", "bybit"}:
            raise ValueError("Supported public venues: bitget, binance, bybit")
        self.exchange = exchange
        self.http_get = http_get or _http_get
        self.include_rwa = include_rwa
        self.max_workers = max(1, min(4, int(max_workers)))
        self.max_attempts = max(1, min(2, int(max_attempts)))
        self.timeout = max(1.0, min(8.0, float(timeout)))
        self._cache = {}
        self._lock = threading.Lock()
        self._fund_lock = threading.Lock()
        self.last_discovery = {"status": "NOT_RUN", "sources": [], "errors": []}

    def _get(self, name, url, ttl=0):
        with self._lock:
            cached = self._cache.get(url)
            if cached and time.monotonic() - cached[0] < ttl:
                data, source = copy.deepcopy(cached[1:])
                source["cached"] = True
                source["cache_age_seconds"] = round(time.monotonic() - cached[0], 3)
                return data, source, None
        source = {"name": name, "url": url, "status": "ERROR", "observed_at": None,
                  "observed_at_ms": None, "timestamp_kind": "UNKNOWN", "fetched_at": None,
                  "fetched_at_ms": None, "server_response_time_ms": None, "cached": False}
        start = time.monotonic()
        error = None
        for attempt in range(self.max_attempts):
            try:
                data = self.http_get(url, timeout=self.timeout)
                if hasattr(data, "raise_for_status"):
                    data.raise_for_status()
                    data = data.json()
                _validate_envelope(data)
                fetched = int(time.time() * 1000)
                source.update({"status": "OK", "fetched_at": utc(fetched), "fetched_at_ms": fetched,
                               "server_response_time_ms": _ms(data.get("requestTime", data.get("time"))) if isinstance(data, dict) else None,
                               "elapsed_seconds": round(time.monotonic() - start, 3), "attempts": attempt + 1})
                if ttl:
                    with self._lock:
                        if len(self._cache) > 1024:
                            self._cache.clear()
                        self._cache[url] = (time.monotonic(), copy.deepcopy(data), copy.deepcopy(source))
                return data, source, None
            except Exception as exc:
                status = getattr(exc, "code", None)
                if status is None:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                error = {"source": name, "code": "HTTP_" + str(status) if status else type(exc).__name__,
                         "message": _redact(exc), "url": url}
                retryable = status == 429 or (isinstance(status, int) and status >= 500) or (status is None and isinstance(exc, (TimeoutError, urllib.error.URLError)))
                if not retryable or attempt + 1 >= self.max_attempts:
                    break
                time.sleep(.3 * (attempt + 1))
        fetched = int(time.time() * 1000)
        source.update({"fetched_at": utc(fetched), "fetched_at_ms": fetched,
                       "elapsed_seconds": round(time.monotonic() - start, 3), "error": error["code"]})
        return None, source, error

    def _batch(self, jobs):
        result, sources, errors = {}, [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            pending = {pool.submit(self._get, name, url, ttl): name for name, (url, ttl) in jobs.items()}
            for future in concurrent.futures.as_completed(pending):
                name = pending[future]
                data, source, error = future.result()
                result[name] = data
                sources.append(source)
                if error:
                    errors.append(error)
        return result, sources, errors

    @staticmethod
    def _stamp(source, stamp, kind="EXCHANGE_EVENT"):
        if source is not None and stamp:
            source.update({"observed_at_ms": int(stamp), "observed_at": utc(stamp), "timestamp_kind": kind})

    def _instrument_jobs(self, symbol=None):
        if self.exchange == "bitget":
            return {"instruments_spot": (_url(BITGET, "/api/v3/market/instruments", category="SPOT"), 3600),
                    "instruments_perp": (_url(BITGET, "/api/v3/market/instruments", category="USDT-FUTURES"), 3600)}
        if self.exchange == "binance":
            return {"instruments_spot": (_url(BINANCE, "/api/v3/exchangeInfo", symbol=symbol,
                                             permissions="SPOT" if symbol is None else None, showPermissionSets="false"), 3600),
                    "instruments_perp": (_url(BINANCE_PERP, "/fapi/v1/exchangeInfo"), 3600)}
        return {"instruments_spot": (_url(BYBIT, "/v5/market/instruments-info", category="spot", symbol=symbol, limit=1000), 3600),
                "instruments_perp": (_url(BYBIT, "/v5/market/instruments-info", category="linear", symbol=symbol, limit=1000), 3600)}

    def _rows(self, payload, kind="generic"):
        if payload is None:
            return []
        if self.exchange == "bitget":
            if not isinstance(payload, dict):
                return []
            data = payload.get("data", [])
            if isinstance(data, list):
                return data
            return data.get("list", []) if isinstance(data, dict) and isinstance(data.get("list"), list) else []
        if self.exchange == "bybit":
            result = payload.get("result") if isinstance(payload, dict) else None
            return result.get("list", []) if isinstance(result, dict) and isinstance(result.get("list"), list) else []
        if kind == "instrument":
            return payload.get("symbols", []) if isinstance(payload, dict) and isinstance(payload.get("symbols"), list) else []
        return payload if isinstance(payload, list) else [payload]

    @staticmethod
    def _instrument_type(row):
        tags = " ".join(str(row.get(k, "")) for k in ("instrumentType", "symbolType", "fullName", "assetType", "underlyingType"))
        if (str(row.get("isRwa", "")).lower() in {"yes", "true", "1"} or
                str(row.get("isReality", "")).lower() in {"yes", "true", "1"} or row.get("isin") or
                re.search(r"(?i)\b(stock|stocks|xstocks|equity|equities|rwa|tokenized|commodity|commodities|forex|tradfi|index)\b", tags)):
            return "RWA"
        return "CRYPTO"

    def _crypto_instrument(self, row):
        base = str(row.get("baseCoin", row.get("baseAsset", "")))
        quote = row.get("quoteCoin", row.get("quoteAsset"))
        status = str(row.get("status", row.get("symbolStatus", ""))).lower()
        if quote != "USDT" or status not in {"online", "normal", "trading"}:
            return False
        rwa = self._instrument_type(row) == "RWA"
        if rwa and not self.include_rwa:
            return False
        if re.search(r"(?:[235][LS])$", base):
            return False
        return True

    def discover(self, limit=30, min_turnover=500000, max_turnover=50000000, *, cursor=0):
        """Return a rotating, metadata-verified spot universe, not a buy ranking.

        The worker should persist last_discovery.next_cursor to cover the whole
        eligible universe. This avoids repeatedly sampling only yesterday's
        biggest pumps or highest-volume coins. No contract guess is made here.
        """
        limit = max(1, min(500, int(limit)))
        minimum, maximum = float(min_turnover), float(max_turnover)
        if minimum < 0 or maximum < minimum:
            raise ValueError("Invalid turnover bounds")
        jobs = {"instruments_spot": self._instrument_jobs()["instruments_spot"]}
        if self.exchange == "bitget":
            jobs["tickers"] = (_url(BITGET, "/api/v3/market/tickers", category="SPOT"), 5)
        elif self.exchange == "binance":
            jobs["tickers"] = (_url(BINANCE, "/api/v3/ticker/24hr"), 5)
        else:
            jobs["tickers"] = (_url(BYBIT, "/v5/market/tickers", category="spot"), 5)
        data, sources, errors = self._batch(jobs)
        instruments = {x["symbol"]: x for x in self._rows(data.get("instruments_spot"), "instrument") if isinstance(x, dict) and x.get("symbol") and self._crypto_instrument(x)}
        candidates = []
        for row in self._rows(data.get("tickers")):
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if symbol not in instruments:
                continue
            turnover = number(row.get("turnover24h", row.get("quoteVolume")))
            if turnover is None or not minimum <= turnover <= maximum:
                continue
            price = number(row.get("lastPrice"))
            change = number(row.get("price24hPcnt"))
            change = change * 100 if change is not None else number(row.get("priceChangePercent"))
            if price is None or price <= 0:
                continue
            candidates.append({"symbol": symbol, "exchange": self.exchange, "product": "SPOT", "price": price,
                               "turnover_24h_usdt": turnover, "change_24h_pct": change,
                               "instrument_type": self._instrument_type(instruments[symbol]), "observed_at_ms": _ms(row.get("ts", row.get("closeTime"))),
                               "universe_only": True})
        candidates.sort(key=lambda x: x["symbol"])
        count = len(candidates)
        offset = int(cursor) % count if count else 0
        selected = (candidates[offset:] + candidates[:offset])[:min(limit, count)]
        self.last_discovery = {"status": "OK" if not errors else "PARTIAL" if selected else "ERROR", "sources": sources,
                               "errors": errors, "eligible_count": count, "returned_count": len(selected),
                               "cursor": offset, "next_cursor": (offset + len(selected)) % count if count else 0,
                               "selection": "ALPHABETICAL_ROTATION_NOT_PREDICTIVE_RANKING"}
        return selected

    def collect(self, symbol, now_ms=None):
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        symbol = clean_symbol(symbol)
        instruments, isources, ierrors = self._batch(self._instrument_jobs(symbol))
        spot_rows = self._rows(instruments.get("instruments_spot"), "instrument")
        perp_rows = self._rows(instruments.get("instruments_perp"), "instrument")
        spot_info = next((x for x in spot_rows if isinstance(x, dict) and x.get("symbol") == symbol), None)
        perp_info = next((x for x in perp_rows if isinstance(x, dict) and x.get("symbol") == symbol), None)
        spot_available = spot_info is not None and self._crypto_instrument(spot_info)
        future_available = perp_info is not None and self._crypto_instrument(perp_info)
        excluded = not spot_available and not future_available and (spot_info is not None or perp_info is not None)
        result = {"symbol": symbol, "exchange": self.exchange, "product": "SPOT", "spot_available": spot_available,
                  "has_futures": future_available, "price": None, "change_24h_pct": None,
                  "turnover_24h_usdt": None, "observed_at_ms": None, "ticker_observed_at_ms": None,
                  "candles": {tf: [] for tf in INTERVALS}, "derivatives": {"status": "UNKNOWN", "available": future_available},
                  "sources": isources, "errors": ierrors, "collected_at": utc(now_ms), "collected_at_ms": now_ms,
                  "market_microstructure": {"status": "UNKNOWN"}, "spot_flow": {"status": "UNKNOWN"},
                  "instrument_type": "EXCLUDED_OR_UNAVAILABLE" if excluded else "CRYPTO", "history_status": {}}
        if excluded and not self.include_rwa:
            result["errors"].append({"source": "instrument", "code": "INSTRUMENT_EXCLUDED", "message": "Non-crypto or inactive instrument excluded"})
            return result
        # If metadata lookup failed, do not guess universe identity or silently
        # request a similarly named contract. Partial evidence remains explicit.
        if not spot_available and not future_available:
            result["errors"].append({"source": "instrument", "code": "SYMBOL_UNAVAILABLE", "message": "Symbol not verified in this venue's active crypto instruments"})
            return result
        product = "SPOT" if spot_available else "USDT-FUTURES"
        result["product"] = product
        result["price_product"] = product
        chosen_instrument = spot_info if spot_available else perp_info
        result["instrument_type"] = self._instrument_type(chosen_instrument)
        result["instrument"] = {k: chosen_instrument.get(k) for k in ("symbol", "baseCoin", "baseAsset", "quoteCoin", "quoteAsset", "status", "symbolType", "instrumentType", "launchTime") if k in chosen_instrument}
        result["listing_time_ms"] = _ms(chosen_instrument.get("launchTime"))
        jobs = self._market_jobs(symbol, product, future_available)
        data, sources, errors = self._batch(jobs)
        result["sources"].extend(sources)
        result["errors"].extend(errors)
        smap = {x["name"]: x for x in sources}
        ticker = self._first(data.get("ticker"))
        if ticker:
            price = number(ticker.get("lastPrice"))
            change = number(ticker.get("price24hPcnt"))
            result.update({"price": price, "change_24h_pct": change * 100 if change is not None else number(ticker.get("priceChangePercent")),
                           "turnover_24h_usdt": number(ticker.get("turnover24h", ticker.get("quoteVolume")))})
            stamp = _ms(ticker.get("ts", ticker.get("closeTime")))
            result.update({"observed_at_ms": stamp, "ticker_observed_at_ms": stamp})
            self._stamp(smap.get("ticker"), stamp, "ROLLING_WINDOW_CLOSE" if self.exchange == "binance" else "EXCHANGE_EVENT")
        for tf in INTERVALS:
            raw = data.get("candles_" + tf)
            rows = self._rows(raw) if self.exchange != "binance" else raw or []
            candles = normalize_candles(rows, tf, now_ms, exchange=self.exchange, product=product)
            result["candles"][tf] = candles
            closed = [x for x in candles if x["is_closed"]]
            result["history_status"][tf] = {"total": len(candles), "closed": len(closed),
                                            "status": "AVAILABLE" if closed else "INSUFFICIENT",
                                            "gaps": sum(1 for a, b in zip(candles, candles[1:]) if b["open_time"] - a["open_time"] != INTERVALS[tf]),
                                            "latest_closed_at_ms": closed[-1]["close_time"] if closed else None}
            if closed:
                self._stamp(smap.get("candles_" + tf), closed[-1]["close_time"], "CANDLE_CLOSE")
            if not candles:
                result["errors"].append({"source": "candles_" + tf, "code": "HISTORY_UNAVAILABLE", "message": "No valid candles; no interpolation or guessed history"})
        book = self._book_data(data.get("book"))
        bookstamp = _ms(book.get("ts", book.get("T")))
        self._stamp(smap.get("book"), bookstamp)
        result["market_microstructure"] = _book_metrics(book, observed_at_ms=bookstamp)
        result["market_microstructure"].update({"exchange": self.exchange, "product": product})
        # A Binance REST spot depth response has lastUpdateId but no exchange
        # event timestamp. fetched_at must not impersonate observed_at.
        flow_rows = self._rows(data.get("flow"))
        result["spot_flow" if product == "SPOT" else "perp_flow"] = _flow_metrics(flow_rows, self.exchange)
        flow = result.get("spot_flow" if product == "SPOT" else "perp_flow", {})
        self._stamp(smap.get("flow"), flow.get("window_end_ms"), "LATEST_TRADE_IN_SAMPLE")
        if future_available:
            result["derivatives"] = self._derivatives(symbol, data, smap, perp_info or {})
        if product != "SPOT":
            result["errors"].append({"source": "spot", "code": "SPOT_DEMAND_UNAVAILABLE", "message": "Perpetual-only data cannot prove spot demand"})
        result["sources"].sort(key=lambda x: x["name"])
        return result

    def _first(self, payload):
        rows = self._rows(payload)
        return rows[0] if rows and isinstance(rows[0], dict) else None

    def _book_data(self, payload):
        if not isinstance(payload, dict):
            return {}
        if self.exchange == "bitget":
            return payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if self.exchange == "bybit":
            return payload.get("result") if isinstance(payload.get("result"), dict) else {}
        return payload

    def _market_jobs(self, symbol, product, future_available):
        jobs = {}
        if self.exchange == "bitget":
            for tf in INTERVALS:
                interval = {"15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}[tf]
                if tf == "1d":
                    # v3 1D is UTC+8 anchored. Use the documented classic UTC
                    # daily granularity; still the same venue and product.
                    if product == "SPOT":
                        jobs["candles_" + tf] = (_url(BITGET, "/api/v2/spot/market/candles", symbol=symbol, granularity="1Dutc", limit=300), 10)
                    else:
                        jobs["candles_" + tf] = (_url(BITGET, "/api/v2/mix/market/candles", symbol=symbol, productType="USDT-FUTURES", granularity="1Dutc", limit=300), 10)
                else:
                    jobs["candles_" + tf] = (_url(BITGET, "/api/v3/market/candles", category=product, symbol=symbol, interval=interval, limit=300, type="market"), 10)
            jobs.update({"ticker": (_url(BITGET, "/api/v3/market/tickers", category=product, symbol=symbol), 2),
                         "book": (_url(BITGET, "/api/v3/market/orderbook", category=product, symbol=symbol, limit=50), 0),
                         "flow": (_url(BITGET, "/api/v3/market/fills", category=product, symbol=symbol, limit=100), 0)})
            if future_available:
                jobs.update({"perp_ticker": (_url(BITGET, "/api/v3/market/tickers", category="USDT-FUTURES", symbol=symbol), 2),
                             "funding": (_url(BITGET, "/api/v2/mix/market/current-fund-rate", productType="USDT-FUTURES", symbol=symbol), 10),
                             "perp_book": (_url(BITGET, "/api/v3/market/orderbook", category="USDT-FUTURES", symbol=symbol, limit=50), 0),
                             "perp_flow": (_url(BITGET, "/api/v3/market/fills", category="USDT-FUTURES", symbol=symbol, limit=100), 0)})
        elif self.exchange == "binance":
            base, prefix = (BINANCE, "/api/v3") if product == "SPOT" else (BINANCE_PERP, "/fapi/v1")
            for tf in INTERVALS:
                jobs["candles_" + tf] = (_url(base, prefix + "/klines", symbol=symbol, interval=tf, limit=300), 10)
            jobs.update({"ticker": (_url(base, prefix + "/ticker/24hr", symbol=symbol), 2),
                         "book": (_url(base, prefix + "/depth", symbol=symbol, limit=100), 0),
                         "flow": (_url(base, prefix + "/aggTrades", symbol=symbol, limit=100), 0)})
            if future_available:
                jobs.update({"perp_ticker": (_url(BINANCE_PERP, "/fapi/v1/ticker/24hr", symbol=symbol), 2),
                             "funding": (_url(BINANCE_PERP, "/fapi/v1/premiumIndex", symbol=symbol), 5),
                             "open_interest": (_url(BINANCE_PERP, "/fapi/v1/openInterest", symbol=symbol), 0),
                             "funding_info": (_url(BINANCE_PERP, "/fapi/v1/fundingInfo"), 3600),
                             "perp_book": (_url(BINANCE_PERP, "/fapi/v1/depth", symbol=symbol, limit=100), 0),
                             "perp_flow": (_url(BINANCE_PERP, "/fapi/v1/aggTrades", symbol=symbol, limit=100), 0)})
        else:
            category = "spot" if product == "SPOT" else "linear"
            for tf in INTERVALS:
                interval = {"15m": "15", "1h": "60", "4h": "240", "1d": "D"}[tf]
                jobs["candles_" + tf] = (_url(BYBIT, "/v5/market/kline", category=category, symbol=symbol, interval=interval, limit=300), 10)
            jobs.update({"ticker": (_url(BYBIT, "/v5/market/tickers", category=category, symbol=symbol), 2),
                         "book": (_url(BYBIT, "/v5/market/orderbook", category=category, symbol=symbol, limit=50), 0),
                         "flow": (_url(BYBIT, "/v5/market/recent-trade", category=category, symbol=symbol, limit=100), 0)})
            if future_available:
                jobs.update({"perp_ticker": (_url(BYBIT, "/v5/market/tickers", category="linear", symbol=symbol), 2),
                             "open_interest": (_url(BYBIT, "/v5/market/open-interest", category="linear", symbol=symbol, intervalTime="5min", limit=1), 0),
                             "perp_book": (_url(BYBIT, "/v5/market/orderbook", category="linear", symbol=symbol, limit=50), 0),
                             "perp_flow": (_url(BYBIT, "/v5/market/recent-trade", category="linear", symbol=symbol, limit=100), 0)})
        return jobs

    def _derivatives(self, symbol, data, smap, instrument):
        ticker = self._first(data.get("perp_ticker")) or {}
        perp_price = number(ticker.get("lastPrice"))
        oi = number(ticker.get("openInterest"))
        funding, interval, next_time, funding_stamp = number(ticker.get("fundingRate")), None, None, None
        oi_stamp = _ms(ticker.get("ts"))
        if self.exchange == "bitget":
            row = self._first(data.get("funding")) or {}
            funding = number(row.get("fundingRate"))
            interval = number(row.get("fundingRateInterval"))
            next_time = _ms(row.get("nextUpdate"))
            funding_stamp = None  # requestTime is a server response timestamp, not event observation.
        elif self.exchange == "binance":
            premium = data.get("funding") or {}
            funding = number(premium.get("lastFundingRate"))
            next_time, funding_stamp = _ms(premium.get("nextFundingTime")), _ms(premium.get("time"))
            info = next((x for x in data.get("funding_info") or [] if isinstance(x, dict) and x.get("symbol") == symbol), None)
            interval = number(info.get("fundingIntervalHours")) if info else None
            # fundingInfo is only adjusted contracts; absence does not establish 8h.
            oirow = data.get("open_interest") or {}
            oi, oi_stamp = number(oirow.get("openInterest")), _ms(oirow.get("time"))
        else:
            interval_minutes = number(instrument.get("fundingInterval"))
            interval = interval_minutes / 60 if interval_minutes else None
            next_time = _ms(ticker.get("nextFundingTime"))
            oirow = self._first(data.get("open_interest")) or {}
            oi, oi_stamp = number(oirow.get("openInterest")), _ms(oirow.get("timestamp"))
        book = self._book_data(data.get("perp_book"))
        book_stamp = _ms(book.get("ts", book.get("T")))
        flow = _flow_metrics(self._rows(data.get("perp_flow")), self.exchange)
        self._stamp(smap.get("perp_book"), book_stamp)
        self._stamp(smap.get("perp_flow"), flow.get("window_end_ms"), "LATEST_TRADE_IN_SAMPLE")
        self._stamp(smap.get("perp_ticker"), _ms(ticker.get("ts", ticker.get("closeTime"))))
        self._stamp(smap.get("open_interest"), oi_stamp, "OPEN_INTEREST_SNAPSHOT")
        self._stamp(smap.get("funding"), funding_stamp, "FUNDING_SNAPSHOT")
        perp_book = _book_metrics(book, observed_at_ms=book_stamp)
        perp_book["slippage_buy_bps"] = perp_book.get("buy_slippage_bps_from_mid")
        perp_book["slippage_sell_bps"] = perp_book.get("sell_slippage_bps_from_mid")
        return {"status": "AVAILABLE" if ticker else "UNKNOWN", "available": bool(ticker), "exchange": self.exchange,
                "product": "USDT-FUTURES", "price": perp_price,
                "turnover_24h_usdt": number(ticker.get("turnover24h", ticker.get("quoteVolume"))),
                "open_interest": oi, "open_interest_base_units": oi, "oi_unit": "BASE_COIN",
                "oi_snapshot": {"base_units": oi, "observed_at_ms": oi_stamp, "unit": "BASE_COIN"},
                "oi_observed_at_ms": oi_stamp, "oi_notional_usd": oi * perp_price if oi is not None and perp_price else None,
                "oi_timestamp_kind": "TICKER_ASOF_NOT_OI_CHANGE_EVENT" if self.exchange == "bitget" else "OPEN_INTEREST_SNAPSHOT",
                "oi_notional_method": "ESTIMATE_BASE_UNITS_X_PERP_LAST_PRICE",
                "oi_change_4h_pct": None, "oi_change_24h_pct": None,
                "oi_history_note": "One base-unit snapshot; changes require persisted, time-aligned base-unit snapshots",
                "funding_rate": funding, "funding_rate_pct": funding * 100 if funding is not None else None,
                "funding_interval_hours": interval, "next_funding_time": next_time,
                "funding_observed_at_ms": funding_stamp,
                "funding_snapshot_fetched_at_ms": (smap.get("funding") or smap.get("perp_ticker") or {}).get("fetched_at_ms"),
                "funding_note": "Current published rate; settlement can change. Interval is never assumed 8h.",
                "orderbook": perp_book, "taker_flow": flow,
                "liquidations": {"status": "UNKNOWN", "note": "No liquidation tape fetched; price/OI/flow are not a liquidation fact"}}

    def enrich_identity(self, symbol, market_data, now_ms=None):
        """Fetch registry-scoped fundamentals and approved DEX pools, public only.

        All registered CoinGecko IDs share one markets request/cache to reduce
        rate-limit exposure. Unknown projects remain visible without fabricated
        cap/supply/unlock data. Unlock calendars, VC/whale attribution, and news
        must be added as independently sourced evidence, not inferred here.
        """
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        identity = get_identity(symbol)
        answer = {"identity": identity,
                  "fundamentals": {"status": "UNKNOWN", "market_cap_usd": None, "fdv_usd": None,
                                   "circulating_supply": None, "total_supply": None, "max_supply": None,
                                   "circulating_pct": None, "fdv_mcap_ratio": None,
                                   "observed_at_ms": None, "unlocks": {"status": "UNKNOWN", "events": []}},
                  "onchain": aggregate_dex_pairs(identity, [], market_data.get("price"), now_ms),
                  "sources": [], "errors": []}
        if not identity.get("coingecko_id"):
            answer["errors"].append({"source": "identity", "code": "PROJECT_ID_UNVERIFIED", "message": "Unreviewed project: no CoinGecko or DEX ticker guessing"})
            return answer
        ids = registry_ids()
        url = _url(COINGECKO, "/coins/markets", vs_currency="usd", ids=",".join(sorted(ids.values())),
                   price_change_percentage="7d,30d", per_page=100, page=1, sparkline="false")
        with self._fund_lock:
            payload, source, error = self._get("coingecko_markets", url, 120)
        answer["sources"].append(source)
        if error:
            answer["errors"].append(error)
        match = next((r for r in payload or [] if isinstance(r, dict) and r.get("id") == identity["coingecko_id"]), None) if isinstance(payload, list) else None
        if match is not None and str(match.get("symbol", "")).upper() != identity["symbol"]:
            answer["errors"].append({"source": "coingecko", "code": "PROJECT_SYMBOL_MISMATCH", "message": "Curated ID returned another symbol; data excluded"})
            match = None
        if match is not None:
            cap, fdv = number(match.get("market_cap")), number(match.get("fully_diluted_valuation"))
            circulating, total, maximum = number(match.get("circulating_supply")), number(match.get("total_supply")), number(match.get("max_supply"))
            stamp = None
            try:
                timestamp = dt.datetime.fromisoformat(str(match.get("last_updated")).replace("Z", "+00:00"))
                if timestamp.tzinfo is not None:
                    stamp = int(timestamp.timestamp() * 1000)
            except (ValueError, TypeError):
                pass
            self._stamp(source, stamp, "AGGREGATOR_LAST_UPDATED")
            answer["fundamentals"].update({"status": "AVAILABLE" if cap is not None and circulating is not None else "PARTIAL",
                                           "project_id": match["id"], "project_name": match.get("name"),
                                           "price_usd_aggregate": number(match.get("current_price")),
                                           "market_cap_usd": cap, "fdv_usd": fdv,
                                           "circulating_supply": circulating, "total_supply": total, "max_supply": maximum,
                                           "circulating_pct": circulating / maximum * 100 if circulating is not None and maximum and maximum > 0 else None,
                                           "circulating_pct_denominator": "MAX_SUPPLY" if maximum and maximum > 0 else None,
                                           "fdv_mcap_ratio": fdv / cap if fdv is not None and cap and cap > 0 else None,
                                           "market_cap_rank": match.get("market_cap_rank"),
                                           "ath_usd_aggregate": number(match.get("ath")), "ath_date_aggregate": match.get("ath_date"),
                                           "change_7d_pct": number(match.get("price_change_percentage_7d_in_currency")),
                                           "change_30d_pct": number(match.get("price_change_percentage_30d_in_currency")),
                                           "observed_at_ms": stamp, "fetched_at_ms": source["fetched_at_ms"],
                                           "source": source["url"], "venue_scope": "AGGREGATE_NOT_EXCHANGE_CANDLES"})
        elif error is None:
            answer["errors"].append({"source": "coingecko", "code": "PROJECT_NOT_RETURNED", "message": "No data for curated CoinGecko project ID"})
        pairs = []
        for contract in identity.get("contracts", []):
            if not contract.get("source"):
                continue
            chain, address = contract.get("chain"), contract.get("address")
            url = DEXSCREENER + "/token-pairs/v1/" + urllib.parse.quote(str(chain), safe="") + "/" + urllib.parse.quote(str(address), safe="")
            payload, source, error = self._get("dex_" + str(chain), url, 30)
            answer["sources"].append(source)
            if error:
                answer["errors"].append(error)
            elif isinstance(payload, list):
                pairs.extend(dict(x, _source_fetched_at_ms=source["fetched_at_ms"]) for x in payload if isinstance(x, dict))
            elif isinstance(payload, dict):
                pairs.extend(dict(x, _source_fetched_at_ms=source["fetched_at_ms"]) for x in payload.get("pairs") or [] if isinstance(x, dict))
        answer["onchain"] = aggregate_dex_pairs(identity, pairs, market_data.get("price"), now_ms)
        answer["onchain"]["source_urls"] = [x["url"] for x in answer["sources"] if x["name"].startswith("dex_")]
        return answer
