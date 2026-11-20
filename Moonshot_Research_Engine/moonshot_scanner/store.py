"""SQLite WAL job queue, versioned research snapshots and delivery outbox.

Single-host storage only. A Render disk cannot be shared by separate services;
web and worker must use the same mounted volume on one host for this backend.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clean_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean_json(x) for x in value]
    return value


def dumps(value):
    return json.dumps(clean_json(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE, kind TEXT NOT NULL,
              payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
              created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
              leased_until REAL, error TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
              exchange TEXT NOT NULL, symbol TEXT NOT NULL, created_at TEXT NOT NULL,
              data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS snapshots_asset ON snapshots(exchange,symbol,id);
            CREATE TABLE IF NOT EXISTS scan_errors (
              id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
              exchange TEXT, symbol TEXT, error TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS oi (
              exchange TEXT NOT NULL, symbol TEXT NOT NULL, observed_ms INTEGER NOT NULL,
              base_units REAL NOT NULL, unit TEXT NOT NULL,
              PRIMARY KEY(exchange,symbol,observed_ms));
            CREATE TABLE IF NOT EXISTS evidence (
              id TEXT PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL,
              observed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
              verified INTEGER NOT NULL, source_url TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL, payload TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt REAL NOT NULL DEFAULT 0, leased_until REAL,
              delivered_at TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rose_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
              received_at TEXT NOT NULL, source_post_at TEXT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS watches (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
              exchange TEXT NOT NULL, symbol TEXT NOT NULL, plan TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'WAITING', checked_at TEXT, result TEXT);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
        finally:
            db.close()

    def set_state(self, key, value):
        with self.connection() as db:
            db.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    def get_state(self, key, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def enqueue(self, kind, payload, dedup_key=None):
        job_id = uuid.uuid4().hex
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,dedup_key,kind,payload,created_at) VALUES(?,?,?,?,?)", (job_id, dedup_key, kind, dumps(payload), utc_now()))
            if dedup_key:
                row = db.execute("SELECT id FROM jobs WHERE dedup_key=?", (dedup_key,)).fetchone()
                return row[0]
        return job_id

    def claim(self, lease_seconds=900):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                row = db.execute("SELECT * FROM jobs WHERE status='PENDING' OR (status='RUNNING' AND leased_until<?) ORDER BY created_at LIMIT 1", (now,)).fetchone()
                if not row:
                    db.commit()
                    return None
                db.execute("UPDATE jobs SET status='RUNNING',started_at=?,leased_until=? WHERE id=?", (utc_now(), now + lease_seconds, row["id"]))
                db.commit()
                result = dict(row)
                result["payload"] = json.loads(result["payload"])
                return result
            except BaseException:
                db.rollback()
                raise

    def renew(self, job_id, seconds=900):
        with self.connection() as db:
            db.execute("UPDATE jobs SET leased_until=? WHERE id=? AND status='RUNNING'", (time.time() + seconds, job_id))

    def finish(self, job_id, result=None, error=None):
        with self.connection() as db:
            db.execute("UPDATE jobs SET status=?,finished_at=?,leased_until=NULL,result=?,error=? WHERE id=?", ("FAILED" if error else "DONE", utc_now(), dumps(result) if result is not None else None, error, job_id))

    def job(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            for key in ("payload", "result"):
                result[key] = json.loads(result[key]) if result[key] else None
            result["results"] = [json.loads(x[0]) for x in db.execute("SELECT data FROM snapshots WHERE job_id=? ORDER BY id", (job_id,))]
            result["errors"] = [dict(x) for x in db.execute("SELECT exchange,symbol,error,created_at FROM scan_errors WHERE job_id=? ORDER BY id", (job_id,))]
            return result

    def add_snapshot(self, job_id, data):
        with self.connection() as db:
            # Retry after process death replaces the same asset's incomplete result.
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM snapshots WHERE job_id=? AND exchange=? AND symbol=?", (job_id, data["exchange"], data["symbol"]))
                db.execute("INSERT INTO snapshots(job_id,exchange,symbol,created_at,data) VALUES(?,?,?,?,?)", (job_id, data["exchange"], data["symbol"], utc_now(), dumps(data)))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def add_error(self, job_id, exchange, symbol, error):
        with self.connection() as db:
            db.execute("INSERT INTO scan_errors(job_id,exchange,symbol,error,created_at) VALUES(?,?,?,?,?)", (job_id, exchange, symbol, str(error), utc_now()))

    def latest(self, limit=100):
        with self.connection() as db:
            rows = db.execute("SELECT data FROM snapshots WHERE id IN (SELECT MAX(id) FROM snapshots GROUP BY exchange,symbol) ORDER BY id DESC LIMIT ?", (min(200, max(1, limit)),))
            return [json.loads(x[0]) for x in rows]

    def add_oi(self, exchange, symbol, observed_ms, base_units, unit="BASE_COIN"):
        if not observed_ms or not math.isfinite(float(base_units)) or base_units <= 0 or unit != "BASE_COIN":
            return
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO oi VALUES(?,?,?,?,?)", (exchange, symbol, int(observed_ms), float(base_units), unit))

    def oi_windows(self, exchange, symbol, observed_ms, base_units, tolerance_ms=600000):
        result = {"unit": "BASE_COIN", "observed_at_ms": observed_ms, "base_units": base_units}
        with self.connection() as db:
            for hours in (4, 24):
                key = str(hours) + "h"
                target = observed_ms - hours * 3600000
                row = db.execute("SELECT * FROM oi WHERE exchange=? AND symbol=? AND observed_ms BETWEEN ? AND ? AND unit='BASE_COIN' ORDER BY ABS(observed_ms-?) LIMIT 1", (exchange, symbol, target-tolerance_ms, target+tolerance_ms, target)).fetchone()
                result[key] = {"change_pct": (base_units / row["base_units"] - 1) * 100 if row else None,
                               "baseline_at_ms": row["observed_ms"] if row else None,
                               "status": "AVAILABLE" if row else "HISTORY_INSUFFICIENT"}
        return result

    def add_evidence(self, data):
        fields = {"symbol", "kind", "observed_at", "expires_at", "source_url", "verified", "facts"}
        if not fields.issubset(data) or data["kind"] not in {"unlock", "catalyst", "fundamental", "whale_transfer", "security", "macro"}:
            raise ValueError("Evidence requires symbol, kind, observed_at, expires_at, source_url, verified and facts")
        if not isinstance(data["verified"], bool) or not isinstance(data["facts"], dict):
            raise ValueError("verified must be boolean and facts an object")
        import re
        if data["symbol"] == "*":
            if data["kind"] != "macro":
                raise ValueError("Wildcard evidence is only permitted for macro events")
        elif not re.fullmatch(r"[A-Z0-9]{2,24}USDT",str(data["symbol"])):
            raise ValueError("Asset evidence symbol must be an uppercase USDT pair")
        from urllib.parse import urlparse
        u = urlparse(data["source_url"])
        if u.scheme != "https" or not u.hostname or u.username or u.password:
            raise ValueError("Evidence requires a credential-free HTTPS source URL")
        observed = datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        if not observed.tzinfo or not expires.tzinfo or expires <= observed:
            raise ValueError("Evidence times must include UTC offset and expiry must follow observation")
        # 'verified' is an operator-reviewed assertion, never proof from merely a URL.
        eid = hashlib.sha256(dumps(data).encode()).hexdigest()
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?)", (eid, data["symbol"], data["kind"], data["observed_at"], data["expires_at"], int(data["verified"]), data["source_url"], dumps(data)))
        return eid

    def evidence(self, symbol, now=None):
        now = now or datetime.now(timezone.utc)
        with self.connection() as db:
            rows = db.execute("SELECT data FROM evidence WHERE symbol=? OR (symbol='*' AND kind='macro') ORDER BY observed_at DESC", (symbol,))
            result = []
            for row in rows:
                data = json.loads(row[0])
                data["fresh"] = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")) > now and datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00")) <= now
                result.append(data)
            return result

    def queue_delivery(self, key, payload):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO outbox(dedup_key,created_at,payload) VALUES(?,?,?)", (key, utc_now(), dumps(payload)))

    def claim_delivery(self, lease_seconds=120):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM outbox WHERE next_attempt<=? AND (status='PENDING' OR (status='SENDING' AND leased_until<?)) ORDER BY id LIMIT 1", (time.time(), time.time())).fetchone()
                if not row:
                    db.commit()
                    return None
                db.execute("UPDATE outbox SET status='SENDING',leased_until=?,attempts=attempts+1 WHERE id=?", (time.time()+lease_seconds, row["id"]))
                db.commit()
                result = dict(row)
                result["payload"] = json.loads(result["payload"])
                return result
            except BaseException:
                db.rollback()
                raise

    def finish_delivery(self, item_id, error=None):
        with self.connection() as db:
            if error:
                row = db.execute("SELECT attempts FROM outbox WHERE id=?", (item_id,)).fetchone()
                delay = min(3600, 15 * 2 ** min(row[0], 8))
                db.execute("UPDATE outbox SET status='PENDING',error=?,next_attempt=?,leased_until=NULL WHERE id=?", (str(error), time.time()+delay, item_id))
            else:
                db.execute("UPDATE outbox SET status='DELIVERED',delivered_at=?,error=NULL,leased_until=NULL WHERE id=?", (utc_now(), item_id))

    def outbox_status(self):
        with self.connection() as db:
            return {x[0]: x[1] for x in db.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status")}

    def record_rose(self, event_key, data, source_post_at=None):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO rose_events(event_key,received_at,source_post_at,data) VALUES(?,?,?,?)", (event_key, utc_now(), source_post_at, dumps(data)))

    def recent_rose(self, limit=20):
        with self.connection() as db:
            rows = db.execute("SELECT event_key,received_at,source_post_at,data FROM rose_events ORDER BY id DESC LIMIT ?", (min(100, max(1, limit)),))
            return [{**dict(x), "data": json.loads(x["data"])} for x in rows]

    def learning_status(self):
        return {"mode": "SHADOW_ONLY", "calibrated_probability": None, "live_weights_changed": False,
                "reason": "No validated prospective out-of-sample model has been installed; heuristic ranks are not probabilities."}

    def maintain(self,full_count=300,history_count=5000):
        """Bound repeated OHLC storage; retain latest asset charts and compact history."""
        with self.connection() as db:
            cut=db.execute('SELECT id FROM snapshots ORDER BY id DESC LIMIT 1 OFFSET ?', (full_count-1,)).fetchone()
            if cut:
                rows=db.execute('SELECT id,data FROM snapshots WHERE id<? AND id NOT IN (SELECT MAX(id) FROM snapshots GROUP BY exchange,symbol) AND data LIKE ? LIMIT 100',(cut[0],'%"candles":%')).fetchall()
                for row in rows:
                    data=json.loads(row['data']);data.get('market',{}).pop('candles',None);data['raw_candles_retained']=False
                    db.execute('UPDATE snapshots SET data=? WHERE id=?',(dumps(data),row['id']))
                self.set_state('compacted_snapshot_id',cut[0]-1)
            db.execute('DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT ?) AND id NOT IN (SELECT MAX(id) FROM snapshots GROUP BY exchange,symbol)',(history_count,))
            db.execute('DELETE FROM oi WHERE observed_ms<?',(int(time.time()*1000)-35*86400000,))
            db.execute("DELETE FROM scan_errors WHERE id NOT IN (SELECT id FROM scan_errors ORDER BY id DESC LIMIT 5000)")
            db.execute("DELETE FROM jobs WHERE status IN ('DONE','FAILED','EXPIRED','SKIPPED') AND id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC LIMIT 5000)")
            db.execute("DELETE FROM outbox WHERE status='DELIVERED' AND id NOT IN (SELECT id FROM outbox ORDER BY id DESC LIMIT 5000)")
            db.execute('PRAGMA wal_checkpoint(PASSIVE)')
        self.set_state('retention',{'full_recent_snapshots':full_count,'compact_history_count':history_count,'oi_days':35,'last_maintenance':utc_now(),'latest_asset_chart_preserved':True})

    def recover_worker(self):
        """Call only after acquiring the exclusive worker process lock."""
        with self.connection() as db:
            db.execute("UPDATE jobs SET status='PENDING',leased_until=NULL WHERE status='RUNNING'")
            db.execute("UPDATE outbox SET status='PENDING',leased_until=NULL WHERE status='SENDING'")

    def add_watch(self, exchange, symbol, plan, lifetime_hours=24):
        from datetime import timedelta
        if not 1 <= lifetime_hours <= 72:
            raise ValueError("Watch lifetime must be 1..72 hours")
        key = hashlib.sha256(dumps({"exchange":exchange,"symbol":symbol,"plan":plan}).encode()).hexdigest()[:24]
        now = datetime.now(timezone.utc)
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO watches(id,created_at,expires_at,exchange,symbol,plan) VALUES(?,?,?,?,?,?)", (key,now.isoformat(),(now+timedelta(hours=lifetime_hours)).isoformat(),exchange,symbol,dumps(plan)))
        return key

    def watches(self):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM watches ORDER BY created_at DESC")
            return [{**dict(x),"plan":json.loads(x["plan"]),"result":json.loads(x["result"]) if x["result"] else None} for x in rows]

    def update_watch(self, key, status, result):
        with self.connection() as db:
            db.execute("UPDATE watches SET status=?,result=?,checked_at=? WHERE id=?", (status,dumps(result),utc_now(),key))

    def delete_watch(self, key):
        with self.connection() as db:
            db.execute("DELETE FROM watches WHERE id=?", (key,))
