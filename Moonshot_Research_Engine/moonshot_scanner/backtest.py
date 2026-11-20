"""Conservative bar replay for explicit, timestamped research signals.

This utility is not strategy validation by itself. Signals must be generated using
only information available at their signal bar. No in-sample statistic is presented
as a calibrated probability of the next signal.
"""
from __future__ import annotations

import math
from statistics import mean


def _valid_number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _wilson(wins, total):
    if not total:
        return None
    z = 1.959963984540054
    p = wins / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, centre - half), min(1.0, centre + half)]


def chronological_split(candles, train_fraction=0.7, purge_bars=18):
    """Time-order split with an explicit purge gap, without fitting any model."""
    if not 0 < train_fraction < 1 or purge_bars < 0:
        raise ValueError("invalid split parameters")
    count = len(candles)
    train_end = int(count * train_fraction)
    test_start = min(count, train_end + int(purge_bars))
    return {"train": candles[:train_end], "test": candles[test_start:],
            "train_end_index": train_end, "test_start_index": test_start,
            "purged_bars": test_start - train_end,
            "note": "split only; no out-of-sample improvement has been established"}


def run_backtest(candles, signals=None, *, fees_bps=12.0, slippage_bps=10.0,
                 funding_bps_per_8h=0.0, max_holding_bars=18, now_ms=None):
    """Replay explicit signals with next-bar earliest fills and stop-first ties.

    Signal fields: signal_index, side, stop, target (or tp1/targets), optional
    entry_mode (MARKET/LIMIT/BREAKOUT), entry, expires_bars, generated_at_ms.
    Index refers to the supplied chronological candle list, before open bars are
    removed. Signals on developing bars are rejected. Only one position at a time.
    fees_bps/slippage_bps are round-trip estimates; funding is an explicit cost
    proxy, not historical venue funding. R uses actual entry-to-stop distance.
    """
    if fees_bps < 0 or slippage_bps < 0 or max_holding_bars < 1:
        raise ValueError("costs must be nonnegative and holding period positive")
    if any(_valid_number(v) is None for v in (fees_bps, slippage_bps, funding_bps_per_8h)):
        raise ValueError("cost assumptions must be finite")
    bars, original_to_closed = [], {}
    for original, raw in enumerate(candles):
        if raw.get("is_closed") is not True:
            continue
        opened, ended = _valid_number(raw.get("open_time")), _valid_number(raw.get("close_time"))
        if opened is None or ended is None or ended <= opened or (now_ms is not None and ended > now_ms):
            continue
        nums = {key: _valid_number(raw.get(key)) for key in ("open", "high", "low", "close")}
        if any(x is None or x <= 0 for x in nums.values()):
            raise ValueError("invalid OHLC in closed replay candle")
        if not nums["low"] <= min(nums["open"], nums["close"]) <= max(nums["open"], nums["close"]) <= nums["high"]:
            raise ValueError("invalid OHLC geometry in closed replay candle")
        if bars and opened <= bars[-1]["open_time"]:
            raise ValueError("candles must be strictly chronological without duplicates")
        original_to_closed[original] = len(bars)
        bars.append(dict(raw, **nums))
    result = {
        "status": "NOT_RUN" if signals is None else "COMPLETED",
        "trades": [], "trade_count": 0, "win_rate": None, "win_rate_pct": None,
        "win_rate_wilson_95": None, "expectancy_r": None, "profit_factor": None,
        "max_drawdown_r": None, "equity_curve_r": [0.0], "reliability": "NONE",
        "rejected_signals": [], "fees_bps": fees_bps, "slippage_bps": slippage_bps,
        "funding_model": "FIXED_COST_PROXY" if funding_bps_per_8h else "NOT_INCLUDED",
        "probability_next_trade": None,
        "limitations": ["bar OHLC cannot reveal intrabar path; same-bar TP/SL uses stop first",
                        "signal provenance must be point-in-time; this replay cannot audit external feature generation",
                        "fees/slippage are estimates, not historical order-book fills",
                        "no liquidation model, leverage model, borrowing, or market impact",
                        "no out-of-sample edge has been demonstrated"],
    }
    if not funding_bps_per_8h:
        result["limitations"].append("historical funding is absent; reported returns exclude funding")
    else:
        result["limitations"].append("funding is a fixed cost proxy, not contract-specific historical settlements")
    if signals is None:
        result["limitations"].append("no explicit strategy signals supplied; no backtest was invented")
        return result
    available_after = -1
    half_slip = slippage_bps / 20_000.0
    fee_rate = fees_bps / 20_000.0
    ordered = sorted(signals, key=lambda s: s.get("signal_index", s.get("index", -1)))
    for signal in ordered:
        original_index = signal.get("signal_index", signal.get("index"))
        if not isinstance(original_index, int) or original_index not in original_to_closed:
            result["rejected_signals"].append({"signal_index": original_index, "reason": "SIGNAL_BAR_NOT_CLOSED_OR_MISSING"})
            continue
        index = original_to_closed[original_index]
        if index < available_after:
            result["rejected_signals"].append({"signal_index": original_index, "reason": "OVERLAPPING_POSITION"})
            continue
        generated_at = _valid_number(signal.get("generated_at_ms"))
        if signal.get("generated_at_ms") is not None and (generated_at is None or generated_at > bars[index]["close_time"]):
            result["rejected_signals"].append({"signal_index": original_index, "reason": "SIGNAL_GENERATED_AFTER_BAR_CLOSE"})
            continue
        side = str(signal.get("side", "")).upper()
        stop = _valid_number(signal.get("stop"))
        raw_target = signal.get("target", signal.get("tp1"))
        if raw_target is None and signal.get("targets"):
            raw_target = signal["targets"][0]
            raw_target = raw_target.get("price") if isinstance(raw_target, dict) else raw_target
        target = _valid_number(raw_target)
        entry_value = _valid_number(signal.get("entry", signal.get("entry_mid")))
        mode = str(signal.get("entry_mode", "MARKET")).upper()
        if side not in ("LONG", "SHORT") or stop is None or target is None or min(stop, target) <= 0 or mode not in ("MARKET", "LIMIT", "BREAKOUT"):
            result["rejected_signals"].append({"signal_index": original_index, "reason": "INVALID_SIGNAL_PLAN"})
            continue
        if mode != "MARKET" and (entry_value is None or entry_value <= 0):
            result["rejected_signals"].append({"signal_index": original_index, "reason": "ENTRY_TRIGGER_MISSING"})
            continue
        sign = 1.0 if side == "LONG" else -1.0
        try:
            expires = max(1, min(int(signal.get("expires_bars", 3)), len(bars)))
        except (TypeError, ValueError, OverflowError):
            result["rejected_signals"].append({"signal_index": original_index, "reason": "INVALID_ENTRY_EXPIRY"})
            continue
        filled = None
        for fill_index in range(index + 1, min(len(bars), index + 1 + expires)):
            bar = bars[fill_index]
            if mode == "MARKET":
                fill = bar["open"] * (1 + sign * half_slip)
            elif mode == "LIMIT":
                touched = bar["low"] <= entry_value if side == "LONG" else bar["high"] >= entry_value
                if not touched:
                    continue
                fill = min(bar["open"], entry_value) if side == "LONG" else max(bar["open"], entry_value)
            else:
                touched = bar["high"] >= entry_value if side == "LONG" else bar["low"] <= entry_value
                if not touched:
                    invalidated = bar["low"] <= stop if side == "LONG" else bar["high"] >= stop
                    if invalidated:
                        result["rejected_signals"].append({"signal_index": original_index, "reason": "INVALIDATION_BEFORE_ENTRY"})
                        filled = False
                        break
                    continue
                fill = (max(bar["open"], entry_value) if side == "LONG" else min(bar["open"], entry_value)) * (1 + sign * half_slip)
            if not (stop < fill < target if side == "LONG" else target < fill < stop):
                result["rejected_signals"].append({"signal_index": original_index, "reason": "GAP_OR_COST_INVALIDATED_FILL_GEOMETRY"})
                filled = False
                break
            filled = fill_index, fill
            break
        if not filled:
            if filled is None:
                result["rejected_signals"].append({"signal_index": original_index, "reason": "ENTRY_NOT_FILLED_BEFORE_EXPIRY"})
            continue
        fill_index, entry = filled
        risk = abs(entry - stop)
        exit_index = min(len(bars) - 1, fill_index + max_holding_bars - 1)
        exit_raw, reason, ambiguous = bars[exit_index]["close"], "TIME_EXIT", False
        if exit_index == len(bars) - 1 and exit_index < fill_index + max_holding_bars - 1:
            reason = "END_OF_SAMPLE_MARK_TO_MARKET"
        for j in range(fill_index, exit_index + 1):
            bar = bars[j]
            stop_hit = bar["low"] <= stop if side == "LONG" else bar["high"] >= stop
            target_hit = bar["high"] >= target if side == "LONG" else bar["low"] <= target
            if stop_hit:
                # A gap through the stop must be worse than the intended stop.
                exit_raw = min(bar["open"], stop) if side == "LONG" else max(bar["open"], stop)
                exit_index, reason, ambiguous = j, "STOP", bool(target_hit)
                break
            if target_hit:
                # Do not grant favourable gap improvements to a limit target.
                exit_raw, exit_index, reason = target, j, "TARGET"
                break
        exit_fill = exit_raw * (1 - sign * half_slip)
        duration = max(0, bars[exit_index]["close_time"] - bars[fill_index]["open_time"])
        fees = (entry + exit_fill) * fee_rate
        funding = entry * funding_bps_per_8h / 10_000.0 * duration / 28_800_000.0
        net = sign * (exit_fill - entry) - fees - funding
        net_r = net / risk
        trade = {"signal_index": original_index, "fill_index": fill_index, "exit_index": exit_index,
                 "side": side, "entry_mode": mode, "entry": entry, "exit": exit_fill,
                 "stop": stop, "target": target, "net_r": net_r, "exit_reason": reason,
                 "same_bar_path_ambiguous": ambiguous,
                 "entry_time_ms": bars[fill_index]["open_time"],
                 "exit_time_ms": bars[exit_index]["close_time"],
                 "fees_per_unit": fees, "funding_proxy_per_unit": funding}
        result["trades"].append(trade)
        result["equity_curve_r"].append(result["equity_curve_r"][-1] + net_r)
        available_after = exit_index
    trades = result["trades"]
    count = len(trades)
    result["trade_count"] = count
    if count:
        returns = [t["net_r"] for t in trades]
        wins = sum(x > 0 for x in returns)
        gains, losses = sum(max(x, 0) for x in returns), -sum(min(x, 0) for x in returns)
        peak, drawdown = 0.0, 0.0
        for equity in result["equity_curve_r"]:
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        result.update(win_rate=wins / count, win_rate_pct=wins / count * 100.0,
                      win_rate_wilson_95=_wilson(wins, count), expectancy_r=mean(returns),
                      profit_factor=gains / losses if losses else None,
                      max_drawdown_r=drawdown,
                      reliability="INSUFFICIENT" if count < 30 else "IN_SAMPLE_ONLY")
    return result
