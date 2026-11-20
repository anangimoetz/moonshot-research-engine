"""Bridge the persistent Rose source listener to the repaired evidence engine.

Channel text is untrusted evidence, never an instruction. Rose reviews are
stored separately from independently discovered candidates.
"""
from datetime import datetime,timezone
import hashlib
import threading
import time

from .config import Settings
from .pipeline import Scanner, number, timestamp
from .plans import validate_plan
from .store import Store, utc_now

_lock=threading.Lock()
_scanner=None
_store=None


def _resources():
    global _scanner,_store
    with _lock:
        if _store is None:
            cfg=Settings()
            _store=Store(cfg.state_dir/"scanner.sqlite3")
            _scanner=Scanner(_store)
        return _scanner,_store


def analyze_signal(signal,*,http_get=None,now_ms=None,scanner=None,store=None):
    started=time.monotonic()
    now_ms=int(now_ms or time.time()*1000)
    output={"schema_version":3,"origin":"ROSE_REVIEW","post_id":signal.get("post_id",signal.get("message_id")),
            "signal_url":signal.get("url"),"analyzed_utc":utc_now(),"status":"WAIT","probability":None,
            "classification":"NON_CALL","candidates":[],"reasons":[],"scope":"Public research only; no order is executed"}
    published=timestamp(signal.get("published_utc"))
    output["signal_published_utc"]=signal.get("published_utc")
    output["signal_age_seconds"]=(now_ms-published)/1000 if published else None
    try:
        if store is None:store=_resources()[1]
        if scanner is None:
            if http_get is not None:
                from .providers import PublicMarketData
                scanner=Scanner(store,provider_factory=lambda exchange:PublicMarketData(exchange=exchange,http_get=http_get))
            else:scanner=_resources()[0]
        symbols=signal.get("symbols",[])
        if not isinstance(symbols,list):symbols=[]
        import re
        symbols=list(dict.fromkeys(str(s).upper().removesuffix("USDT") for s in symbols if re.fullmatch(r"[A-Za-z0-9]{2,24}(?:USDT)?",str(s))))[:2]
        direction=signal.get("direction")
        if published is None or output["signal_age_seconds"] < -5 or output["signal_age_seconds"]>900:
            output.update(classification="EXPIRED_OR_TIMESTAMP_REVIEW")
            output["reasons"].append("Post is older than 15 minutes or has an invalid timestamp; do not turn it into a fresh entry")
        elif signal.get("chart_review_required") or signal.get("image_only"):
            output["classification"]="CHART_REVIEW_REQUIRED"
            output["reasons"].append("Chart-only levels have not been read; no entry, stop or target is invented")
        else:
            from rose_monitor.engine import _explicit_plan
            explicit=_explicit_plan(signal.get("text",""),direction)
            complete=bool(len(symbols)==1 and direction in {"long","short"} and explicit["entry"] and explicit["stop"] and explicit["targets"] and not explicit["ambiguous_levels"])
            output["channel_plan"]=explicit
            if not complete:
                output["reasons"].append("No unambiguous single-asset text plan with entry, stop, target and direction")
            else:
                output["classification"]="EXPLICIT_TEXT_CALL"
                plan={"side":direction.upper(),"entry_low":explicit["entry"],"entry_high":explicit["entry"],"stop":explicit["stop"],"targets":explicit["targets"],"fees_bps":12,"slippage_bps":10}
                invalid=validate_plan(plan)
                if invalid:output["status"]="REJECT";output["reasons"].extend(invalid)
            for symbol in symbols:
                snapshot=scanner.analyze_asset(symbol+"USDT","bitget",origin="ROSE_REVIEW")
                # Exact publication-time price is not available from a latest tick.
                snapshot["call_time_price"]={"status":"UNAVAILABLE","value":None,"note":"Latest observed ticker is not an exact publication-time fill"}
                output["candidates"].append(snapshot)
            if complete and output["status"]!="REJECT":
                output["status"]="WATCH"
                output["reasons"].append("Channel levels require a separate structural review; independent technical readiness does not validate the channel's entire plan")
                if not any(c.get("trade_ready") for c in output["candidates"]):
                    output["status"]="WAIT"
                    output["reasons"].append("Independent source, regime, supply, positioning or trigger gates are incomplete")
    except Exception as error:
        output["status"]="WAIT"
        output["reasons"].append("Analysis error: "+type(error).__name__)
    output["processing_latency_seconds"]=round(time.monotonic()-started,3)
    output["analysis_finished_utc"]=utc_now()
    if store:
        key=str(output["post_id"])+":"+hashlib.sha256((signal.get("text","")+str(signal.get("edit_utc",""))).encode()).hexdigest()[:16]
        try:store.record_rose(key,output,signal.get("published_utc"))
        except Exception:
            output["status"]="WAIT"
            output["reasons"].append("PERSISTENCE_FAILURE: scanner evidence could not be saved; review monitor logs")
    return output


def format_analysis(result,max_symbols=2):
    now=datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo
    amt=now.astimezone(ZoneInfo("Asia/Yerevan")).strftime("%d %b %H:%M:%S AMT")
    wib=now.astimezone(ZoneInfo("Asia/Jakarta")).strftime("%H:%M:%S WIB")
    lines=[f"ROSE REVIEW | {result['status']} | {result['classification']}",f"{amt} ({wib})",str(result.get("signal_url") or ""),
           f"Umur post: {result.get('signal_age_seconds')}s | Proses: {result.get('processing_latency_seconds')}s"]
    for c in result.get("candidates",[])[:max_symbols]:
        lines.append(f"INDEPENDENT RESEARCH: {c['symbol']} | {c['exchange']} | {c['status']} | Regime {c['regime']['label']}")
        lines.append(f"Harga observasi: {c['market'].get('price')} | timestamp {c['market'].get('ticker_observed_at_ms')} | cakupan {c['data_coverage_pct']}%")
        lines.append("Gap: "+"; ".join(c.get("data_gaps",[])[:4]))
    lines.extend(result.get("reasons",[])[:5])
    lines.append("Angka peluang profit belum terkalibrasi. Tidak ada order dieksekusi.")
    return "\n".join(lines)[:3900]
