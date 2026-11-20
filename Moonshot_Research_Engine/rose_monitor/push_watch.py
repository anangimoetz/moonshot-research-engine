"""Persistent Telegram user-session watcher. No scheduled polling or trading.

Authorize interactively once, then run unattended on an always-on host.
Telegram source membership/session and separate delivery-bot setup are required.
"""
import argparse
import asyncio
from datetime import timezone
import getpass
import json
import os
from pathlib import Path
import signal
import sys

try:
    from .monitor import Config, Monitor, ProcessLock, atomic_write, utc_now
except ImportError:
    from monitor import Config, Monitor, ProcessLock, atomic_write, utc_now


class SetupError(Exception):
    pass


def message_post(message, channel):
    """No message text or quoted instructions are executed."""
    published = message.date.astimezone(timezone.utc).isoformat()
    media = getattr(message, "photo", None) or getattr(message, "document", None)
    media_id = str(getattr(media, "id", "")) or None
    edited = getattr(message, "edit_date", None)
    return {"channel": channel, "message_id": message.id, "id": message.id,
            "url": f"https://t.me/{channel}/{message.id}", "published_utc": published,
            "text": message.message or "", "photo_urls": ["telegram-media-review"] if media else [],
            "links": [], "media_id": media_id, "edited_visible": edited is not None,
            "edit_utc": edited.astimezone(timezone.utc).isoformat() if edited else None,
            "source_transport": "telegram_mtproto_push"}


def ingest_event(monitor, message):
    if getattr(message, "date", None) is None or not hasattr(message, "message"):
        return
    monitor.store.ingest([message_post(message, monitor.config.channel)])
    monitor.store.set("last_push_event_at", utc_now())


async def recover_messages(client, entity, monitor):
    # Separate recovered watermark: a newer live event must not skip older gaps.
    floor = monitor.store.get("mtproto_recovered_id")
    if floor is None:
        latest = await client.get_messages(entity, limit=1)
        if latest:
            monitor.store.ingest([message_post(latest[0], monitor.config.channel)], bootstrap=True)
            monitor.store.set("mtproto_recovered_id", latest[0].id)
        else:
            monitor.store.set("mtproto_recovered_id", 0)
        return
    async for message in client.iter_messages(entity, min_id=floor, reverse=True):
        if getattr(message, "date", None) is not None and hasattr(message, "message"):
            monitor.store.ingest([message_post(message, monitor.config.channel)])
        # Commit post/job before advancing cursor. An interrupted replay dedupes.
        monitor.store.set("mtproto_recovered_id", message.id)


async def run(config, authorize=False):
    from telethon import TelegramClient, events, functions
    from telethon.errors import UserNotParticipantError

    api_id = os.environ.get("ROSE_TG_API_ID", "")
    api_hash = os.environ.get("ROSE_TG_API_HASH", "")
    if not api_id.isdigit() or not api_hash or any(c.isspace() for c in api_hash):
        raise SetupError("Set ROSE_TG_API_ID and ROSE_TG_API_HASH privately before authorization.")
    client = TelegramClient(str(config.state_dir / "rose-source"), int(api_id), api_hash,
                            auto_reconnect=True, connection_retries=10, retry_delay=2)
    monitor = None
    try:
        await client.connect()
        if authorize:
            await client.start(phone=lambda: input("Telegram phone number: "),
                               code_callback=lambda: getpass.getpass("Telegram login code (hidden): "),
                               password=lambda: getpass.getpass("Telegram 2FA password (hidden): "))
        elif not await client.is_user_authorized():
            raise SetupError("Authorize this source session locally with push_watch.py --authorize first.")
        entity = await client.get_entity(config.channel)
        try:
            await client(functions.channels.GetParticipantRequest(entity, "me"))
        except UserNotParticipantError:
            raise SetupError("Join the Rose channel in your Telegram app, then rerun this watcher.") from None
        if authorize:
            print("Source session authorized and channel membership verified. Keep the session file private.")
            return
        monitor = Monitor(config)
        monitor.store.set_many({"source_transport": "telegram_mtproto_push", "run_mode": "continuous_push",
                                "last_event_error": None})
        # Recover before handlers are installed. catch_up and a second history
        # scan after installation cover posts in the short registration gap.
        await recover_messages(client, entity, monitor)

        async def incoming(event):
            try:
                ingest_event(monitor, event.message)
            except Exception as error:
                monitor.store.set("last_event_error", type(error).__name__)
                monitor.stop.set()  # supervisor restarts, history replays safely

        client.add_event_handler(incoming, events.NewMessage(chats=entity))
        client.add_event_handler(incoming, events.MessageEdited(chats=entity))
        monitor.start_workers()
        await client.catch_up()
        await recover_messages(client, entity, monitor)
        loop = asyncio.get_running_loop()
        def stop(*_):
            loop.call_soon_threadsafe(monitor.stop.set)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, stop)
        print("Dedicated push watcher connected. No daily digests enabled.", flush=True)
        cycle = 0
        while not monitor.stop.is_set():
            if cycle % 6 == 0:
                # Independent API contact checks health even when Rose is quiet.
                await asyncio.wait_for(client(functions.updates.GetStateRequest()), timeout=20)
                monitor.store.set("last_source_contact_at", utc_now())
            if cycle % 12 == 0:
                await recover_messages(client, entity, monitor)
            monitor.store.set("process_heartbeat_at", utc_now())
            state = monitor.store.status(config)
            state["source"] = "Telegram MTProto persistent connection; no fixed latency guarantee"
            atomic_write(config.state_dir / "status.json", json.dumps(state, indent=2) + "\n")
            await asyncio.sleep(5)
            cycle += 1
        if monitor.store.get("last_event_error"):
            raise RuntimeError("Push event persistence failed; restarting for recovery.")
    finally:
        await client.disconnect()
        if monitor:
            monitor.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize", action="store_true")
    args = parser.parse_args()
    try:
        config = Config.from_env()
        if not args.authorize and not config.telegram_enabled:
            raise SetupError("Configure ROSE_BOT_TOKEN and ROSE_CHAT_ID so analyses can reach you.")
        config.state_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(config.state_dir, 0o700)
        with ProcessLock(config.state_dir):
            asyncio.run(run(config, args.authorize))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        # Library exceptions can include account identifiers. Print type only.
        if isinstance(error, SetupError):
            print(str(error), file=sys.stderr)
        else:
            print("Push watcher stopped: " + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
