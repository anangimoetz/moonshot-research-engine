"""Offline push ingestion and gap recovery tests, no credentials or network."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from .monitor import Config, Monitor
from .push_watch import ingest_event, message_post, recover_messages


def message(mid, text="#SOL LONG", photo=None, edit=None):
    return SimpleNamespace(id=mid, message=text, date=datetime.now(timezone.utc),
                           photo=photo, document=None, edit_date=edit)


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.fail_after = None

    async def get_messages(self, entity, limit):
        return self.messages[-limit:]

    async def iter_messages(self, entity, min_id, reverse):
        for m in self.messages:
            if m.id > min_id:
                yield m
                if m.id == self.fail_after:
                    raise ConnectionError("interrupted")


class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.monitor = Monitor(Config(state_dir=Path(self.tmp.name)))

    def tearDown(self):
        self.monitor.close()
        self.tmp.cleanup()

    def test_bootstrap_then_push_dedup_and_edit(self):
        asyncio.run(recover_messages(FakeClient([message(10)]), None, self.monitor))
        self.assertIsNone(self.monitor.store.claim_job())
        ingest_event(self.monitor, message(11))
        ingest_event(self.monitor, message(11))
        first = self.monitor.store.claim_job()
        self.assertIsNotNone(first)
        self.assertIsNone(self.monitor.store.claim_job())
        ingest_event(self.monitor, message(11, "#SOL SHORT", edit=datetime.now(timezone.utc)))
        self.assertIsNotNone(self.monitor.store.claim_job())

    def test_new_event_cannot_skip_older_gap_on_restart(self):
        self.monitor.store.set("mtproto_recovered_id", 10)
        ingest_event(self.monitor, message(15))
        client = FakeClient([message(i) for i in range(11, 16)])
        client.fail_after = 12
        with self.assertRaises(ConnectionError):
            asyncio.run(recover_messages(client, None, self.monitor))
        self.assertEqual(self.monitor.store.get("mtproto_recovered_id"), 12)
        client.fail_after = None
        asyncio.run(recover_messages(client, None, self.monitor))
        self.assertEqual(self.monitor.store.get("mtproto_recovered_id"), 15)
        self.assertEqual(self.monitor.store.status(self.monitor.config)["queue"]["pending"], 5)

    def test_media_change_is_an_edit_and_requires_chart_review(self):
        asyncio.run(recover_messages(FakeClient([message(1)]), None, self.monitor))
        ingest_event(self.monitor, message(2, photo=SimpleNamespace(id=100)))
        ingest_event(self.monitor, message(2, photo=SimpleNamespace(id=101)))
        self.assertEqual(self.monitor.store.status(self.monitor.config)["queue"]["pending"], 2)
        self.assertEqual(message_post(message(3), "RoseSignalsPremium")["source_transport"], "telegram_mtproto_push")

    def test_no_daily_digest_by_default(self):
        self.assertFalse(self.monitor.maybe_daily())
        self.assertFalse(Config.from_env().daily_enabled)


if __name__ == "__main__":
    unittest.main()
