"""Discovery and execution are separate, fail-closed decisions.

All ranks are heuristics. Nothing here estimates a calibrated win probability.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import math
import statistics
import threading
import time

from .features import analyze_features
from .plans import build_trade_plans, validate_plan
from .providers import PublicMarketData
from .store import utc_now


def number(value):
    if isinstance(value,bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def timestamp(value):
    try:
        if isinstance(value,bool):
            return None
        if isinstance(value,(float,int)):
            return int(value if value > 100000000000 else value * 1000) if math.isfinite(value) else None
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(value.timestamp() * 1000) if value.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def correlation(asset_candles, benchmark_candles):
    def returns(candles):
        xs = sorted((x for x in candles if x.get("is_closed")), key=lambda x:x["open_time"])
        result = {}
        for a,b in zip(xs, xs[1:]):
            if a["close"] > 0 and b["open_time"]-a["open_time"] == 14400000:
                result[b["close_time"]] = b["close"] / a["close"] - 1
        return result
    a,b = returns(asset_candles), returns(benchmark_candles)
    keys = sorted(set(a)&set(b))[-126:]
    if len(keys) < 42:
        return {"status":"HISTORY_INSUFFICIENT","n":len(keys),"correlation":None,"beta":None,"residual_24h_pct":None}
    av,bv = [a[k] for k in keys],[b[k] for k in keys]
    ma,mb = statistics.mean(av),statistics.mean(bv)
    cov = sum((x-ma)*(y-mb) for x,y in zip(av,bv))
    va,vb = sum((x-ma)**2 for x in av),sum((x-mb)**2 for x in bv)
    beta = cov/vb if vb else None
    corr = cov/math.sqrt(va*vb) if va and vb else None
    return {"status":"AVAILABLE","n":len(keys),"interval":"4h","correlation":corr,"beta":beta,
            "residual_24h_pct":sum(a[k]-beta*b[k] for k in keys[-6:])*100 if beta is not None else None,
            "note":"Synchronized return regression; residual strength does not imply crash immunity or independence."}


def frame_bias(frame):
    if not frame:
        return "UNKNOWN"
    price = number(frame.get("last_close"))
    e13,e21 = number(frame.get("ema13")),number(frame.get("ema21"))
    if None in (price,e13,e21):
        return "UNKNOWN"
    return "LONG" if price > e13 > e21 else "SHORT" if price < e13 < e21 else "NEUTRAL"


def source_age(market, now_ms):
    observed = timestamp(market.get("ticker_observed_at_ms",market.get("observed_at_ms")))
    return (now_ms-observed)/1000 if observed is not None else None


def unlock_review(evidence, symbol=None):
    rows = [e for e in evidence if e.get("kind")=="unlock" and e.get("symbol")!="*" and (symbol is None or e.get("symbol")==symbol) and e.get("verified") and e.get("fresh")]
    if not rows:
        return {"status":"UNKNOWN","classification":"UNKNOWN","source_url":None}
    row = rows[0]
    facts = row["facts"]
    pct = number(facts.get("circulating_pct_unlock_30d"))
    lookahead = number(facts.get("lookahead_days"))
    if pct is None or pct < 0 or lookahead is None or lookahead < 30 or not facts.get("recipients"):
        return {"status":"INCOMPLETE","classification":"UNKNOWN","source_url":row["source_url"]}
    classification = "HIGH SELL-OVERHANG" if pct >= 5 else "MODERATE" if pct >= 1 else "LOW"
    if facts.get("active_distribution"):
        # A release schedule cannot establish that selling actually happened.
        transfers = [e for e in evidence if e.get("kind")=="whale_transfer" and e.get("symbol")==row.get("symbol") and e.get("verified") and e.get("fresh")
                     and e.get("facts",{}).get("transaction_hash") and e.get("facts",{}).get("recipient_exchange_verified")]
        if transfers:
            classification = "ACTIVE DISTRIBUTION CONFIRMED"
    return {"status":"REVIEWED","classification":classification,"pct_circulating_30d":pct,
            "source_url":row["source_url"],"recipients":facts["recipients"],
            "provenance":"OPERATOR_REVIEWED_SOURCE","note":"Threshold classes are risk heuristics; transfers alone do not prove an executed sale."}


def readiness(plan, market, features, regime, enrichment, evidence, now_ms, *, created_after_ms=None):
    reasons = list(validate_plan(plan,min_rr=2.0))
    if plan.get("builder_status",plan.get("status"))!="CONDITIONAL":
        reasons.append("Plan builder did not produce a valid conditional plan")
    expiry=timestamp(plan.get("expires_at_ms"))
    if expiry is None or expiry<=now_ms:
        reasons.append("Plan expiry is missing or passed; create a newly reviewed plan")
    if not plan.get("invalidation_reason") or not plan.get("target_reason"):
        reasons.append("Structural invalidation or target rationale missing")
    side = plan.get("side")
    frames = features.get("timeframes",{})
    age = source_age(market, now_ms)
    if age is None or not -5 <= age <= 120:
        reasons.append("Ticker timestamp unavailable, stale or in the future")
    if market.get("spot_available",market.get("product")=="SPOT") is not True:
        reasons.append("Spot-demand confirmation unavailable; perpetual turnover is not spot turnover")
    for tf,ms in (("4h",14400000),("1h",3600000),("15m",900000)):
        frame = frames.get(tf,{})
        close = timestamp(frame.get("last_close_time",frame.get("close_time")))
        if close is None or not 0 <= now_ms-close <= ms+120000:
            reasons.append(tf+" completed-candle feed is missing, stale or discontinuous")
        if frame_bias(frame) != side:
            reasons.append(tf+" closed-candle direction does not confirm the plan")
        if frame.get("data_gaps") or (number(frame.get("count_closed")) or 0)<21:
            reasons.append(tf+" candle continuity or required indicator history is incomplete")
    if side=="LONG" and regime.get("label") in {"RISK_OFF","UNKNOWN"}:
        reasons.append("BTC/ETH hostile or unknown regime blocks a new alt long")
    identity = enrichment.get("identity",{})
    if identity.get("status") != "VERIFIED_PROJECT":
        reasons.append("Project identity has not been verified")
    fund = enrichment.get("fundamentals",{})
    if (number(fund.get("market_cap_usd",fund.get("market_cap"))) or 0)<=0:
        reasons.append("Market-cap and fundamental coverage incomplete")
    fund_observed = timestamp(fund.get("observed_at_ms",fund.get("observed_at",fund.get("last_updated"))))
    if fund_observed is None or not 0 <= now_ms-fund_observed <= 86400000:
        reasons.append("Fundamental observation timestamp unavailable or older than 24h")
    supply = unlock_review(evidence,market.get("symbol"))
    if supply["status"] != "REVIEWED":
        reasons.append("Verified 30-day unlock/recipient review missing")
    elif side=="LONG" and supply["classification"] in {"HIGH SELL-OVERHANG","ACTIVE DISTRIBUTION CONFIRMED"}:
        reasons.append("Supply overhang blocks a new long pending a reviewed exception")
    deriv = market.get("derivatives",{})
    if deriv.get("available") is not True or deriv.get("product")!="USDT-FUTURES":
        reasons.append("A live perpetual contract on this venue has not been verified")
    funding = number(deriv.get("funding_rate"))
    interval = number(deriv.get("funding_interval_hours"))
    if funding is None or interval is None or not 0<interval<=24:
        reasons.append("Perpetual funding and actual settlement interval missing")
    elif (side=="LONG" and funding*8/interval>.0003) or (side=="SHORT" and funding*8/interval<-.0003):
        reasons.append("Funding indicates crowded positioning for the proposed side")
    funding_observed=timestamp(deriv.get("funding_observed_at_ms"))
    if funding_observed is None:
        # This is a visible fetch-time proxy for an explicitly current-rate API,
        # never an invented exchange event timestamp.
        funding_source=next((s for s in market.get("sources",[]) if s.get("name")=="funding" and s.get("status") in {"OK","AVAILABLE"}),{})
        funding_observed=timestamp(funding_source.get("fetched_at_ms"))
    if funding_observed is None or not -5000<=now_ms-funding_observed<=120000:
        reasons.append("Current published funding snapshot is missing or stale")
    next_funding=timestamp(deriv.get("next_funding_time"))
    if next_funding is None or next_funding<=now_ms:
        reasons.append("Next funding settlement is unknown or passed")
    windows=deriv.get("oi_windows",{})
    if any(windows.get(k,{}).get("status")!="AVAILABLE" for k in ("4h","24h")):
        reasons.append("Aligned base-unit OI 4h/24h history is warming up or unavailable")
    oi_time=timestamp(windows.get("observed_at_ms"))
    if windows.get("unit")!="BASE_COIN" or (number(windows.get("base_units")) or 0)<=0 or oi_time is None or not -5000<=now_ms-oi_time<=360000:
        reasons.append("Current base-unit OI snapshot is missing, invalid or stale")
    else:
        for hours in (4,24):
            w=windows.get(str(hours)+"h",{})
            baseline=timestamp(w.get("baseline_at_ms"))
            if number(w.get("change_pct")) is None or baseline is None or abs((oi_time-baseline)-hours*3600000)>600000:
                reasons.append(str(hours)+"h OI baseline is not aligned to the declared window")
    book = deriv.get("orderbook",{})
    spread = number(book.get("spread_bps"))
    book_age = timestamp(book.get("observed_at_ms"))
    if book_age is None or not -5000<=now_ms-book_age<=30000 or spread is None or not 0<=spread<=30:
        reasons.append("Fresh perpetual depth/spread confirmation missing or spread excessive")
    slip = number(book.get("slippage_buy_bps" if side=="LONG" else "slippage_sell_bps"))
    book_notional = number(book.get("reference_notional_usd"))
    plan_notional = number(plan.get("position_notional_usd",plan.get("position_notional")))
    if slip is None or not 0<=slip<=50 or book_notional is None or plan_notional is None or plan_notional<=0 or book_notional<plan_notional:
        reasons.append("Executable slippage for the planned notional is unverified or excessive")
    elif number(plan.get("slippage_bps")) is not None:
        # Both entry and exit are charged this conservative live-depth proxy.
        live_cost_plan={**plan,"slippage_bps":max(plan["slippage_bps"],2*slip)}
        if validate_plan(live_cost_plan,min_rr=2.0):
            reasons.append("Observed executable slippage reduces net RR below the minimum")
    ratio = number(features.get("turnover_ratio_4h"))
    if ratio is None or ratio<1.3:
        reasons.append("Completed spot 4h turnover does not confirm demand")
    current = number(market.get("price"))
    lo,hi = number(plan.get("entry_low")),number(plan.get("entry_high"))
    close15 = frames.get("15m",{})
    closed_trigger = number(close15.get("last_close"))
    if current is None or lo is None or hi is None or not lo<=current<=hi:
        reasons.append("Current price has not entered the immutable plan zone; do not chase")
    if closed_trigger is None or lo is None or hi is None or not lo<=closed_trigger<=hi:
        reasons.append("15m trigger has not closed inside the entry zone")
    if created_after_ms is not None and (timestamp(close15.get("last_close_time",close15.get("close_time"))) or 0)<=created_after_ms:
        reasons.append("No new completed trigger candle since watch creation")
    rsi = number(frames.get("4h",{}).get("rsi14"))
    change = number(market.get("change_24h_pct"))
    if side=="LONG" and ((rsi is not None and rsi>=75) or (change is not None and change>=25)):
        reasons.append("Overheated / vertical expansion; wait for a new base")
    if side=="SHORT" and ((rsi is not None and rsi<=25) or (change is not None and change<=-25)):
        reasons.append("Oversold / vertical decline; do not chase the short")
    reasons = list(dict.fromkeys(reasons))
    return {"status":"READY" if not reasons else "WAIT","ready":not reasons,"waiting_for":reasons,
            "probability":None,"scope":"Research setup validation, not a profit prediction or order."}


class Scanner:
    def __init__(self, store, provider_factory=PublicMarketData):
        self.store = store
        self.provider_factory = provider_factory
        self._market_cache = {}
        self._market_lock = threading.Lock()
        self._providers={}
        self._provider_lock=threading.Lock()

    def provider(self,exchange):
        with self._provider_lock:
            if exchange not in self._providers:self._providers[exchange]=self.provider_factory(exchange=exchange)
            return self._providers[exchange]

    def market_context(self, exchange, now_ms=None):
        live_clock=now_ms is None
        now_ms = now_ms or int(time.time()*1000)
        with self._market_lock:
            old = self._market_cache.get(exchange)
            if old and 0<=now_ms-old["computed_at_ms"]<60000:
                return old
            assets={}
            for sym in ("BTCUSDT","ETHUSDT"):
                try:
                    data=self.provider(exchange).collect(sym,now_ms=now_ms)
                    checked_ms=int(time.time()*1000) if live_clock else now_ms
                    feat=analyze_features(data.get("candles",{}),checked_ms)
                    frame=feat.get("timeframes",{}).get("4h",{})
                    closed=timestamp(frame.get("last_close_time"))
                    age=source_age(data,checked_ms)
                    healthy=closed is not None and 0<=checked_ms-closed<=14520000 and age is not None and -5<=age<=120 and not frame.get("data_gaps")
                    assets[sym]={"data":data,"features":feat,"bias":frame_bias(frame) if healthy else "UNKNOWN"}
                except Exception as err:
                    assets[sym]={"bias":"UNKNOWN","error":type(err).__name__}
            biases=[a["bias"] for a in assets.values()]
            label="RISK_OFF" if "SHORT" in biases else "UNKNOWN" if "UNKNOWN" in biases else "RISK_ON" if biases==["LONG","LONG"] else "MIXED"
            result={"label":label,"assets":assets,"computed_at_ms":now_ms,"method":"BTC and ETH completed 4h EMA13/21 direction; a time window has no fixed directional score."}
            self._market_cache[exchange]=result
            if self.store:self.store.set_state("market:"+exchange,result)
            return result

    def analyze_asset(self,symbol,exchange="bitget",*,account_usd=1000,risk_pct=1.0,context=None,now_ms=None,origin="INDEPENDENT"):
        live_clock=now_ms is None
        now_ms=int(now_ms or time.time()*1000)
        provider=self.provider(exchange)
        market=provider.collect(symbol,now_ms=now_ms)
        features=analyze_features(market.get("candles",{}),now_ms)
        enrichment=provider.enrich_identity(symbol,market,now_ms=now_ms)
        context=context or self.market_context(exchange,None if live_clock else now_ms)
        if live_clock:
            now_ms=int(time.time()*1000)
        evidence=self.store.evidence(symbol,datetime.fromtimestamp(now_ms/1000,timezone.utc))
        supply=unlock_review(evidence,symbol)
        deriv=market.get("derivatives",{})
        oi=deriv.get("oi_snapshot",{})
        if not oi:
            oi={"base_units":deriv.get("open_interest_base_units"),"observed_at_ms":deriv.get("oi_observed_at_ms"),"unit":deriv.get("oi_unit")}
        if isinstance(oi,dict):
            base=number(oi.get("base_units"))
            observed=timestamp(oi.get("observed_at_ms"))
            if base and observed and oi.get("unit")=="BASE_COIN":
                self.store.add_oi(exchange,symbol,observed,base)
                deriv["oi_windows"]=self.store.oi_windows(exchange,symbol,observed,base)
        side=frame_bias(features.get("timeframes",{}).get("4h",{}))
        plans=[]
        if side in {"LONG","SHORT"} and number(market.get("price")):
            plans=build_trade_plans(side=side,features=features,price=market["price"],account_usd=account_usd,risk_pct=risk_pct)
            for plan in plans:
                plan["builder_status"]=plan["status"]
                decision=readiness(plan,market,features,context,enrichment,evidence,now_ms)
                plan["decision"]=decision
                plan["execution_status"]=decision["status"]
        ratio=number(features.get("turnover_ratio_4h"))
        price_change=number(features.get("price_change_24h_pct",market.get("change_24h_pct")))
        early=None if ratio is None or price_change is None else ((ratio>=3 and price_change<15) or (ratio>=5 and price_change<25))
        stage="READY" if any(p.get("decision",{}).get("ready") for p in plans) else "EARLY" if early else "WATCH"
        gaps=list(features.get("data_gaps",[]))
        if enrichment.get("identity",{}).get("status")!="VERIFIED_PROJECT":gaps.append("Project identity unknown; independent verification required")
        if supply["status"]!="REVIEWED":gaps.append("Token unlock/recipient review unavailable")
        if not enrichment.get("fundamentals",{}).get("market_cap_usd"):gaps.append("Market-cap/fundamental data unavailable")
        for error in market.get("errors",[])+enrichment.get("errors",[]):gaps.append(str(error))
        coverage_fields=[number(market.get("price")) is not None,ratio is not None,side!="UNKNOWN",
                         enrichment.get("identity",{}).get("status")=="VERIFIED_PROJECT",
                         number(enrichment.get("fundamentals",{}).get("market_cap_usd")) is not None,
                         supply["status"]=="REVIEWED",enrichment.get("onchain",{}).get("status")=="VERIFIED_POOLS",
                         number(deriv.get("funding_rate")) is not None]
        activity=min(60,max(0,ratio or 0)*12)+ (20 if features.get("timeframes",{}).get("4h",{}).get("is_compressed") else 0)+ (20 if early else 0)
        correlations={}
        for name,base in context.get("assets",{}).items():
            correlations[name]=correlation(market.get("candles",{}).get("4h",[]),base.get("data",{}).get("candles",{}).get("4h",[]))
        return {"schema_version":3,"origin":origin,"symbol":symbol,"exchange":exchange,"status":stage,
                "early_discovery":early,"trade_ready":stage=="READY","market_bias":side,"probability":None,
                "activity_score":round(activity),"score_type":"HEURISTIC_ACTIVITY_NOT_UPSIDE_PROBABILITY",
                "data_coverage_pct":round(sum(coverage_fields)/len(coverage_fields)*100),
                "market":market,"features":features,"enrichment":enrichment,"supply_review":supply,"evidence":evidence,
                "regime":{"label":context.get("label","UNKNOWN"),"method":context.get("method")},
                "correlations":correlations,"plans":plans,"data_gaps":list(dict.fromkeys(map(str,gaps))),
                "analyzed_at":utc_now(),"analyzed_at_ms":now_ms,"ticker_age_seconds":source_age(market,now_ms),
                "valid_until_ms":min((timestamp(market.get("ticker_observed_at_ms")) or 0)+120000,(timestamp(deriv.get("orderbook",{}).get("observed_at_ms")) or 0)+30000),
                "note":"Discovery is independent of Rose. Missing data never becomes a bullish fact; no order is placed."}

    def execute_scan(self,job,progress=None):
        cfg=job["payload"]
        exchanges=cfg.get("exchanges",["bitget"])
        all_results=[]; errors=[]; count=0
        for exchange in exchanges:
            if progress:progress(exchange,"universe")
            try:
                provider=self.provider(exchange)
                symbols=cfg.get("symbols",[])
                if not symbols:
                    cursor=self.store.get_state("universe_cursor:"+exchange,0)
                    discovered=provider.discover(limit=cfg.get("limit",12),min_turnover=cfg.get("min_turnover",500000),max_turnover=cfg.get("max_turnover",50000000),cursor=cursor)
                    for failure in getattr(provider,"last_discovery",{}).get("errors",[]):
                        self.store.add_error(job["id"],exchange,None,str(failure))
                        errors.append({"exchange":exchange,"error":str(failure)})
                    symbols=[x["symbol"] for x in discovered]
                    self.store.set_state("universe_cursor:"+exchange,cursor+len(symbols))
                context=self.market_context(exchange)
            except Exception as err:
                self.store.add_error(job["id"],exchange,None,type(err).__name__)
                errors.append({"exchange":exchange,"error":type(err).__name__})
                continue
            count+=len(symbols)
            with ThreadPoolExecutor(max_workers=3) as pool:
                tasks={pool.submit(self.analyze_asset,s,exchange,account_usd=cfg.get("account_usd",1000),risk_pct=cfg.get("risk_pct",1.0),context=context):s for s in symbols}
                for task in as_completed(tasks):
                    symbol=tasks[task]
                    try:
                        data=task.result()
                        self.store.add_snapshot(job["id"],data)
                        all_results.append(data)
                    except Exception as err:
                        error={"exchange":exchange,"symbol":symbol,"error":type(err).__name__}
                        self.store.add_error(job["id"],exchange,symbol,error["error"])
                        errors.append(error)
                    if progress:progress(exchange,symbol)
                    self.store.renew(job["id"])
        source_errors=sum(len(x["market"].get("errors",[]))+len(x["enrichment"].get("errors",[])) for x in all_results)
        result={"attempted":count,"returned":len(all_results),"errors":len(errors),"source_errors":source_errors,"ready":sum(x["trade_ready"] for x in all_results),
                "early":sum(x["early_discovery"] is True for x in all_results),"status":"PARTIAL" if errors or source_errors else "COMPLETE",
                "no_call_is_valid":True,"probability":None}
        return result
