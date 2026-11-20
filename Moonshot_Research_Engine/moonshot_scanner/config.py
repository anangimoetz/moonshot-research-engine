"""Explicit, environment-backed deployment configuration; no embedded secrets."""
from dataclasses import dataclass, field
import os
import re
from pathlib import Path


@dataclass
class Settings:
    state_dir: Path = field(default_factory=lambda: Path(os.getenv("SCANNER_STATE_DIR", "state/scanner")).resolve())
    token: str = field(default_factory=lambda: os.getenv("SCANNER_TOKEN", ""))
    read_token: str = field(default_factory=lambda: os.getenv("SCANNER_READ_TOKEN", ""))
    persistent_storage: bool = field(default_factory=lambda: os.getenv("SCANNER_PERSISTENT_STORAGE", "0") == "1")
    scan_interval_seconds: int = field(default_factory=lambda: int(os.getenv("SCANNER_SCAN_INTERVAL_SECONDS", "300")))
    auto_scan: bool = field(default_factory=lambda: os.getenv("SCANNER_AUTO_SCAN", "0") == "1")
    session_reports: bool = field(default_factory=lambda: os.getenv("SCANNER_SESSION_REPORTS", "1") == "1")
    exchanges: tuple = field(default_factory=lambda: tuple(x.strip() for x in os.getenv("SCANNER_EXCHANGES", "bitget,binance,bybit").split(",") if x.strip()))
    auto_limit: int = field(default_factory=lambda: int(os.getenv("SCANNER_AUTO_LIMIT", "12")))
    bot_token: str = field(default_factory=lambda: os.getenv("SCANNER_TELEGRAM_BOT_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: os.getenv("SCANNER_TELEGRAM_CHAT_ID", ""))
    webhook_url: str = field(default_factory=lambda: os.getenv("SCANNER_WEBHOOK_URL", ""))
    require_telegram: bool = field(default_factory=lambda: os.getenv("SCANNER_REQUIRE_TELEGRAM", "0") == "1")
    startup_notice: bool = field(default_factory=lambda: os.getenv("SCANNER_STARTUP_NOTICE", "0") == "1")
    stale_job_seconds: int = 900
    max_workers: int = 3

    def validate(self):
        if self.auto_scan and not self.persistent_storage:
            raise ValueError("24/7 auto-scan requires SCANNER_PERSISTENT_STORAGE=1 and a real persistent mount")
        if not 60 <= self.scan_interval_seconds <= 86400:
            raise ValueError("Scan interval must be 60..86400 seconds")
        if not 1 <= self.auto_limit <= 50:
            raise ValueError("Auto limit must be 1..50 per exchange")
        if not self.exchanges or any(x not in {"bitget", "binance", "bybit"} for x in self.exchanges):
            raise ValueError("Supported exchanges: bitget, binance, bybit")
        if bool(self.bot_token) != bool(self.chat_id):
            raise ValueError("Set both Telegram bot token and chat ID, or neither")
        if self.require_telegram and (not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+',self.bot_token) or not re.fullmatch(r'[0-9]+',self.chat_id)):
            raise ValueError('Telegram required: set a valid private bot token and recipient chat ID in Render Environment')
        if self.webhook_url and not self.webhook_url.startswith("https://"):
            raise ValueError("Delivery webhook must use HTTPS")
        return self

    def public_status(self):
        return {"auto_scan_configured": self.auto_scan, "scan_interval_seconds": self.scan_interval_seconds,
                "persistent_storage_declared": self.persistent_storage,
                "telegram_configured": bool(self.bot_token and self.chat_id),
                "webhook_configured": bool(self.webhook_url), "exchanges": list(self.exchanges),
                "source_watcher": "NOT_VERIFIED", "trading": "RESEARCH_ONLY"}
