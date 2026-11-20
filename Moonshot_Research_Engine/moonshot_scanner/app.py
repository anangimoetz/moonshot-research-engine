"""Authenticated research dashboard, HTTP evidence feed, and read-only MCP."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time

from flask import Flask, jsonify, redirect, request, session

from . import __version__
from .backtest import run_backtest
from .config import Settings
from .pipeline import Scanner, number, timestamp
from .plans import validate_plan
from .sessions import session_context
from .store import Store, clean_json, dumps, utc_now
from .worker import Worker


def public_snapshot(row, include_candles=False):
    row=copy.deepcopy(row)
    now_ms=int(time.time()*1000)
    row["snapshot_age_seconds"]=(now_ms-row.get("analyzed_at_ms",0))/1000
    row["recorded_status"]=row.get("status")
    row["fresh_for_entry"]=row.get("valid_until_ms",0)>=now_ms
    observed=timestamp(row.get("market",{}).get("ticker_observed_at_ms"))
    row["ticker_age_seconds"]=(now_ms-observed)/1000 if observed else None
    if not row["fresh_for_entry"]:
        row["trade_ready"]=False
        if row.get("status")=="READY":row["status"]="WATCH"
        for plan in row.get("plans",[]):
            plan["execution_status"]="WAIT"
            plan["decision"]={"status":"WAIT","ready":False,"waiting_for":["Snapshot entry validity expired; fetch a fresh review"],"probability":None}
    if not include_candles:row.get("market",{}).pop("candles",None)
    return clean_json(row)


def validate_scan(payload):
    if not isinstance(payload,dict):raise ValueError("Scan request must be a JSON object")
    exchanges=payload.get("exchanges",[payload.get("exchange","bitget")])
    if not isinstance(exchanges,list) or not exchanges or len(exchanges)>3 or any(x not in {"bitget","binance","bybit"} for x in exchanges):
        raise ValueError("Choose bitget, binance and/or bybit")
    symbols=payload.get("symbols",[])
    if isinstance(symbols,str):symbols=[x.strip() for x in re.split(r"[,\s]+",symbols.upper()) if x.strip()]
    if not isinstance(symbols,list) or len(symbols)>50:raise ValueError("At most 50 symbols per exchange")
    symbols=list(dict.fromkeys(str(x).upper() for x in symbols))
    if any(not re.fullmatch(r"[A-Z0-9]{2,24}USDT",s) for s in symbols):raise ValueError("Use explicit USDT pairs, e.g. EVAAUSDT")
    limit=number(payload.get("limit",12))
    account=number(payload.get("account_usd",1000))
    risk=number(payload.get("risk_pct",1))
    minimum=number(payload.get("min_turnover",500000));maximum=number(payload.get("max_turnover",50000000))
    if limit is None or limit!=int(limit) or not 1<=limit<=50:raise ValueError("Limit must be 1..50")
    if account is None or not 0<account<=100000000 or risk is None or not 0<risk<=1:raise ValueError("Account must be positive USD; risk must be >0 and <=1%")
    if minimum is None or maximum is None or not 0<=minimum<=maximum:raise ValueError("Turnover bounds invalid")
    return {"exchanges":list(dict.fromkeys(exchanges)),"symbols":symbols,"limit":int(limit),"account_usd":account,"risk_pct":risk,"min_turnover":minimum,"max_turnover":maximum}


def create_app(settings=None, scanner=None, start_worker=False):
    settings=(settings or Settings()).validate()
    app=Flask(__name__,static_folder="static",static_url_path="/static")
    if os.getenv("SCANNER_TRUST_PROXY","0")=="1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app=ProxyFix(app.wsgi_app,x_for=0,x_proto=1,x_host=1)
    app.secret_key=os.getenv("SCANNER_SESSION_SECRET") or hashlib.sha256((settings.token or secrets.token_hex(32)).encode()).hexdigest()
    app.config.update(MAX_CONTENT_LENGTH=2_000_000,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Strict",
                      SESSION_COOKIE_SECURE=os.getenv("SCANNER_COOKIE_SECURE","1" if os.getenv("RENDER") else "0")=="1")
    hosts=os.getenv("SCANNER_TRUSTED_HOSTS",os.getenv("RENDER_EXTERNAL_HOSTNAME","localhost,127.0.0.1")).split(",")
    app.config["TRUSTED_HOSTS"]=[x.strip() for x in hosts if x.strip()]
    store=Store(settings.state_dir/"scanner.sqlite3")
    scanner=scanner or Scanner(store)
    worker=Worker(settings,store,scanner)
    app.extensions.update(scanner_store=store,scanner_engine=scanner,scanner_worker=worker,scanner_settings=settings)

    def matching(value, expected):
        return bool(value and expected and hmac.compare_digest(str(value),str(expected)))

    def scope():
        bearer=request.headers.get("Authorization","")
        token=bearer[7:] if bearer.startswith("Bearer ") else request.args.get("token","")
        if matching(token,settings.token):return "admin"
        if matching(token,settings.read_token):return "read"
        if bearer:return None
        return "admin" if session.get("authenticated") else None

    @app.before_request
    def authorize():
        origin=request.headers.get("Origin")
        allowed={request.host_url.rstrip("/"),*os.getenv("SCANNER_ALLOWED_ORIGINS","").split(",")}
        if origin and origin.rstrip("/") not in allowed:return jsonify(error="Origin not allowed"),403
        if request.path=="/" or request.path.startswith("/static/") or request.path=="/healthz":return None
        role=scope()
        if not role:return jsonify(error="Authentication required"),401
        read_operation=request.method in {"GET","HEAD","OPTIONS"} or request.path=="/mcp"
        if not read_operation and role!="admin":return jsonify(error="Read-only token cannot mutate state"),403
        if not read_operation and not matching(request.headers.get("Authorization","").removeprefix("Bearer "),settings.token):
            csrf=request.headers.get("X-CSRF-Token","")
            if not matching(csrf,session.get("csrf")):return jsonify(error="CSRF token required"),403

    @app.after_request
    def secure_headers(response):
        response.headers["Cache-Control"]="no-store"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' wss://ws.bitget.com wss://data-stream.binance.vision wss://fstream.binance.com wss://stream.bybit.com; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        return response

    @app.get("/")
    def index():
        if request.args.get("token"):
            if matching(request.args["token"],settings.token):
                session.update(authenticated=True,csrf=secrets.token_urlsafe(24))
            return redirect("/",code=303)
        return app.send_static_file("index.html")

    @app.post("/api/login")
    def login():
        # Login is handled separately below through the public route exception.
        data=request.get_json(silent=True) or {}
        if not isinstance(data,dict):return jsonify(error="Login body must be a JSON object"),400
        if not matching(data.get("token"),settings.token):return jsonify(error="Invalid token or server token not configured"),401
        session.clear();session.update(authenticated=True,csrf=secrets.token_urlsafe(24))
        return jsonify(ok=True,csrf=session["csrf"])

    # Make only this narrowly defined credential-exchange route public; all
    # research data and mutation routes still pass through authorize().
    auth_hook=app.before_request_funcs[None][0]
    def wrapped_auth():
        if request.path=="/api/login" and request.method=="POST":
            origin=request.headers.get("Origin")
            if origin and origin.rstrip("/")!=request.host_url.rstrip("/"):return jsonify(error="Origin not allowed"),403
            return None
        return auth_hook()
    app.before_request_funcs[None][0]=wrapped_auth

    @app.get("/healthz")
    def liveness():return jsonify(status="WEB_RUNNING",version=__version__,not_a_watcher_healthcheck=True)

    def health():
        status=store.get_state("worker",{})
        beat=timestamp(status.get("heartbeat_at"))
        age=(int(time.time()*1000)-beat)/1000 if beat else None
        running=status.get("status")=="RUNNING" and age is not None and -5<=age<60
        rose=store.get_state("rose_health",{"status":"NOT_CONNECTED"})
        source_status_path=Path(os.getenv("ROSE_STATE_DIR",str(settings.state_dir/"rose")))/"status.json"
        if source_status_path.exists():
            try:
                if source_status_path.stat().st_size>100000:raise ValueError("Source status is too large")
                source_state=json.loads(source_status_path.read_text(encoding="utf-8"))
                source_beat=timestamp(source_state.get("process_heartbeat_at"))
                source_contact=timestamp(source_state.get("last_source_contact_at"))
                now_ms=int(time.time()*1000)
                connected=source_beat is not None and source_contact is not None and -5000<=now_ms-source_beat<45000 and -5000<=now_ms-source_contact<120000
                rose={"status":"CONNECTED" if connected else "STALE_OR_STOPPED","transport":source_state.get("source_transport"),"process_heartbeat_at":source_state.get("process_heartbeat_at"),"last_source_contact_at":source_state.get("last_source_contact_at"),"delivery_end_to_end_verified":False}
            except (ValueError,OSError,TypeError):rose={"status":"STATUS_READ_FAILED"}
        return {"version":__version__,"checked_at":utc_now(),"worker":{**status,"heartbeat_age_seconds":age,"healthy":running},
                "configuration":settings.public_status(),"delivery":store.outbox_status(),"rose":rose,
                "delivery_evidence":store.get_state("delivery",{"status":"NOT_TESTED"}),
                "ready_for_24_7":bool(running and settings.persistent_storage and settings.auto_scan and store.get_state("delivery",{}).get("status")=="LAST_DELIVERY_OK"),
                "data_feed":"See each source observation timestamp; web availability does not establish market-data health",
                "trading":"RESEARCH_ONLY"}

    @app.get("/api/health")
    def health_route():return jsonify(health())

    @app.get("/readyz")
    def worker_readiness():
        result=health()
        return jsonify(result),200 if result["worker"]["healthy"] else 503

    @app.get("/api/bootstrap")
    def bootstrap():
        return jsonify(version=__version__,csrf=session.get("csrf"),scope=scope(),health=health(),sessions=session_context())

    @app.post("/api/scan")
    def scan():
        try:cfg=validate_scan(request.get_json(silent=True))
        except (ValueError,TypeError) as error:return jsonify(error=str(error)),400
        with store.connection() as db:
            pending=db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('PENDING','RUNNING')").fetchone()[0]
        if pending>=10:return jsonify(error="Research queue is full; wait for existing scans"),429
        return jsonify(job_id=store.enqueue("scan",cfg)),202

    @app.get("/api/scans/<job_id>")
    def scan_status(job_id):
        job=store.job(job_id)
        if not job:return jsonify(error="Scan not found"),404
        job["results"]=[public_snapshot(x) for x in job["results"]]
        return jsonify(job)

    @app.get("/api/feed/latest")
    def latest():
        return jsonify(as_of=utc_now(),results=[public_snapshot(x) for x in store.latest()],learning=store.learning_status(),health=health())

    @app.get("/api/session")
    def sessions():return jsonify(session_context())

    @app.get("/api/market")
    def market():
        return jsonify({ex:store.get_state("market:"+ex,{"label":"NOT_FETCHED"}) for ex in settings.exchanges})

    @app.get("/api/candles")
    def candles():
        symbol=request.args.get("symbol","");exchange=request.args.get("exchange","bitget");tf=request.args.get("interval","4h").lower()
        row=next((r for r in store.latest(200) if r["symbol"]==symbol and r["exchange"]==exchange),None)
        if not row:return jsonify(status="NOT_SCANNED",candles=[]),404
        return jsonify(symbol=symbol,exchange=exchange,product=row["market"].get("product"),timeframe=tf,candles=row["market"].get("candles",{}).get(tf,[]),snapshot_analyzed_at=row["analyzed_at"],status="SAVED_SNAPSHOT")

    @app.get('/api/chart/history')
    def chart_history():
        from .chart_data import history
        try:
            data=history(request.args.get('exchange','bitget'),request.args.get('symbol',''),request.args.get('product','SPOT'),request.args.get('interval','4h'))
            return jsonify(data),200 if data['candles'] else 502
        except ValueError as error:return jsonify(error=str(error)),400

    @app.get("/api/watch")
    def watches():return jsonify(items=store.watches())

    @app.post("/api/watch")
    def add_watch():
        data=request.get_json(silent=True) or {}
        if not isinstance(data,dict):return jsonify(error="Watch body must be a JSON object"),400
        row=next((r for r in store.latest(200) if r["symbol"]==data.get("symbol") and r["exchange"]==data.get("exchange")),None)
        if not row:return jsonify(error="Scan this asset first"),400
        plan=next((p for p in row.get("plans",[]) if p.get("setup_type")==data.get("setup_type")),None)
        if not plan or plan.get("builder_status",plan.get("status"))!="CONDITIONAL" or validate_plan(plan):return jsonify(error="No structurally valid conditional plan to watch"),400
        if (timestamp(plan.get("expires_at_ms")) or 0)<=int(time.time()*1000):return jsonify(error="Plan expired; scan again"),400
        return jsonify(id=store.add_watch(row["exchange"],row["symbol"],plan)),201

    @app.delete("/api/watch/<watch_id>")
    def remove_watch(watch_id):store.delete_watch(watch_id);return jsonify(ok=True)

    @app.get("/api/learn/stats")
    def learning():return jsonify(store.learning_status())

    @app.get("/api/evidence/<symbol>")
    def evidence(symbol):return jsonify(items=store.evidence(symbol),policy="Operator-reviewed evidence; a URL alone does not verify a claim")

    @app.post("/api/evidence")
    def add_evidence():
        try:key=store.add_evidence(request.get_json(silent=True) or {})
        except (ValueError,TypeError,KeyError) as error:return jsonify(error=str(error)),400
        return jsonify(id=key,provenance="OPERATOR_REVIEWED_SOURCE"),201

    @app.post("/api/backtest/replay")
    def replay():
        data=request.get_json(silent=True) or {}
        if not isinstance(data,dict):return jsonify(error="Replay body must be a JSON object"),400
        bars=data.get("candles",[]);signals=data.get("signals")
        if not isinstance(bars,list) or len(bars)>5000 or (signals is not None and (not isinstance(signals,list) or len(signals)>5000)):
            return jsonify(error="At most 5000 candles and signals"),400
        if any(not isinstance(x,dict) for x in bars) or (signals is not None and any(not isinstance(x,dict) for x in signals)):
            return jsonify(error="Each candle and signal must be a JSON object"),400
        try:return jsonify(clean_json(run_backtest(bars,signals=signals)))
        except (ValueError,TypeError,KeyError,IndexError):return jsonify(error="Invalid replay schema; supply explicit historical signals and closed candles"),400

    @app.get("/api/rose/recent")
    def rose_recent():return jsonify(events=store.recent_rose(),health=health()["rose"])

    tools={
        "market_latest":("Saved independent scanner results with timestamps, gaps and entry expiry",lambda args:{"as_of":utc_now(),"results":[public_snapshot(x) for x in store.latest()]}),
        "scanner_health":("Actual worker, delivery and source-connection status",lambda args:health()),
        "session_brief":("Armenia/WIB session map and next DST-aware run times",lambda args:session_context()),
        "rose_recent":("Stored Rose audits; missing watcher is explicitly reported",lambda args:{"events":store.recent_rose(),"health":health()["rose"]}),
    }

    @app.route("/mcp",methods=["GET","POST","DELETE"])
    def mcp():
        if request.method!="POST":return jsonify(error="This stateless MCP endpoint supports POST; no SSE session is offered"),405
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or body.get("jsonrpc")!="2.0":return jsonify(jsonrpc="2.0",id=None,error={"code":-32600,"message":"Invalid JSON-RPC request"}),400
        msg_id=body.get("id");method=body.get("method");params=body.get("params",{})
        if not isinstance(params,dict):return jsonify(jsonrpc="2.0",id=msg_id,error={"code":-32602,"message":"Invalid params"}),400
        if "id" not in body:
            return ("",202) if method=="notifications/initialized" else (jsonify(error="Notification not supported"),400)
        protocol=request.headers.get("MCP-Protocol-Version")
        if protocol and protocol not in {"2025-06-18","2025-03-26"}:return jsonify(error="Unsupported MCP protocol version"),400
        if method=="initialize":
            requested=params.get("protocolVersion")
            result={"protocolVersion":requested if requested in {"2025-06-18","2025-03-26"} else "2025-06-18","capabilities":{"tools":{"listChanged":False}},"serverInfo":{"name":"moonshot-research-readonly","version":__version__},"instructions":"Read-only evidence, not trading. Preserve source timestamps, missing-data flags and separation of independent discovery from Rose claims."}
        elif method=="ping":result={}
        elif method=="tools/list":
            result={"tools":[{"name":name,"description":value[0],"inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False}} for name,value in tools.items()]}
        elif method=="tools/call":
            name=params.get("name")
            if not isinstance(name,str) or name not in tools:return jsonify(jsonrpc="2.0",id=msg_id,error={"code":-32602,"message":"Unknown read-only tool"})
            args=params.get("arguments",{})
            if not isinstance(args,dict) or args:return jsonify(jsonrpc="2.0",id=msg_id,error={"code":-32602,"message":"This tool accepts an empty arguments object"})
            result={"content":[{"type":"text","text":dumps(tools[name][1](args))}],"isError":False}
        else:return jsonify(jsonrpc="2.0",id=msg_id,error={"code":-32601,"message":"Method not found"})
        return jsonify(jsonrpc="2.0",id=msg_id,result=result)

    if start_worker:
        if not worker.start():store.set_state("web_worker_start",{"status":"LOCK_ALREADY_HELD","at":utc_now()})
    return app
