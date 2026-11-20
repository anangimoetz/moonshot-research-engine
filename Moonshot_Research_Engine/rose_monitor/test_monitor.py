"""Offline behavioral checks; no Telegram/exchange requests or credentials."""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

try:
    from .monitor import Config, Monitor, Store, fingerprint, parse_preview, signal_metadata
except ImportError:
    from monitor import Config, Monitor, Store, fingerprint, parse_preview, signal_metadata


CHANNEL = "RoseSignalsPremium"


def post(mid, text="#SOL LONG", edited=False):
    return {"channel": CHANNEL, "message_id": mid, "id": mid, "text": text,
            "url": f"https://t.me/{CHANNEL}/{mid}", "published_utc": "2026-11-18T05:00:00+00:00",
            "timestamp": "2026-11-18T05:00:00+00:00", "photo_urls": [], "links": [], "edited_visible": edited}


class ParserTests(unittest.TestCase):
    def test_nested_caption_timestamp_photos_and_edit(self):
        html = '''<a class="tme_messages_more" data-before="50"></a>
        <div class="tgme_widget_message" data-post="OtherChannel/9"><div class="tgme_widget_message_text">ignore</div></div>
        <div class="tgme_widget_message js-widget_message" data-post="RoseSignalsPremium/51">
          <div class="tgme_widget_message_text">#SOL <b>LONG</b><br>Entry &amp; <a href="https://example.com/">details</a></div>
          <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn.test/image?token=secret')"></a>
          <div class="tgme_widget_message_meta">edited<span class="tgme_widget_message_views">20K</span>
            <a class="tgme_widget_message_date"><time datetime="2026-11-18T05:00:01+00:00">05:00</time></a>
          </div>
        </div>'''
        rows, before = parse_preview(html, CHANNEL)
        self.assertEqual(before, 50)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "#SOL LONG\nEntry & details")
        self.assertEqual(rows[0]["published_utc"], "2026-11-18T05:00:01+00:00")
        self.assertTrue(rows[0]["edited_visible"])
        self.assertEqual(rows[0]["photo_urls"], ["https://cdn.test/image?token=secret"])

    def test_fingerprint_ignores_views_and_cdn_rotations(self):
        first = dict(post(1), views="1K", photo_urls=["https://cdn.test/a"])
        second = dict(first, views="2K", photo_urls=["https://different.test/b"])
        self.assertEqual(fingerprint(first), fingerprint(second))
        self.assertNotEqual(fingerprint(first), fingerprint(dict(second, text="#SOL SHORT")))
        self.assertNotEqual(fingerprint(first), fingerprint(dict(second, edited_visible=True)))

    def test_metadata_does_not_invent_chart_direction(self):
        self.assertEqual(signal_metadata(post(1, "#SOL LONG"))["direction"], "long")
        self.assertEqual(signal_metadata(post(1, "#SOL LONG"))["symbols"], ["SOL"])
        self.assertIsNone(signal_metadata(post(1, "#SOL LONG then SHORT"))["direction"])
        metadata = signal_metadata(dict(post(2, "Chart update"), photo_urls=["https://cdn.test/img"]))
        self.assertTrue(metadata["image_only"])
        self.assertIsNone(metadata["direction"])
        self.assertTrue(signal_metadata(dict(post(3, "#SOL BULLISH"), photo_urls=["https://cdn.test/img"]))["chart_review_required"])

    def test_reply_text_is_not_the_new_signal(self):
        html = '''<div class="tgme_widget_message" data-post="RoseSignalsPremium/50">
        <a class="tgme_widget_message_reply"><div class="tgme_widget_message_text js-message_reply_text">#BLESS BULLISH</div></a>
        <div class="tgme_widget_message_text js-message_text">Eyes</div>
        <a class="tgme_widget_message_date"><time datetime="2026-11-18T05:00:00Z"></time></a></div>'''
        rows, _ = parse_preview(html, CHANNEL)
        self.assertEqual(rows[0]["text"], "Eyes")
        self.assertEqual(signal_metadata(rows[0])["symbols"], [])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_bootstrap_no_historical_alert_then_restart_and_edit(self):
        self.assertEqual(self.store.ingest([post(10)], bootstrap=True)["baseline"], 1)
        self.assertIsNone(self.store.claim_job())
        self.assertEqual(self.store.ingest([post(9)])["baseline"], 1)
        self.assertEqual(self.store.ingest([post(11)])["new"], 1)
        self.assertEqual(self.store.ingest([post(11)])["new"], 0)
        claimed = self.store.claim_job()
        self.assertIsNotNone(claimed)
        self.store.close()
        self.store = Store(self.directory)
        self.store.recover()
        recovered = self.store.claim_job()
        self.assertEqual(recovered["id"], claimed["id"])
        self.store.complete_job(recovered, {"ok": True}, "Test analysis", False)
        self.store.project_alerts()
        self.store.project_alerts()
        self.assertEqual(len((self.directory / "alerts.jsonl").read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(self.store.ingest([post(11, "#SOL SHORT", True)])["edited"], 1)
        self.assertIsNotNone(self.store.claim_job())

    def test_failed_job_retries_are_persistent(self):
        self.store.ingest([post(10)], bootstrap=True)
        self.store.ingest([post(11)])
        job = self.store.claim_job()
        self.store.fail_job(job, "Analysis failed: Timeout")
        self.assertIsNone(self.store.claim_job())
        status = self.store.status(Config(state_dir=self.directory))
        self.assertEqual(status["queue"]["pending"], 1)

    def test_edit_reversion_is_a_new_durable_version(self):
        self.store.ingest([post(10)], bootstrap=True)
        self.store.ingest([post(11)])
        self.store.ingest([post(11, "#SOL SHORT")])
        self.assertEqual(self.store.ingest([post(11)])["edited"], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 3)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.cfg = Config(state_dir=self.directory, max_pages=3, overlap_pages=2)
        self.monitor = Monitor(self.cfg, analyze=lambda p: {"decision": "WAIT", "post_id": p["post_id"]}, formatter=lambda result: "WAIT: test")

    def tearDown(self):
        self.monitor.close()
        self.temporary.cleanup()

    def test_analysis_outbox_and_daily_idempotent(self):
        self.monitor.store.ingest([post(10)], bootstrap=True)
        self.monitor.store.ingest([post(11)])
        self.assertTrue(self.monitor.analyze_one())
        self.assertFalse(self.monitor.analyze_one())
        self.monitor.project()
        item = json.loads((self.directory / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(item["event_key"], "signal:2")
        self.assertEqual(item["telegram_required"], 0)
        now = datetime(2026, 11, 18, 5, 0, tzinfo=timezone.utc)
        self.monitor.config = replace(self.cfg, daily_enabled=True)
        self.assertTrue(self.monitor.maybe_daily(now))
        self.assertFalse(self.monitor.maybe_daily(now))
        self.assertEqual(self.monitor.store.status(self.cfg)["notifications"]["total"], 2)

    def test_interrupted_catchup_preserves_old_floor(self):
        self.monitor.store.ingest([post(10)], bootstrap=True)
        pages = {None: ([post(39), post(40)], 39), 39: ([post(29), post(30)], 29),
                 29: ([post(19), post(20)], 19), 19: ([post(9), post(10)], 9)}
        def fail_old_page(before=None):
            if before is not None:
                raise requests_timeout()
            return pages[before]
        self.monitor.fetch_page = fail_old_page
        with self.assertRaises(TimeoutError):
            self.monitor.poll_once()
        self.assertEqual(self.monitor.store.get("last_message_id"), 40)
        self.assertEqual(self.monitor.store.get("backfill_floor"), 10)
        self.assertEqual(self.monitor.store.get("backfill_before"), 39)
        self.monitor.store.close()
        self.monitor.store = Store(self.directory)
        self.monitor.fetch_page = lambda before=None: pages[before]
        self.monitor.poll_once()
        self.assertEqual(self.monitor.store.get("backfill_before"), 19)
        self.monitor.poll_once()
        self.assertIsNone(self.monitor.store.get("backfill_before"))
        count = self.monitor.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        self.assertEqual(count, 6)

    def test_telegram_error_never_persists_token(self):
        import requests
        self.monitor.config = Config(state_dir=self.directory, bot_token="SECRET_TOKEN", chat_id="123")
        with patch.object(requests, "post", side_effect=requests.Timeout("https://api.telegram.org/botSECRET_TOKEN/sendMessage")):
            with self.assertRaises(Exception) as caught:
                self.monitor.send_telegram("hello")
        self.assertNotIn("SECRET_TOKEN", str(caught.exception))

    def test_telegram_invalid_json_shape_is_a_safe_failure(self):
        import requests
        self.monitor.config = Config(state_dir=self.directory, bot_token="SECRET_TOKEN", chat_id="123")
        with patch.object(requests, "post") as request:
            request.return_value.json.return_value = []
            request.return_value.status_code = 200
            with self.assertRaises(Exception) as caught:
                self.monitor.send_telegram("hello")
        self.assertIn("invalid response", str(caught.exception))

    def test_expired_catchup_skips_live_apis(self):
        called = []
        self.monitor.analyze = lambda p: called.append(p) or {"status": "WAIT"}
        self.monitor.store.ingest([post(10)], bootstrap=True)
        self.monitor.store.ingest([dict(post(11), published_utc="2021-01-01T00:00:00+00:00")])
        self.monitor.analyze_one()
        self.monitor.project()
        item = json.loads((self.directory / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(called, [])
        self.assertEqual(item["analysis"]["analysis"]["classification"], "EXPIRED_REVIEW")


def requests_timeout():
    return TimeoutError("simulated network interruption")


if __name__ == "__main__":
    unittest.main()
