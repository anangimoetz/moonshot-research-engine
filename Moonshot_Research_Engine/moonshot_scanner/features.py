"""Closed-candle features for research, never a next-trade probability.

Inputs are venue/product specific normalized candles. Timestamps are milliseconds.
The last completed 4h bar is compared with the *preceding* 42 completed bars;
missing intervals and developing bars must not silently change that denominator.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Any


INTERVAL_MS = {"1m": 60_000, "15m": 900_000, "1h": 3_600_000,
               "4h": 14_400_000, "1d": 86_400_000}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for value in values[period:]:
        result += alpha * (value - result)
    return result


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(x, 0.0) for x in changes[:period]) / period
    loss = sum(max(-x, 0.0) for x in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
    if gain == loss == 0:
        return 50.0
    return 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)


def _atr(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    values = []
    for previous, candle in zip(candles, candles[1:]):
        values.append(max(candle["high"] - candle["low"],
                          abs(candle["high"] - previous["close"]),
                          abs(candle["low"] - previous["close"])))
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (result * (period - 1) + value) / period
    return result


def _dedupe_levels(values: list[float]) -> list[float]:
    result = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > max(value, result[-1]) * 0.001:
            result.append(value)
    return result


def _closed(candles: list[dict], now_ms: int, interval: int | None):
    result, issues, developing = [], [], []
    previous_open = None
    seen = set()
    for raw in sorted(candles, key=lambda x: _number(x.get("open_time")) or 0):
        opened, ended = _number(raw.get("open_time")), _number(raw.get("close_time"))
        if opened is None or ended is None or ended <= opened:
            issues.append("INVALID_CANDLE_TIMESTAMPS")
            continue
        if opened in seen:
            issues.append("DUPLICATE_CANDLE")
            continue
        seen.add(opened)
        if opened > now_ms:
            issues.append("FUTURE_CANDLE")
            continue
        if raw.get("is_closed") is not True or ended > now_ms:
            developing.append(raw)
            continue
        nums = {key: _number(raw.get(key)) for key in ("open", "high", "low", "close")}
        if any(v is None or v <= 0 for v in nums.values()):
            issues.append("INVALID_OHLC")
            continue
        if not (nums["low"] <= min(nums["open"], nums["close"]) <=
                max(nums["open"], nums["close"]) <= nums["high"]):
            issues.append("INVALID_OHLC_GEOMETRY")
            continue
        if interval and not (interval - 1 <= ended - opened <= interval):
            issues.append("CANDLE_DURATION_MISMATCH")
            continue
        if previous_open is not None and interval and opened - previous_open != interval:
            issues.append("CANDLE_GAP")
        previous_open = opened
        row = dict(raw, **nums, open_time=int(opened), close_time=int(ended))
        row["quote_turnover"] = _number(raw.get("quote_turnover"))
        if row["quote_turnover"] is not None and row["quote_turnover"] < 0:
            row["quote_turnover"] = None
        result.append(row)
    return result, sorted(set(issues)), developing


def _frame(candles: list[dict], issues: list[str]) -> dict:
    count = len(candles)
    result = {
        "count_closed": count,
        "history_status": ("MISSING" if count == 0 else "HISTORY_INSUFFICIENT" if count < 21
                           else "PARTIAL_HISTORY" if count < 200 else "COMPLETE"),
        "last_close": None, "last_open_time": None, "last_close_time": None,
        "last_high": None, "last_low": None,
        "ema13": None, "ema21": None, "ema50": None, "ema200": None,
        "rsi14": None, "atr14": None, "trend": "INSUFFICIENT",
        "higher_lows": None, "lower_highs": None,
        "compression_ratio": None, "is_compressed": None,
        "structural_support": None, "structural_resistance": None,
        "support_levels": [], "resistance_levels": [], "data_gaps": issues,
    }
    if not candles:
        return result
    last = candles[-1]
    closes = [c["close"] for c in candles]
    result.update(last_close=last["close"], last_high=last["high"], last_low=last["low"],
                  last_open_time=last["open_time"], last_close_time=last["close_time"],
                  rsi14=_rsi(closes), atr14=_atr(candles))
    for period in (13, 21, 50, 200):
        result[f"ema{period}"] = _ema(closes, period)
    if result["ema21"] is not None:
        e13, e21 = result["ema13"], result["ema21"]
        result["trend"] = ("BULLISH" if last["close"] > e13 > e21 else
                           "BEARISH" if last["close"] < e13 < e21 else "MIXED")
    if count >= 22:
        e13_prev, e21_prev = _ema(closes[:-1], 13), _ema(closes[:-1], 21)
        result["ema13_cross_up"] = e13_prev <= e21_prev and result["ema13"] > result["ema21"]
        result["ema13_cross_down"] = e13_prev >= e21_prev and result["ema13"] < result["ema21"]
    else:
        result["ema13_cross_up"] = result["ema13_cross_down"] = None
    pivot_lows, pivot_highs = [], []
    # Both right-hand confirming candles are already closed at the analysis time.
    for index in range(2, count - 2):
        group = candles[index - 2:index + 3]
        candle = candles[index]
        if candle["low"] == min(x["low"] for x in group) and any(x["low"] > candle["low"] for x in group):
            pivot_lows.append(candle["low"])
        if candle["high"] == max(x["high"] for x in group) and any(x["high"] < candle["high"] for x in group):
            pivot_highs.append(candle["high"])
    result["support_levels"] = _dedupe_levels(pivot_lows)
    result["resistance_levels"] = _dedupe_levels(pivot_highs)
    if len(pivot_lows) >= 3:
        result["higher_lows"] = pivot_lows[-3] < pivot_lows[-2] < pivot_lows[-1]
    if len(pivot_highs) >= 3:
        result["lower_highs"] = pivot_highs[-3] > pivot_highs[-2] > pivot_highs[-1]
    if count >= 21:
        previous = candles[-21:-1]
        result["structural_support"] = min(c["low"] for c in previous)
        result["structural_resistance"] = max(c["high"] for c in previous)
    if count >= 30:
        recent, previous = candles[-10:], candles[-30:-10]
        old_range = max(c["high"] for c in previous) - min(c["low"] for c in previous)
        new_range = max(c["high"] for c in recent) - min(c["low"] for c in recent)
        if old_range > 0:
            result["compression_ratio"] = new_range / old_range
            result["is_compressed"] = result["compression_ratio"] <= 0.65
    return result


def analyze_features(candles_by_tf: dict[str, list], now_ms: int) -> dict:
    """Return descriptive closed-bar evidence; absent history stays absent."""
    timeframes, closed_by_tf, developing, gaps = {}, {}, {}, []
    normalized = {str(k).lower(): v for k, v in candles_by_tf.items()}
    for timeframe in ("1d", "4h", "1h", "15m"):
        candles, issues, open_bars = _closed(normalized.get(timeframe, []), now_ms,
                                              INTERVAL_MS[timeframe])
        closed_by_tf[timeframe] = candles
        timeframes[timeframe] = _frame(candles, issues)
        developing[timeframe] = bool(open_bars)
        gaps.extend(f"{timeframe}:{issue}" for issue in issues)
        if len(candles) < 21:
            gaps.append(f"{timeframe}:HISTORY_INSUFFICIENT_FOR_EMA21")
        if len(candles) < 200:
            gaps.append(f"{timeframe}:EMA200_UNAVAILABLE")
    c4 = closed_by_tf["4h"]
    ratio, baseline, change24, evidence = None, None, None, []
    # The latest full bar plus exactly seven days of its immediate predecessors.
    window = c4[-43:]
    contiguous = len(window) == 43 and all(
        b["open_time"] - a["open_time"] == INTERVAL_MS["4h"]
        for a, b in zip(window, window[1:]))
    if contiguous and all(c.get("quote_turnover") is not None for c in window):
        baseline = median(c["quote_turnover"] for c in window[:-1])
        if baseline > 0:
            ratio = window[-1]["quote_turnover"] / baseline
        else:
            gaps.append("4h:ZERO_TURNOVER_BASELINE")
    else:
        gaps.append("4h:SEVEN_DAY_TURNOVER_BASELINE_UNAVAILABLE")
    last7 = c4[-7:]
    if len(last7) == 7 and all(b["open_time"] - a["open_time"] == INTERVAL_MS["4h"]
                              for a, b in zip(last7, last7[1:])):
        change24 = (last7[-1]["close"] / last7[0]["close"] - 1.0) * 100.0
    if ratio is not None:
        if change24 is not None and ((ratio >= 3 and change24 < 15) or (ratio >= 5 and change24 < 25)):
            evidence.append("RELATIVE_TURNOVER_ACTIVATION")
        if ratio >= 3:
            evidence.append("TURNOVER_SPIKE_IS_NOT_ACCUMULATION_PROOF")
    frame = timeframes["4h"]
    if frame["is_compressed"]:
        evidence.append("CLOSED_4H_RANGE_COMPRESSION")
    if frame["higher_lows"]:
        evidence.append("CONFIRMED_PIVOT_HIGHER_LOWS")
    if frame.get("ema13_cross_up"):
        evidence.append("CLOSED_4H_EMA13_CROSS_ABOVE_EMA21")
    active = "RELATIVE_TURNOVER_ACTIVATION" in evidence
    discovery_status = "EARLY" if active else "WATCH" if c4 else "HISTORY_INSUFFICIENT"
    return {
        "timeframes": timeframes, "4h": frame,
        "turnover_ratio_4h": ratio, "baseline_4h_median_7d": baseline,
        "turnover_baseline_bars": 42 if contiguous else None,
        "turnover_baseline_method": "latest closed 4h quote turnover / median preceding 42 contiguous closed 4h bars",
        "price_change_24h_pct": change24,
        "discovery_status": discovery_status, "discovery_evidence": evidence,
        "early_discovery": True if active else False if ratio is not None and change24 is not None else None,
        "data_gaps": sorted(set(gaps)), "latest_closed_4h": c4[-1] if c4 else None,
        "current_4h_developing": developing["4h"], "developing_timeframes": developing,
        "observed_at_ms": c4[-1]["close_time"] if c4 else None,
        "analyzed_at_ms": now_ms,
        "probability": None,
    }
