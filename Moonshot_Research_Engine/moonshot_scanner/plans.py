"""Separate structural research plans. No orders and no leverage suggestions."""
from __future__ import annotations

import math


def _finite_positive(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _targets(plan):
    raw = plan.get("targets")
    if raw is None:
        raw = [plan.get("tp1"), plan.get("tp2")]
    if not isinstance(raw, (list, tuple)):
        return []
    return [v.get("price") if isinstance(v, dict) else v for v in raw if v is not None]


def validate_plan(plan: dict, min_rr: float = 2.0) -> list[str]:
    """Validate the worst price in the entry zone, including estimated costs."""
    errors = []
    if not isinstance(plan, dict):
        return ["INVALID_PLAN_SCHEMA"]
    threshold = _finite_positive(min_rr)
    if threshold is None or threshold < 2:
        return ["INVALID_RR_THRESHOLD"]
    min_rr = threshold
    if "fees_bps" not in plan or "slippage_bps" not in plan:
        errors.append("MISSING_COST_ASSUMPTIONS")
    side = str(plan.get("side", "")).upper()
    if side not in ("LONG", "SHORT"):
        errors.append("INVALID_PLAN_SIDE")
    lo, hi = _finite_positive(plan.get("entry_low")), _finite_positive(plan.get("entry_high"))
    stop = _finite_positive(plan.get("stop"))
    targets = [_finite_positive(v) for v in _targets(plan)]
    if lo is None or hi is None or stop is None or not targets or any(v is None for v in targets):
        return errors + ["MISSING_OR_NONFINITE_PLAN_LEVELS"]
    if lo > hi:
        errors.append("ENTRY_RANGE_REVERSED")
    if side == "LONG":
        if not stop < lo <= hi < targets[0]:
            errors.append("LONG_LEVEL_GEOMETRY_INVALID")
        if any(b <= a for a, b in zip(targets, targets[1:])):
            errors.append("LONG_TARGETS_NOT_INCREASING")
        entry, sign = hi, 1.0
    else:
        if not targets[0] < lo <= hi < stop:
            errors.append("SHORT_LEVEL_GEOMETRY_INVALID")
        if any(b >= a for a, b in zip(targets, targets[1:])):
            errors.append("SHORT_TARGETS_NOT_DECREASING")
        entry, sign = lo, -1.0
    try:
        if isinstance(plan.get("fees_bps"), bool) or isinstance(plan.get("slippage_bps"), bool):
            raise ValueError
        fees, slippage = float(plan.get("fees_bps", 0)), float(plan.get("slippage_bps", 0))
        if not math.isfinite(fees + slippage) or fees < 0 or slippage < 0:
            raise ValueError
        # Price-dependent round-trip cost proxy, explicitly not exact fills.
        estimated_cost = entry * (fees + slippage) / 10_000.0
        risk = abs(entry - stop) + estimated_cost
        reward = sign * (targets[0] - entry) - estimated_cost
        rr = reward / risk if risk > 0 else -math.inf
        if rr < min_rr:
            errors.append("NET_RR_BELOW_MINIMUM")
    except (TypeError, ValueError, OverflowError):
        errors.append("INVALID_COST_ASSUMPTIONS")
    return errors


def _wait(side, setup, reasons):
    return {"side": side, "setup_type": setup, "status": "WAIT", "reasons": reasons,
            "entry_low": None, "entry_high": None, "entry_mid": None, "stop": None,
            "targets": [], "tp1": None, "tp2": None, "rr_net": None,
            "position_units": None, "position_notional_usd": None}


def build_trade_plans(*, side: str, features: dict, price: float, account_usd: float,
                      risk_pct: float, min_rr: float = 2.0, fees_bps: float = 12.0,
                      slippage_bps: float = 10.0) -> list[dict]:
    """Build independent PULLBACK and BREAKOUT plans from observed structure.

    Targets are historical confirmed levels, never an arbitrary price multiple.
    A nearby first obstacle below minimum net RR blocks the plan instead of being
    silently skipped in favour of a more impressive distant target.
    """
    side = str(side).upper()
    if side not in ("LONG", "SHORT"):
        return [_wait(side, setup, ["DIRECTION_NOT_ESTABLISHED"]) for setup in ("PULLBACK", "BREAKOUT")]
    values = [price, account_usd, risk_pct, min_rr]
    if any(_finite_positive(v) is None for v in values):
        return [_wait(side, setup, ["INVALID_PRICE_ACCOUNT_OR_RISK"]) for setup in ("PULLBACK", "BREAKOUT")]
    price, account_usd, risk_pct, min_rr = map(float, values)
    try:
        if isinstance(fees_bps, bool) or isinstance(slippage_bps, bool):
            raise ValueError
        fees_bps, slippage_bps = float(fees_bps), float(slippage_bps)
        if not math.isfinite(fees_bps + slippage_bps) or min(fees_bps, slippage_bps) < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return [_wait(side, setup, ["INVALID_COST_ASSUMPTIONS"]) for setup in ("PULLBACK", "BREAKOUT")]
    if risk_pct > 1.0:
        return [_wait(side, setup, ["RISK_EXCEEDS_RULEBOOK_ONE_PERCENT_BASELINE"]) for setup in ("PULLBACK", "BREAKOUT")]
    if min_rr < 2.0:
        return [_wait(side, setup, ["MINIMUM_RR_CANNOT_BE_BELOW_TWO"]) for setup in ("PULLBACK", "BREAKOUT")]
    frame = features.get("4h") or features.get("timeframes", {}).get("4h", {})
    atr = _finite_positive(frame.get("atr14"))
    if atr is None:
        return [_wait(side, setup, ["CLOSED_4H_ATR_HISTORY_UNAVAILABLE"]) for setup in ("PULLBACK", "BREAKOUT")]
    supports = sorted(set(float(x) for x in frame.get("support_levels", []) if _finite_positive(x)))
    resistances = sorted(set(float(x) for x in frame.get("resistance_levels", []) if _finite_positive(x)))
    structural_support = _finite_positive(frame.get("structural_support"))
    structural_resistance = _finite_positive(frame.get("structural_resistance"))
    if structural_support:
        supports = sorted(set(supports + [structural_support]))
    if structural_resistance:
        resistances = sorted(set(resistances + [structural_resistance]))
    plans = []
    width, buffer = atr * 0.05, atr * 0.10
    for setup in ("PULLBACK", "BREAKOUT"):
        if side == "LONG":
            if setup == "PULLBACK":
                anchors = [x for x in supports if x < price]
                anchor = anchors[-1] if anchors else None
                lo, hi = (anchor - width, anchor + width) if anchor else (None, None)
                invalidations = [x for x in supports if lo is not None and x < lo]
                invalidation = invalidations[-1] if invalidations else None
                stop = invalidation - buffer if invalidation else None
                trigger = anchor
            else:
                trigger = structural_resistance
                lo = trigger + width if trigger else None
                hi = lo + width if lo else None
                invalidations = [x for x in supports if trigger and x < trigger]
                invalidation = invalidations[-1] if invalidations else None
                stop = invalidation - buffer if invalidation else None
            targets = [x for x in resistances if hi is not None and x > hi]
            sign = 1.0
        else:
            if setup == "PULLBACK":
                anchors = [x for x in resistances if x > price]
                anchor = anchors[0] if anchors else None
                lo, hi = (anchor - width, anchor + width) if anchor else (None, None)
                invalidations = [x for x in resistances if hi is not None and x > hi]
                invalidation = invalidations[0] if invalidations else None
                stop = invalidation + buffer if invalidation else None
                trigger = anchor
            else:
                trigger = structural_support
                hi = trigger - width if trigger else None
                lo = hi - width if hi else None
                invalidations = [x for x in resistances if trigger and x > trigger]
                invalidation = invalidations[0] if invalidations else None
                stop = invalidation + buffer if invalidation else None
            targets = sorted([x for x in supports if lo is not None and x < lo], reverse=True)
            sign = -1.0
        if lo is None or hi is None or stop is None or stop <= 0:
            plans.append(_wait(side, setup, ["STRUCTURAL_ENTRY_OR_INVALIDATION_UNAVAILABLE"]))
            continue
        if not targets:
            plans.append(_wait(side, setup, ["NO_VERIFIED_TARGET_BEYOND_ACTUAL_ENTRY"]))
            continue
        entry = hi if side == "LONG" else lo
        cost = entry * (fees_bps + slippage_bps) / 10_000.0
        risk_per_unit = abs(entry - stop) + cost
        rr_net = (sign * (targets[0] - entry) - cost) / risk_per_unit
        risk_budget = account_usd * risk_pct / 100.0
        units = min(risk_budget / risk_per_unit, account_usd / entry)
        plan = {
            "side": side, "setup_type": setup, "status": "CONDITIONAL",
            "entry_low": lo, "entry_high": hi, "entry_mid": (lo + hi) / 2.0,
            "entry_for_risk": entry, "trigger_level": trigger,
            "stop": stop, "structural_invalidation": invalidation,
            "invalidation_reason": "beyond previously confirmed 4h support" if side == "LONG" else "beyond previously confirmed 4h resistance",
            "targets": targets[:2], "tp1": targets[0], "tp2": targets[1] if len(targets) > 1 else None,
            "target_reason": "historical closed 4h resistance/pivot" if side == "LONG" else "historical closed 4h support/pivot",
            "rr_net": rr_net, "rr_basis": "worst entry in zone, first target, estimated round-trip fees and slippage",
            "fees_bps": fees_bps, "slippage_bps": slippage_bps,
            "risk_budget_usd": risk_budget, "risk_pct": risk_pct,
            "position_units": units,
            "position_notional_usd": min(units * entry, account_usd),
            "risk_used_usd": units * risk_per_unit,
            "sizing_note": "research illustration capped at available account notional; no leverage assumed",
            "cost_assumptions": "estimated round-trip bps; funding and gap risk are additional",
            "created_from_close_time": frame.get("last_close_time"),
            "expires_at_ms": (frame["last_close_time"] + 28_800_000) if frame.get("last_close_time") else None,
            "reasons": ["CONDITIONAL_PLAN_IS_NOT_ENTRY_CONFIRMATION"],
            "entry_trigger": ("closed 4h breakout and closed 1h/15m confirmation, then re-evaluate current fill and net RR"
                              if setup == "BREAKOUT" else
                              "retest of confirmed structural level with closed 1h/15m confirmation; cancel if invalidation breaks"),
        }
        errors = validate_plan(plan, min_rr)
        if errors:
            plan["status"] = "WAIT"
            plan["reasons"] += errors
            plan["position_units"] = plan["position_notional_usd"] = None
        plans.append(plan)
    return plans
