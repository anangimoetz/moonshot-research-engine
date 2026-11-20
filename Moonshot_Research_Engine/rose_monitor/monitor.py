"""Durable, read-only Rose public-preview watcher. No exchange keys or trades.

The public preview has no delivery SLA. This process must remain awake and online.
SQLite is the source of truth; JSON files are local notification projections.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import urljoin

import requests

UTC = timezone.utc
CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,63}$")
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True)
class Config:
    channel: str = "RoseSignalsPremium"
    state_dir: Path = Path(__file__).resolve().parent / "state"
    poll_seconds: float = 15
    timeout: float = 15
    overlap_pages: int = 2
    max_pages: int = 20
    workers: int = 2
    daily_hour_utc: int = 5
    daily_enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    once_timeout: float = 180

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            channel=os.environ.get("ROSE_CHANNEL", cls.channel).lstrip("@"),
            state_dir=Path(os.environ.get("ROSE_STATE_DIR", str(cls.state_dir))).expanduser().resolve(),
            poll_seconds=float(os.environ.get("ROSE_POLL_SECONDS", "15")),
            timeout=float(os.environ.get("ROSE_HTTP_TIMEOUT", "15")),
            overlap_pages=int(os.environ.get("ROSE_OVERLAP_PAGES", "2")),
            max_pages=int(os.environ.get("ROSE_MAX_PAGES_PER_POLL", "20")),
            workers=int(os.environ.get("ROSE_ANALYSIS_WORKERS", "2")),
            daily_hour_utc=int(os.environ.get("ROSE_DAILY_HOUR_UTC", "5")),
            daily_enabled=os.environ.get("ROSE_DAILY_ENABLED", "0") == "1",
            bot_token=os.environ.get("ROSE_BOT_TOKEN", ""),
            chat_id=os.environ.get("ROSE_CHAT_ID", ""),
            once_timeout=float(os.environ.get("ROSE_ONCE_TIMEOUT", "180")),
        )
        if not CHANNEL_RE.fullmatch(cfg.channel):
            raise ValueError("ROSE_CHANNEL must be a Telegram channel username")
        if cfg.poll_seconds < 1 or cfg.timeout < 1:
            raise ValueError("Polling and HTTP timeout must be at least one second")
        if not 1 <= cfg.overlap_pages <= cfg.max_pages <= 200:
            raise ValueError("Require 1 <= overlap pages <= max pages <= 200")
        if not 1 <= cfg.workers <= 8 or not 0 <= cfg.daily_hour_utc <= 23:
            raise ValueError("Invalid workers or daily UTC hour")
        if bool(cfg.bot_token) != bool(cfg.chat_id):
            raise ValueError("Set both ROSE_BOT_TOKEN and ROSE_CHAT_ID, or neither")
        if re.search(r"\s", cfg.bot_token + cfg.chat_id):
            raise ValueError("Telegram delivery credentials must not contain whitespace")
        return cfg

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


class PreviewParser(HTMLParser):
    """Extract message bodies without following or executing channel content."""

    def __init__(self, channel: str):
        super().__init__(convert_charrefs=True)
        self.channel = channel
        self.stack: list[tuple[str, set[str]]] = []
        self.current: dict | None = None
        self.root_depth = 0
        self.text_depth = 0
        self.meta_depth = 0
        self.text_parts: list[str] = []
        self.meta_parts: list[str] = []
        self.posts: list[dict] = []
        self.next_before: int | None = None

    def handle_starttag(self, tag: str, attributes):
        attrs = dict(attributes)
        classes = set((attrs.get("class") or "").split())
        depth = len(self.stack) + 1
        if "tme_messages_more" in classes and attrs.get("data-before", "").isdigit():
            self.next_before = int(attrs["data-before"])
        if "tgme_widget_message" in classes and self.current is None:
            match = re.fullmatch(re.escape(self.channel) + r"/(\d+)", attrs.get("data-post", ""), re.I)
            if match:
                message_id = int(match.group(1))
                self.current = {"channel": self.channel, "message_id": message_id, "id": message_id,
                                "url": f"https://t.me/{self.channel}/{message_id}",
                                "published_utc": None, "photo_urls": [], "links": []}
                self.root_depth = depth
                self.text_parts, self.meta_parts = [], []
        if self.current is not None:
            reply_ancestor = any("tgme_widget_message_reply" in c for _, c in self.stack)
            if "tgme_widget_message_text" in classes and "js-message_reply_text" not in classes and not reply_ancestor:
                self.text_depth = depth
            if "tgme_widget_message_meta" in classes:
                self.meta_depth = depth
            if tag == "br" and self.text_depth:
                self.text_parts.append("\n")
            if tag == "a" and self.text_depth and attrs.get("href"):
                self.current["links"].append(urljoin("https://t.me", attrs["href"]))
            if tag == "time" and attrs.get("datetime") and any(
                    "tgme_widget_message_date" in c for _, c in self.stack):
                self.current["published_utc"] = attrs["datetime"]
            if "tgme_widget_message_photo_wrap" in classes:
                match = re.search(r"background-image\s*:\s*url\(\s*['\"]?([^)'\"]+)", attrs.get("style", ""))
                if match:
                    self.current["photo_urls"].append(match.group(1).strip())
        if tag not in VOID_TAGS:
            self.stack.append((tag, classes))

    def handle_startendtag(self, tag: str, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str):
        matching = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if matching is None:
            return
        if self.current is not None:
            if self.text_depth and matching < self.text_depth:
                self.text_depth = 0
            if self.meta_depth and matching < self.meta_depth:
                self.meta_depth = 0
            if matching < self.root_depth:
                self.current["text"] = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.text_parts)).strip()
                self.current["edited_visible"] = bool(re.search(r"\bedited\b", " ".join(self.meta_parts), re.I))
                self.current["edited"] = self.current["edited_visible"]
                try:
                    published = datetime.fromisoformat((self.current["published_utc"] or "").replace("Z", "+00:00"))
                    self.current["published_utc"] = published.astimezone(UTC).isoformat() if published.tzinfo else None
                except ValueError:
                    self.current["published_utc"] = None
                self.current["timestamp"] = self.current["published_utc"]
                self.posts.append(self.current)
                self.current = None
                self.text_depth = self.meta_depth = 0
        del self.stack[matching:]

    def handle_data(self, value: str):
        if self.current is not None:
            if self.text_depth:
                self.text_parts.append(value)
            if self.meta_depth:
                self.meta_parts.append(value)


def parse_preview(html: str, channel: str) -> tuple[list[dict], int | None]:
    parser = PreviewParser(channel)
    parser.feed(html)
    parser.close()
    unique = {post["message_id"]: post for post in parser.posts}
    return sorted(unique.values(), key=lambda p: p["message_id"]), parser.next_before


def fingerprint(post: dict) -> str:
    # View counters and expiring Telegram CDN links are deliberately excluded.
    # Later image-only replacements with an existing 'edited' marker cannot be
    # reliably distinguished by this public transport; full media needs MTProto.
    substantive = {"text": post.get("text", ""), "links": sorted(set(post.get("links", []))),
                   "photo_count": len(post.get("photo_urls", [])),
                   "edited_visible": bool(post.get("edited_visible", False)),
                   "media_id": post.get("media_id"), "edit_utc": post.get("edit_utc")}
    return sha256(compact_json(substantive).encode()).hexdigest()


def signal_metadata(post: dict) -> dict:
    """Conservative caption extraction; never infer chart levels from a preview."""
    text = post.get("text", "")
    symbols = list(dict.fromkeys(re.findall(r"(?<!\w)[#$]([A-Za-z][A-Za-z0-9]{1,14})(?:\b)", text)))
    symbols += re.findall(r"\b([A-Z][A-Z0-9]{1,14})[/_-]?USDT\b", text)
    symbols = list(dict.fromkeys(s.upper().removesuffix("USDT") for s in symbols))
    long = bool(re.search(r"\b(?:long|bullish|buy)\b", text, re.I))
    short = bool(re.search(r"\b(?:short|bearish|sell)\b", text, re.I))
    direction = "long" if long and not short else "short" if short and not long else None
    text_plan = all(re.search(pattern, text, re.I) for pattern in (
        r"\bentry\b[^\n]{0,30}\d", r"\b(?:stop|stoploss|sl)\b[^\n]{0,30}\d",
        r"\b(?:targets?|tp\d*)\b[^\n]{0,30}\d"))
    chart_review = bool(post.get("photo_urls")) and not (symbols and direction and text_plan)
    return {**post, "post_id": post["message_id"], "symbols": symbols, "direction": direction,
            "image_only": chart_review, "chart_review_required": chart_review}


class Store:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.state_dir / "monitor.sqlite3", timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS posts(
                    channel TEXT, message_id INTEGER, current_hash TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(channel,message_id));
                CREATE TABLE IF NOT EXISTS versions(
                    id INTEGER PRIMARY KEY, channel TEXT NOT NULL, message_id INTEGER NOT NULL,
                    hash TEXT NOT NULL, observed_at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    version_id INTEGER PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    last_error TEXT, FOREIGN KEY(version_id) REFERENCES versions(id));
                CREATE TABLE IF NOT EXISTS outbox(
                    id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                    payload TEXT NOT NULL, text TEXT NOT NULL,
                    telegram_required INTEGER NOT NULL DEFAULT 0, telegram_sent_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT);
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state,next_attempt);
                CREATE INDEX IF NOT EXISTS outbox_due ON outbox(telegram_required,telegram_sent_at,next_attempt);
            """)
            version_sql = self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='versions'").fetchone()[0]
            if "UNIQUE(channel,message_id,hash)" in version_sql:
                self.db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE versions_new(id INTEGER PRIMARY KEY, channel TEXT NOT NULL,
                        message_id INTEGER NOT NULL, hash TEXT NOT NULL, observed_at TEXT NOT NULL,
                        kind TEXT NOT NULL, payload TEXT NOT NULL);
                    INSERT INTO versions_new SELECT * FROM versions;
                    DROP TABLE versions;
                    ALTER TABLE versions_new RENAME TO versions;
                    COMMIT;
                """)
            self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    @staticmethod
    def _get(db, key: str, default=None):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(db, key: str, value):
        db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, compact_json(value)))

    def get(self, key: str, default=None):
        with self.lock:
            return self._get(self.db, key, default)

    def set(self, key: str, value):
        with self.transaction() as db:
            self._set(db, key, value)

    def set_many(self, values: dict):
        with self.transaction() as db:
            for key, value in values.items():
                self._set(db, key, value)

    def recover(self):
        # A process-level lock ensures no live worker owns these claims.
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state='pending' WHERE state='running'")

    def ingest(self, posts: list[dict], *, bootstrap: bool = False, telegram: bool = False) -> dict:
        now = utc_now()
        counters = {"new": 0, "edited": 0, "baseline": 0}
        with self.transaction() as db:
            floor = self._get(db, "bootstrap_max_id", 0)
            if bootstrap:
                floor = max((p["message_id"] for p in posts), default=0)
                self._set(db, "bootstrap_max_id", floor)
                self._set(db, "bootstrapped_at", now)
            latest = self._get(db, "last_message_id", 0)
            for original in posts:
                post = dict(original)
                mid, channel, digest = post["message_id"], post["channel"], fingerprint(post)
                old = db.execute("SELECT current_hash FROM posts WHERE channel=? AND message_id=?", (channel, mid)).fetchone()
                latest = max(latest, mid)
                db.execute("INSERT INTO posts VALUES(?,?,?,?,?) ON CONFLICT(channel,message_id) DO UPDATE SET current_hash=excluded.current_hash,last_seen=excluded.last_seen",
                           (channel, mid, digest, now, now))
                if old and old[0] == digest:
                    continue
                kind = "baseline" if bootstrap or (not old and mid <= floor) else ("edited" if old else "new")
                post.update(observed_at=now, kind=kind, content_hash=digest)
                cursor = db.execute("INSERT INTO versions(channel,message_id,hash,observed_at,kind,payload) VALUES(?,?,?,?,?,?)",
                                    (channel, mid, digest, now, kind, compact_json(post)))
                if cursor.rowcount:
                    counters[kind] += 1
                    if kind != "baseline":
                        db.execute("INSERT INTO jobs(version_id) VALUES(?)", (cursor.lastrowid,))
            self._set(db, "last_message_id", latest)
            self._set(db, "last_ingest_at", now)
        return counters

    def claim_job(self):
        with self.transaction() as db:
            row = db.execute("SELECT v.id,v.payload,j.attempts FROM jobs j JOIN versions v ON v.id=j.version_id WHERE j.state='pending' AND j.next_attempt<=? ORDER BY j.attempts ASC,v.id DESC LIMIT 1", (time.time(),)).fetchone()
            if row:
                db.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE version_id=?", (row["id"],))
                return dict(row)
        return None

    def complete_job(self, job: dict, payload: dict, text: str, telegram: bool):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO outbox(event_key,created_at,payload,text,telegram_required) VALUES(?,?,?,?,?)",
                       (f"signal:{job['id']}", utc_now(), compact_json(payload), text, int(telegram)))
            db.execute("UPDATE jobs SET state='done',last_error=NULL WHERE version_id=?", (job["id"],))

    def fail_job(self, job: dict, error: str):
        attempts = job["attempts"] + 1
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state='pending',next_attempt=?,last_error=? WHERE version_id=?",
                       (time.time() + min(3600, 5 * 2 ** min(attempts, 10)), error, job["id"]))

    def due_delivery(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM outbox WHERE telegram_required=1 AND telegram_sent_at IS NULL AND next_attempt<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
            return dict(row) if row else None

    def record_delivery(self, row: dict, error: str | None = None, retry_after: float | None = None):
        with self.transaction() as db:
            if error is None:
                db.execute("UPDATE outbox SET telegram_sent_at=?,attempts=attempts+1,last_error=NULL WHERE id=?", (utc_now(), row["id"]))
            else:
                delay = retry_after or min(3600, 5 * 2 ** min(row["attempts"] + 1, 10))
                db.execute("UPDATE outbox SET attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?", (time.time() + delay, error, row["id"]))

    def poll_result(self, error: str | None = None):
        with self.transaction() as db:
            self._set(db, "last_poll_at", utc_now())
            self._set(db, "poll_count", self._get(db, "poll_count", 0) + 1)
            self._set(db, "consecutive_poll_failures", self._get(db, "consecutive_poll_failures", 0) + 1 if error else 0)
            self._set(db, "last_poll_error", error)
            if not error:
                self._set(db, "last_success_at", utc_now())

    def enqueue_status(self, day: str, payload: dict, text: str, telegram: bool):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO outbox(event_key,created_at,payload,text,telegram_required) VALUES(?,?,?,?,?)",
                       ("daily:" + day, utc_now(), compact_json(payload), text, int(telegram)))
            self._set(db, "last_daily_day", day)
            self._set(db, "last_daily_at", payload["period"]["until"])

    def period_counts(self, since: str, until: str) -> dict:
        with self.lock:
            return {r[0]: r[1] for r in self.db.execute("SELECT kind,count(*) FROM versions WHERE observed_at>=? AND observed_at<? GROUP BY kind", (since, until))}

    def status(self, config: Config) -> dict:
        with self.lock:
            meta = {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT key,value FROM meta")}
            queue = {r[0]: r[1] for r in self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state")}
            notifications = self.db.execute("SELECT count(*),sum(CASE WHEN telegram_required=1 AND telegram_sent_at IS NULL THEN 1 ELSE 0 END),sum(CASE WHEN telegram_sent_at IS NOT NULL THEN 1 ELSE 0 END) FROM outbox").fetchone()
            today = datetime.now(UTC).date().isoformat()
            daily = {r[0]: r[1] for r in self.db.execute("SELECT kind,count(*) FROM versions WHERE observed_at>=? GROUP BY kind", (today,))}
        return {"checked_at": utc_now(), "channel": config.channel, "source": (
                    "Telegram MTProto persistent connection; no fixed latency guarantee"
                    if meta.get("source_transport") == "telegram_mtproto_push" else "public Telegram preview; no latency SLA"),
                "poll_seconds": config.poll_seconds, "telegram_configured": config.telegram_enabled,
                "local_output": str(config.state_dir / "alerts.jsonl"), "queue": queue,
                "notifications": {"total": notifications[0], "telegram_pending": notifications[1] or 0, "telegram_sent": notifications[2] or 0},
                "today_utc": {"date": today, **daily}, **meta}

    def project_alerts(self):
        # Regenerate atomically from SQLite: a crash cannot append duplicate lines.
        with self.lock:
            rows = list(reversed(self.db.execute("SELECT id,event_key,created_at,payload,text,telegram_required,telegram_sent_at,attempts,last_error FROM outbox ORDER BY id DESC LIMIT 500").fetchall()))
        if not rows:
            return
        events = []
        for row in rows:
            item = dict(row)
            item["analysis"] = json.loads(item.pop("payload"))
            events.append(item)
        atomic_write(self.state_dir / "alerts.jsonl", "".join(compact_json(event) + "\n" for event in events))
        atomic_write(self.state_dir / "latest.json", json.dumps(events[-1], ensure_ascii=False, indent=2, default=str) + "\n")
        atomic_write(self.state_dir / "latest.txt", events[-1]["text"] + "\n")

    def close(self):
        with self.lock:
            self.db.close()


class ProcessLock:
    """OS-held lock is automatically released on crash; never rely on stale PID."""
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.stream = (directory / "monitor.lock").open("a+b")

    def __enter__(self):
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                if not self.stream.read(1):
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError("Another Rose monitor already owns this state directory") from None
        return self

    def __exit__(self, *_):
        self.stream.close()


class DeliveryError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Monitor:
    def __init__(self, config: Config, *, analyze: Callable | None = None, formatter: Callable | None = None,
                 session=None):
        self.config = config
        self.store = Store(config.state_dir)
        stored_channel = self.store.get("channel")
        if stored_channel and stored_channel.casefold() != config.channel.casefold():
            self.store.close()
            raise ValueError("This state directory belongs to another channel; use a separate ROSE_STATE_DIR")
        self.store.set("channel", config.channel)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "RoseReadOnlyMonitor/1.0", "Cache-Control": "no-cache", "Pragma": "no-cache"})
        self.analyze = analyze
        self.formatter = formatter
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.projection_lock = threading.Lock()
        self.last_projection_signature = None

    def fetch_page(self, before: int | None = None):
        params = {"before": before} if before is not None else {}
        response = self.session.get(f"https://t.me/s/{self.config.channel}", params=params, timeout=self.config.timeout)
        response.raise_for_status()
        posts, next_before = parse_preview(response.text, self.config.channel)
        if not posts or not any(p.get("published_utc") for p in posts):
            raise ValueError("Public preview has no readable timestamped channel posts")
        return posts, next_before

    def poll_once(self) -> dict:
        previous = self.store.get("last_message_id", 0)
        bootstrap = self.store.get("bootstrapped_at") is None
        counts = {"new": 0, "edited": 0, "baseline": 0, "pages": 0}
        posts, cursor = self.fetch_page()
        # Persist the old fully-scanned floor BEFORE ingest advances highest ID.
        # A crash or a failed older-page request must not discard the outage gap.
        if not bootstrap and not self.store.get("backfill_before") and cursor and min(p["message_id"] for p in posts) > previous:
            self.store.set_many({"backfill_before": cursor, "backfill_floor": previous})
        result = self.store.ingest(posts, bootstrap=bootstrap)
        for key in result:
            counts[key] += result[key]
        counts["pages"] += 1
        if bootstrap:
            # Start from current visible state; intentional no historical signals.
            self.store.poll_result()
            return counts
        before = self.store.get("backfill_before") or cursor
        floor = self.store.get("backfill_floor") or previous
        passed = min(p["message_id"] for p in posts) <= previous
        overlap = 1
        caught_up = passed and not self.store.get("backfill_before")
        visited = set()
        while before and counts["pages"] < self.config.max_pages:
            if caught_up and overlap >= self.config.overlap_pages:
                break
            if before in visited:
                raise ValueError("Public preview pagination did not advance")
            visited.add(before)
            rows, next_before = self.fetch_page(before)
            result = self.store.ingest(rows)
            for key in result:
                counts[key] += result[key]
            counts["pages"] += 1
            overlap += 1
            before = next_before
            caught_up = caught_up or min(p["message_id"] for p in rows) <= floor
            if not before:
                caught_up = True
        if not caught_up and before:
            self.store.set_many({"backfill_before": before, "backfill_floor": floor})
        else:
            self.store.set_many({"backfill_before": None, "backfill_floor": None})
        self.store.poll_result()
        return counts

    def _load_engine(self):
        if self.analyze is None:
            selected_engine = os.environ.get("ROSE_ANALYSIS_ENGINE", "legacy")
            if selected_engine == "moonshot_scanner":
                from moonshot_scanner import rose_adapter as engine
            elif selected_engine == "legacy":
                try:
                    from . import engine
                except ImportError:
                    import engine
            else:
                raise ValueError("ROSE_ANALYSIS_ENGINE must be legacy or moonshot_scanner")
            self.analyze = engine.analyze_signal
            self.formatter = getattr(engine, "format_analysis", None)

    def analyze_one(self) -> bool:
        job = self.store.claim_job()
        if job is None:
            return False
        try:
            self._load_engine()
            post = signal_metadata(json.loads(job["payload"]))
            try:
                published = datetime.fromisoformat((post.get("published_utc") or "").replace("Z", "+00:00"))
                age = (datetime.now(UTC) - published).total_seconds() if published.tzinfo else None
            except ValueError:
                age = None
            if age is None or age < 0 or age > 900:
                analysis = {"schema_version": 1, "post_id": post["post_id"], "status": "WAIT", "candidates": [],
                            "classification": "EXPIRED_REVIEW" if age is not None and age > 900 else "TIMESTAMP_REVIEW",
                            "analyzed_utc": utc_now(), "signal_age_seconds": age, "probability": None,
                            "request_failures": [], "sources": {}, "live_api_checks_skipped": True,
                            "reasons": ["Publication is over 15 minutes old, missing, or in the future; saved for review. Live checks skipped to preserve capacity for new signals."]}
            else:
                analysis = self.analyze(post)
            if not isinstance(analysis, dict):
                raise TypeError("Analysis must return a dictionary")
            payload = {"type": "signal_analysis", "post": post, "analysis": analysis,
                       "analysis_completed_at": utc_now(), "source_transport": post.get("source_transport", "public_preview"),
                       "latency_guaranteed": False}
            text = self.formatter(analysis) if self.formatter else json.dumps(analysis, ensure_ascii=False, indent=2, default=str)
            text = f"Rose {post['kind']} post #{post['message_id']}\n{post['url']}\nPublished UTC: {post.get('published_utc') or 'unknown'}\nObserved UTC: {post['observed_at']}\n\n{text}"
            self.store.complete_job(job, payload, text, self.config.telegram_enabled)
        except Exception as error:
            self.store.fail_job(job, "Analysis failed: " + type(error).__name__)
        return True

    def send_telegram(self, text: str):
        # One message keeps retries atomic at the application level. Original
        # complete analysis remains available locally if Telegram text is clipped.
        clipped = text.encode("utf-16-le")[:7800].decode("utf-16-le", errors="ignore")
        body = {"chat_id": self.config.chat_id, "text": clipped, "disable_web_page_preview": True}
        try:
            response = requests.post(f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage", json=body, timeout=self.config.timeout)
            result = response.json()
        except (requests.RequestException, ValueError) as error:
            # requests exception strings include the token-bearing URL. Never log.
            raise DeliveryError("Telegram request failed: " + type(error).__name__) from None
        if not isinstance(result, dict):
            raise DeliveryError("Telegram returned an invalid response")
        if response.status_code != 200 or not result.get("ok"):
            parameters = result.get("parameters") or {}
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            try:
                retry_after = min(86400, max(1, float(retry_after))) if retry_after else None
            except (ValueError, TypeError):
                retry_after = None
            raise DeliveryError(f"Telegram rejected delivery (HTTP {response.status_code})", retry_after)

    def deliver_one(self) -> bool:
        row = self.store.due_delivery()
        if row is None or not self.config.telegram_enabled:
            return False
        try:
            self.send_telegram(row["text"])
            self.store.record_delivery(row)
        except DeliveryError as error:
            self.store.record_delivery(row, str(error), error.retry_after)
        except Exception as error:
            self.store.record_delivery(row, "Telegram delivery failed: " + type(error).__name__)
        return True

    def project(self):
        with self.projection_lock:
            with self.store.lock:
                signature = tuple(self.store.db.execute("SELECT count(*),coalesce(sum(attempts),0),max(telegram_sent_at) FROM outbox").fetchone())
            if signature != self.last_projection_signature:
                self.store.project_alerts()
                self.last_projection_signature = signature

    def maybe_daily(self, now: datetime | None = None):
        if not self.config.daily_enabled:
            return False
        now = now or datetime.now(UTC)
        day = now.date().isoformat()
        if now.hour < self.config.daily_hour_utc or self.store.get("last_daily_day") == day:
            return False
        status = self.store.status(self.config)
        until = now.isoformat(timespec="seconds")
        since = self.store.get("last_daily_at")
        first = since is None
        if first:
            since = max((now - timedelta(days=1)).isoformat(timespec="seconds"), self.store.get("bootstrapped_at", until))
        daily = self.store.period_counts(since, until)
        text = (f"Rose monitor daily status — {day} UTC\n"
                f"Period: {since} to {until}{' (first partial window)' if first else ''}.\n"
                f"New posts observed: {daily.get('new', 0)}; edits: {daily.get('edited', 0)}.\n"
                f"Last successful source check: {status.get('last_success_at', 'none')}.\n"
                f"Pending/running analysis: {status['queue'].get('pending', 0)}/{status['queue'].get('running', 0)}.\n"
                f"Consecutive source failures: {status.get('consecutive_poll_failures', 0)}.\n"
                "Public preview has no delivery-time guarantee. No trades placed.")
        self.store.enqueue_status(day, {"type": "daily_status", "status": status,
                                       "period": {"since": since, "until": until, "first_partial": first},
                                       "counts": daily}, text, self.config.telegram_enabled)
        return True

    def _analysis_worker(self):
        while not self.stop.is_set():
            try:
                worked = self.analyze_one()
            except Exception as error:
                print(compact_json({"at": utc_now(), "error": "Analysis worker operation failed", "kind": type(error).__name__}), flush=True)
                worked = False
            if not worked:
                self.stop.wait(0.3)

    def _delivery_worker(self):
        while not self.stop.is_set():
            try:
                self.deliver_one()
                self.project()
            except Exception as error:
                print(compact_json({"at": utc_now(), "error": "Delivery worker operation failed", "kind": type(error).__name__}), flush=True)
            self.stop.wait(0.5)

    def start_workers(self):
        self.store.recover()
        self.store.set_many({"process_started_at": utc_now(), "process_pid": os.getpid(),
                             "process_stopped_at": None, "process_state": "running"})
        # Fixed worker count: no unbounded executor/futures queue in memory.
        for index in range(self.config.workers):
            self.threads.append(threading.Thread(target=self._analysis_worker, name=f"analysis-{index}", daemon=True))
        self.threads.append(threading.Thread(target=self._delivery_worker, name="delivery", daemon=True))
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=self.config.timeout + 5)
        if any(thread.is_alive() for thread in self.threads):
            self.store.set_many({"process_state": "stopping", "process_stop_requested_at": utc_now()})
            atomic_write(self.config.state_dir / "status.json", json.dumps(self.store.status(self.config), ensure_ascii=False, indent=2) + "\n")
            return  # process exit releases DB; running job is recovered next start
        self.project()
        self.store.set_many({"process_state": "stopped", "process_stopped_at": utc_now()})
        atomic_write(self.config.state_dir / "status.json", json.dumps(self.store.status(self.config), ensure_ascii=False, indent=2) + "\n")
        self.session.close()
        self.store.close()

    def run(self, once: bool = False) -> int:
        self.store.set("run_mode", "once" if once else "continuous")
        self.start_workers()
        failure_count = 0
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    counts = self.poll_once()
                    failure_count = 0
                    print(compact_json({"at": utc_now(), "poll": counts}), flush=True)
                except Exception as error:
                    failure_count += 1
                    self.store.poll_result("Source check failed: " + type(error).__name__)
                    print(compact_json({"at": utc_now(), "error": "Source check failed", "kind": type(error).__name__}), flush=True)
                self.maybe_daily()
                self.store.set("process_heartbeat_at", utc_now())
                atomic_write(self.config.state_dir / "status.json", json.dumps(self.store.status(self.config), ensure_ascii=False, indent=2) + "\n")
                if once:
                    deadline = time.monotonic() + self.config.once_timeout
                    while time.monotonic() < deadline and not self.stop.is_set():
                        state = self.store.status(self.config)
                        if not state["queue"].get("pending", 0) and not state["queue"].get("running", 0) and not state["notifications"]["telegram_pending"]:
                            break
                        self.stop.wait(0.2)
                    self.project()
                    state = self.store.status(self.config)
                    print(compact_json(state), flush=True)
                    incomplete = state["queue"].get("pending", 0) or state["queue"].get("running", 0) or state["notifications"]["telegram_pending"]
                    return 1 if failure_count else 3 if incomplete else 0
                delay = min(300, self.config.poll_seconds * 2 ** min(failure_count, 4)) if failure_count else self.config.poll_seconds
                self.stop.wait(max(0, delay - (time.monotonic() - started)))
        finally:
            self.close()
        return 0


def self_test() -> int:
    """Offline smoke test, never contacting Telegram or an exchange."""
    sample = '<div class="tgme_widget_message" data-post="RoseSignalsPremium/1"><div class="tgme_widget_message_text">#SOL long<br>Watch &amp; wait</div><span class="tgme_widget_message_meta">edited<a class="tgme_widget_message_date"><time datetime="2026-11-18T05:00:00+00:00"></time></a></span></div>'
    rows, _ = parse_preview(sample, "RoseSignalsPremium")
    assert len(rows) == 1 and rows[0]["text"] == "#SOL long\nWatch & wait" and rows[0]["edited_visible"]
    with tempfile.TemporaryDirectory(prefix="rose-monitor-test-") as directory:
        store = Store(Path(directory))
        assert store.ingest(rows, bootstrap=True)["baseline"] == 1
        assert store.claim_job() is None
        row = dict(rows[0], message_id=2, id=2)
        assert store.ingest([row])["new"] == 1
        assert store.ingest([row])["new"] == 0
        job = store.claim_job()
        assert job
        store.complete_job(job, {"offline_test": True}, "Offline test passed", False)
        store.project_alerts()
        store.project_alerts()
        assert len((Path(directory) / "alerts.jsonl").read_text(encoding="utf-8").splitlines()) == 1
        store.close()
    print("Offline parser, bootstrap, deduplication, durable queue and output self-test passed.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Fetch fresh source data once, drain queued work, then exit")
    parser.add_argument("--status", action="store_true", help="Print saved local health; makes no network requests")
    parser.add_argument("--self-test", action="store_true", help="Run offline smoke test in temporary storage")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        config = Config.from_env()
        if args.status:
            if not (config.state_dir / "monitor.sqlite3").exists():
                print(compact_json({"state": "not_started", "channel": config.channel, "telegram_configured": config.telegram_enabled}))
                return 0
            store = Store(config.state_dir)
            try:
                print(json.dumps(store.status(config), ensure_ascii=False, indent=2))
            finally:
                store.close()
            return 0
        with ProcessLock(config.state_dir):
            monitor = Monitor(config)
            def request_stop(*_):
                monitor.stop.set()
            signal.signal(signal.SIGINT, request_stop)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, request_stop)
            return monitor.run(args.once)
    except (ValueError, RuntimeError) as error:
        print("Rose monitor: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
