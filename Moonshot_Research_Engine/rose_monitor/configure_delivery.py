"""Enter delivery secrets privately; write a service environment file, then test a DM."""
import argparse
import getpass
import os
from pathlib import Path
import re
import sys

import requests


def bot_call(token, method, data=None):
    # Exception messages from requests can include the token-bearing URL.
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}", json=data or {}, timeout=20
        )
        body = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("Telegram connection failed; credentials have not been printed.") from None
    if not response.ok or not body.get("ok"):
        code = body.get("error_code", response.status_code)
        raise RuntimeError(f"Telegram rejected {method} (code {code}).")
    return body["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="config.local.env")
    parser.add_argument("--test", action="store_true", help="Send one test DM to your selected private chat")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        print(f"Existing file preserved: {output}. Choose a new --output path.")
        return 1
    print("Create your own bot using @BotFather and press Start in its private chat first.")
    token = getpass.getpass("Your bot token (hidden): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise RuntimeError("The bot token has an unexpected format.")
    me = bot_call(token, "getMe")
    print(f"Connected to your delivery bot: @{me.get('username', '(username unavailable)')}")
    updates = bot_call(token, "getUpdates", {"limit": 100, "timeout": 0})
    private_chats = {}
    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        if chat.get("type") == "private":
            private_chats[str(chat["id"])] = chat
    if len(private_chats) == 1:
        chat_id = next(iter(private_chats))
        answer = input(f"Use private chat {chat_id} for your alerts? [y/N] ").strip().lower()
        if answer != "y":
            raise RuntimeError("No delivery destination was selected.")
    else:
        if not private_chats:
            print("No recent private message found. Start/message the bot, then rerun if needed.")
        else:
            print("Recent private chat IDs: " + ", ".join(sorted(private_chats)))
        chat_id = input("Your private Telegram chat ID: ").strip()
        if chat_id not in private_chats:
            raise RuntimeError("That chat has not sent a recent private message to this bot.")
    if not re.fullmatch(r"[0-9]+", chat_id):
        raise RuntimeError("A positive private chat ID is required.")
    payload = (
        f"ROSE_BOT_TOKEN={token}\nROSE_CHAT_ID={chat_id}\n"
        "ROSE_POLL_SECONDS=15\nROSE_DAILY_HOUR_UTC=5\n"
    )
    descriptor = os.open(str(output), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    print(f"Saved credentials to {output}. Keep this file private and out of source control.")
    if os.name == "nt":
        print("On Windows, check the file's Security permissions before sharing this folder.")
    if args.test:
        bot_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "Rose monitor delivery test. Telegram delivery works; continuous source monitoring still requires the monitor service to be running.",
            "disable_web_page_preview": True,
        })
        print("Telegram accepted the test DM. Verify it arrived on your phone.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, EOFError, KeyboardInterrupt) as error:
        print(str(error) or "Setup cancelled.", file=sys.stderr)
        sys.exit(1)
