"""Independent scan/schedule/watch/delivery loops; durable at-least-once reports.

Queue deduplication cannot prevent Telegram duplicates after remote send success
and local acknowledgement failure. Source Telegram authorization is separate.
"""
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import threading
import time
import uuid
import requests
from .pipeline import readiness, timestamp, number, source_age
from .sessions import session_context, next_session_runs
from .store import dumps, utc_now


class WorkerLock:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, IOError):
            self.file.close()
            self.file = None
            return False

    def close(self):
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def message_text(kind, data):
    event = "\nEvent ID: " + str(data.get("event_id", data.get("job_id", "unknown")))
    if kind == "session":
        summary = data.get("summary", {})
        return ("SESSION BRIEF | " + data["time"]["display"] + event + "\n"
                + "Scan: " + str(summary.get("returned", 0)) + " hasil, "
                + str(summary.get("errors", 0)) + " error; "
                + str(summary.get("early", 0)) + " EARLY, " + str(summary.get("ready", 0)) + " READY.\n"
                + "Delay dari jadwal: " + str(round(data.get("delay_seconds", 0))) + "s\n"
                + "Regime: " + ", ".join(data.get("regimes", [])) + "\n"
                + "Kandidat: " + ", ".join(data.get("candidates", [])) + "\n"
                + "Discovery ≠ entry. Tidak ada probabilitas profit terkalibrasi. Lihat dashboard untuk waktu tiap sumber dan gap data.")
    if kind == "watch":
        return ("WATCH " + data["status"] + " | " + data["exchange"] + " " + data["symbol"]
                + event + "\n" + data.get("summary", "") + "\nTidak ada order yang dieksekusi.")
    return "SCANNER STATUS | " + json.dumps(data, ensure_ascii=False)[:3000]


class Worker:
    def __init__(self, settings, store, scanner):
        self.settings, self.store, self.scanner = settings, store, scanner
        self.stop = threading.Event()
        self.lock = WorkerLock(settings.state_dir / "worker.lock")
        self.thread = None
        self.threads = {}
        self.clock = lambda: datetime.now(timezone.utc)
        self._health_lock = threading.Lock()
        self._component_state, self._active, self._failed = {}, 0, False

    def _delivery_channels(self):
        return [name for name, enabled in (("telegram", self.settings.bot_token and self.settings.chat_id),
                ("webhook", self.settings.webhook_url)) if enabled]

    def heartbeat(self, **extra):
        with self._health_lock:
            status = "FAILED" if self._failed else "STOPPING" if self.stop.is_set() else "RUNNING"
            components = {name: dict(state) for name, state in self._component_state.items()}
        self.store.set_state("worker", {"status": status, "pid": os.getpid(), "heartbeat_at": utc_now(),
            "components": components, "delivery_channels": self._delivery_channels(),
            "delivery_status": "CONFIGURED_NOT_VERIFIED" if self._delivery_channels() else "UNCONFIGURED",
            "delivery_semantics": "AT_LEAST_ONCE", "rose_watcher": "NOT_VERIFIED", **extra})

    def _guard(self, name, function):
        with self._health_lock:
            self._component_state[name] = {"status": "RUNNING", "started_at": utc_now()}
        try:
            function()
            if not self.stop.is_set():
                raise RuntimeError("Worker loop exited unexpectedly")
        except BaseException as error:
            with self._health_lock:
                self._failed = True
                self._component_state[name] = {"status": "FAILED", "error": type(error).__name__, "at": utc_now()}
            self.stop.set()
            self.heartbeat(failed_component=name, error=type(error).__name__)
        finally:
            with self._health_lock:
                if self._component_state.get(name, {}).get("status") != "FAILED":
                    self._component_state[name] = {"status": "STOPPED", "at": utc_now()}
                self._active -= 1
                last = self._active == 0
            if last:
                if self._failed:
                    self.heartbeat()
                else:
                    self.store.set_state("worker", {"status": "STOPPED", "heartbeat_at": utc_now(), "components": self._component_state})
                self.lock.close()

    def start(self):
        if any(t.is_alive() for t in self.threads.values()):
            return not self.stop.is_set()
        if self.thread and self.thread.is_alive():
            return True
        if not self.lock.acquire():
            return False
        try:
            self.store.recover_worker()
            self.stop.clear()
            self._failed = False
            loops = {"scan": self.run, "scheduler": self._schedule_loop,
                     "watch": self._watch_loop, "delivery": self._delivery_loop}
            with self._health_lock:
                self._active = len(loops)
                self._component_state = {name: {"status": "STARTING"} for name in loops}
            self.threads = {name: threading.Thread(target=self._guard, args=(name, fn),
                            name="scanner-" + name, daemon=True) for name, fn in loops.items()}
            self.thread = self.threads["scan"]
            for thread in self.threads.values():
                thread.start()
            self.heartbeat(operation="started")
            if self.settings.startup_notice:
                self.queue_notice('startup:'+uuid.uuid4().hex,'status',{'status':'RESEARCH_WORKER_STARTED','at':utc_now(),'note':'Market research worker started. Rose source requires separate authorization. No order execution.'})
            return True
        except BaseException:
            self.stop.set()
            self.lock.close()
            raise

    def close(self):
        self.stop.set()
        deadline = time.monotonic() + 5
        for thread in self.threads.values():
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if not any(thread.is_alive() for thread in self.threads.values()):
            self.lock.close()

    def queue_notice(self, key, kind, data):
        channels = self._delivery_channels()
        for channel in channels:
            self.store.queue_delivery(key + ":" + channel,
                {"channel": channel, "kind": kind, "data": {**data, "event_id": key}})
        self.store.set_state("last_notice", {"event_id": key, "kind": kind, "queued_channels": channels,
                             "status": "QUEUED" if channels else "UNCONFIGURED", "at": utc_now()})
        return len(channels)

    def _enqueue_auto(self, now):
        """Keep one pending auto job, refreshing it instead of growing backlog."""
        key = "auto:" + str(int(now.timestamp()) // self.settings.scan_interval_seconds)
        payload = {"exchanges": list(self.settings.exchanges), "limit": self.settings.auto_limit,
                   "symbols": [], "scheduled": True, "origin": "AUTO", "scheduled_at": now.isoformat()}
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("SELECT 1 FROM jobs WHERE dedup_key=?", (key,)).fetchone():
                    db.commit()
                    return
                pending = db.execute("SELECT id FROM jobs WHERE status='PENDING' AND dedup_key LIKE 'auto:%' ORDER BY created_at DESC").fetchall()
                if pending:
                    db.execute("UPDATE jobs SET dedup_key=?,payload=?,created_at=? WHERE id=?",
                               (key, dumps(payload), now.isoformat(), pending[0]["id"]))
                    for row in pending[1:]:
                        db.execute("UPDATE jobs SET status='SKIPPED',finished_at=?,error=? WHERE id=?",
                                   (now.isoformat(), "Coalesced into a fresher auto scan", row["id"]))
                else:
                    db.execute("INSERT INTO jobs(id,dedup_key,kind,payload,created_at) VALUES(?,?,?,?,?)",
                               (uuid.uuid4().hex, key, "scan", dumps(payload), now.isoformat()))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _missed_session(self, name, run, reason, now):
        self.store.set_state("missed_session:" + name + ":" + str(run.get("utc", "unknown")),
            {"anchor": name, "scheduled_for": run, "status": "MISSED", "reason": reason, "recorded_at": now.isoformat()})

    def schedule(self):
        now = self.clock().astimezone(timezone.utc)
        if self.settings.auto_scan:
            self._enqueue_auto(now)
        if not self.settings.session_reports:
            self.store.set_state("session_scheduler", {"status": "DISABLED", "at": now.isoformat()})
            return
        cursor = timestamp(self.store.get_state("session_scheduler_cursor"))
        now_ms = int(now.timestamp() * 1000)
        start = datetime.fromtimestamp(cursor / 1000, timezone.utc) if cursor is not None else now - timedelta(minutes=2)
        if start > now:
            self.store.set_state("session_clock_anomaly", {"previous_cursor": start.isoformat(), "now": now.isoformat()})
            start = now - timedelta(minutes=2)
        floor = now - timedelta(hours=48)
        if start < floor:
            self.store.set_state("session_recovery_gap", {"status": "MISSED_RANGE", "from": start.isoformat(),
                "through": floor.isoformat(), "reason": "Outage exceeded detailed 48h replay window; no stale briefing flood"})
            start = floor
        for name, schedule in next_session_runs(start, count=10)["schedules"].items():
            for run in schedule["runs"]:
                due = datetime.fromisoformat(run["utc"].replace("Z", "+00:00"))
                if due > now:
                    continue
                delay = (now - due).total_seconds()
                if delay > getattr(self.settings, "stale_job_seconds", 900):
                    self._missed_session(name, run, "Report anchor is too old for a live briefing", now)
                    continue
                self.store.enqueue("session", {"exchanges": list(self.settings.exchanges),
                    "limit": self.settings.auto_limit, "symbols": [], "anchor": name,
                    "scheduled_for": run, "scheduler_delay_seconds": delay}, "session:" + name + ":" + run["utc"])
        self.store.set_state("session_scheduler_cursor", now_ms)
        with self.store.connection() as db:
            missed = db.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'missed_session:%'").fetchone()[0]
        self.store.set_state("session_scheduler", {"status": "ACTIVE", "cursor": now.isoformat(),
            "missed_count": missed, "delivery_status": "CONFIGURED_NOT_VERIFIED" if self._delivery_channels() else "UNCONFIGURED",
            "source_watcher": "NOT_VERIFIED"})

    def _schedule_loop(self):
        last_maintenance=0
        while not self.stop.is_set():
            self.schedule()
            if time.monotonic()-last_maintenance>300:
                self.store.maintain()
                last_maintenance=time.monotonic()
            self.heartbeat(operation="scheduling")
            self.stop.wait(15)

    def deliver_one(self):
        item = self.store.claim_delivery()
        if not item:
            return False
        payload = item["payload"]
        try:
            if payload["channel"] == "telegram":
                if not self.settings.bot_token or not self.settings.chat_id:
                    raise RuntimeError("Recipient unavailable")
                result = requests.post("https://api.telegram.org/bot" + self.settings.bot_token + "/sendMessage",
                    json={"chat_id": self.settings.chat_id, "text": message_text(payload["kind"], payload["data"])[:4000],
                          "disable_web_page_preview": True}, timeout=(5, 15))
                if result.status_code != 200 or not result.json().get("ok"):
                    raise RuntimeError("Telegram rejected delivery")
            elif payload["channel"] == "webhook":
                if not self.settings.webhook_url:
                    raise RuntimeError("Recipient unavailable")
                result = requests.post(self.settings.webhook_url, json={**payload, "event_id": item["dedup_key"]},
                    headers={"Idempotency-Key": item["dedup_key"]}, timeout=(5, 15))
                if not 200 <= result.status_code < 300:
                    raise RuntimeError("Webhook rejected delivery")
            else:
                raise ValueError("Unknown delivery channel")
            self.store.finish_delivery(item["id"])
            self.store.set_state("delivery", {"status": "LAST_DELIVERY_OK", "at": utc_now(),
                "event_id": item["dedup_key"], "channel": payload["channel"], "semantics": "AT_LEAST_ONCE"})
        except Exception as error:
            self.store.finish_delivery(item["id"], type(error).__name__)
            self.store.set_state("delivery", {"status": "RETRY_PENDING", "at": utc_now(),
                "error": type(error).__name__, "channel": payload.get("channel"), "semantics": "AT_LEAST_ONCE"})
        return True

    def _delivery_loop(self):
        while not self.stop.is_set():
            if not self.deliver_one():
                self.stop.wait(2)

    def _job_expired(self, job):
        started = timestamp(job["payload"].get("scheduled_for", {}).get("utc")) if job["kind"] == "session" else timestamp(job.get("created_at"))
        return started is not None and self.clock().timestamp() * 1000 - started > getattr(self.settings, "stale_job_seconds", 900) * 1000

    def process_job(self, job):
        self.heartbeat(job_id=job["id"], operation=job["kind"])
        if self._job_expired(job):
            if job["kind"] == "session":
                self._missed_session(job["payload"].get("anchor", "unknown"), job["payload"].get("scheduled_for", {}),
                                     "Queued report exceeded maximum live age", self.clock())
            self.store.finish(job["id"], {"status": "EXPIRED", "reason": "Stale live job skipped; request fresh analysis", "returned": 0})
            return
        try:
            result = self.scanner.execute_scan(job, progress=lambda ex, sym: self.heartbeat(job_id=job["id"], exchange=ex, asset=sym))
            rows = self.store.job(job["id"])["results"]
            queued = 0
            if job["kind"] == "session":
                now, scheduled = self.clock(), job["payload"].get("scheduled_for", {})
                due = timestamp(scheduled.get("utc"))
                data = {"time": session_context(now)["current_time"], "summary": result,
                    "regimes": sorted(set(x["exchange"] + ":" + x["regime"]["label"] for x in rows)),
                    "candidates": [x["exchange"] + ":" + x["symbol"] + " (" + x["status"] + ")"
                                   for x in sorted(rows, key=lambda x: x["activity_score"], reverse=True)[:5]],
                    "scheduled_for": scheduled, "delay_seconds": max(0, now.timestamp() - due / 1000) if due else 0, "job_id": job["id"]}
                queued += self.queue_notice("brief:" + job["id"], "session", data)
            elif job["payload"].get("scheduled"):
                for row in rows:
                    if row["status"] not in {"EARLY", "READY"}:
                        continue
                    bar = row["features"].get("timeframes", {}).get("4h", {}).get("last_close_time")
                    if timestamp(bar) is None:
                        continue
                    key = f"discovery:{row['exchange']}:{row['symbol']}:{bar}:{row['status']}"
                    queued += self.queue_notice(key, "status", {"symbol": row["symbol"], "exchange": row["exchange"],
                        "stage": row["status"], "coverage_pct": row["data_coverage_pct"], "probability": None, "data_gaps": row["data_gaps"][:5]})
            result["delivery_status"] = "QUEUED" if queued else "UNCONFIGURED" if not self._delivery_channels() else "NO_NEW_NOTICE"
            # Outbox first, DONE second. Replay after a crash cannot lose notice.
            self.store.finish(job["id"], result)
        except Exception as error:
            self.queue_notice("job_failure:" + job["id"], "status", {"status": "SCAN_FAILED", "job_id": job["id"], "error": type(error).__name__})
            self.store.finish(job["id"], error=type(error).__name__)

    def check_watches(self):
        for watch in self.store.watches():
            if self.stop.is_set():
                break
            if watch["status"] in {"INVALIDATED", "EXPIRED"}:
                continue
            try:
                now_ms, plan = int(self.clock().timestamp() * 1000), watch["plan"]
                expiry, plan_expiry = timestamp(watch.get("expires_at")), timestamp(plan.get("expires_at_ms"))
                created, stop = timestamp(watch.get("created_at")), number(plan.get("stop"))
                if expiry is None or plan_expiry is None or created is None or stop is None or stop <= 0 or plan.get("side") not in {"LONG", "SHORT"}:
                    status, result = "WAITING", {"ready": False, "waiting_for": ["Immutable watch levels/timestamps incomplete; re-analysis required"]}
                elif min(expiry, plan_expiry) <= now_ms:
                    status, result = "EXPIRED", {"ready": False, "waiting_for": ["Watch or immutable plan expired; re-analysis required"]}
                else:
                    data = self.scanner.analyze_asset(watch["symbol"], watch["exchange"])
                    now_ms, market = int(self.clock().timestamp() * 1000), data["market"]
                    current = number(market.get("price"))
                    observed = timestamp(market.get("ticker_observed_at_ms", market.get("observed_at_ms")))
                    age = source_age(market, now_ms)
                    fresh = current is not None and current > 0 and observed is not None and observed >= created - 5000 and age is not None and 0 <= age <= 120
                    if not fresh:
                        status, result = "WAITING", {"ready": False, "waiting_for": ["Fresh post-watch ticker unavailable; stale prices cannot invalidate a watch"]}
                    elif (plan["side"] == "LONG" and current <= stop) or (plan["side"] == "SHORT" and current >= stop):
                        status, result = "INVALIDATED", {"ready": False, "waiting_for": ["Fresh observed price crossed structural invalidation"], "observed_at_ms": observed}
                    else:
                        result = readiness(plan, market, data["features"], data["regime"], data["enrichment"], data["evidence"], now_ms, created_after_ms=created)
                        status = "READY" if result["ready"] else "WAITING"
                # Notification dedup is persisted before terminal watch state.
                if status != watch["status"] and status != "WAITING":
                    self.queue_notice("watch:" + watch["id"] + ":" + status, "watch", {
                        "watch_id": watch["id"], "exchange": watch["exchange"], "symbol": watch["symbol"], "status": status,
                        "summary": "; ".join(result.get("waiting_for", [])) or "All configured evidence gates passed on a completed trigger candle."})
                self.store.update_watch(watch["id"], status, result)
            except Exception as error:
                self.store.update_watch(watch["id"], "WAITING", {"ready": False, "waiting_for": ["Watch data error: " + type(error).__name__]})
        self.store.set_state("watch_loop", {"status": "CHECKED", "at": utc_now()})

    def _watch_loop(self):
        while not self.stop.is_set():
            self.check_watches()
            self.stop.wait(60)

    def run(self):
        """Scans do not own the watch, scheduler or delivery clocks."""
        while not self.stop.is_set():
            job = self.store.claim()
            if job:
                self.process_job(job)
            else:
                self.stop.wait(2)
