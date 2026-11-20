"""UTC-based session context and DST-aware report schedules.

These are analytical windows for a 24/7 crypto market.  Equity cash-hour
anchors are labelled separately, and this module does not claim to verify
exchange holidays, early closes, macro release calendars or price direction.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
YEREVAN = ZoneInfo("Asia/Yerevan")
JAKARTA = ZoneInfo("Asia/Jakarta")
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")

_SCHEDULES = (
    ("daily_utc_close", "Daily UTC candle close", "UTC", 0, 0, False),
    ("london_preopen", "London pre-open brief", "Europe/London", 7, 45, True),
    ("ny_premarket", "New York premarket brief", "America/New_York", 8, 15, True),
    ("ny_open_review", "New York post-open review", "America/New_York", 10, 0, True),
)


def _as_utc(now=None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, (int, float)):
        # Collector code uses milliseconds; POSIX seconds remain convenient.
        value = now / 1000 if abs(now) >= 100_000_000_000 else now
        return datetime.fromtimestamp(value, UTC)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if not isinstance(now, datetime):
        raise TypeError("now must be an aware datetime, ISO timestamp or epoch")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return now.astimezone(UTC)


def _stamp(instant: datetime) -> dict:
    u = instant.astimezone(UTC)
    a, w = u.astimezone(YEREVAN), u.astimezone(JAKARTA)
    return {
        "utc": u.isoformat(),
        "armenia": a.isoformat(),
        "wib": w.isoformat(),
        "display": f"{a:%d %b %Y %H:%M} AMT ({w:%d %b %Y %H:%M} WIB)",
    }


def next_session_runs(now=None, count=3) -> dict:
    """Return future report instants grouped by their actual local timezone.

    Each series contains ``count`` instants strictly after ``now``.  Weekday
    schedules skip Saturday/Sunday in their own timezone, not Yerevan's date.
    Holidays are intentionally not inferred.  No running tasks are created.
    """
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 30:
        raise ValueError("count must be an integer from 1 to 30")
    instant = _as_utc(now)
    schedules = {}
    for key, label, zone_name, hour, minute, weekdays in _SCHEDULES:
        zone = UTC if zone_name == "UTC" else ZoneInfo(zone_name)
        local_date = instant.astimezone(zone).date()
        runs = []
        for offset in range(90):
            day = local_date + timedelta(days=offset)
            if weekdays and day.weekday() >= 5:
                continue
            candidate = datetime.combine(day, time(hour, minute), zone)
            if candidate.astimezone(UTC) <= instant:
                continue
            runs.append({
                **_stamp(candidate),
                "scheduled_local": candidate.isoformat(),
                "timezone": zone_name,
                "timezone_abbreviation": candidate.tzname(),
            })
            if len(runs) == count:
                break
        schedules[key] = {
            "label": label,
            "timezone": zone_name,
            "local_time": f"{hour:02d}:{minute:02d}",
            "weekdays_only": weekdays,
            "holiday_calendar_verified": False,
            "runs": runs,
        }
    return {
        "computed_at": instant.isoformat(),
        "display_order": ["Asia/Yerevan", "Asia/Jakarta"],
        "schedules": schedules,
        "notes": [
            "Named source timezones handle UK/US DST independently.",
            "Weekday report schedules are not exchange holiday calendars.",
            "These are computed instants, not proof of an active scheduler.",
        ],
    }


def _window(instant: datetime, name: str, zone, start: time, end: time,
            weekdays_only: bool) -> dict:
    local = instant.astimezone(zone)
    left = datetime.combine(local.date(), start, zone)
    right = datetime.combine(local.date(), end, zone)
    if right <= left:
        right += timedelta(days=1)
    day_allowed = not weekdays_only or local.weekday() < 5
    return {
        "name": name,
        "timezone": getattr(zone, "key", "UTC"),
        "start": _stamp(left),
        "end": _stamp(right),
        "active": day_allowed and left <= local < right,
        "kind": "ANALYTICAL_CRYPTO_WINDOW",
        "weekdays_only": weekdays_only,
        "holiday_calendar_verified": False,
    }


def session_context(now=None) -> dict:
    """Give clock/window context without converting an hour into an edge."""
    instant = _as_utc(now)
    windows = [
        _window(instant, "Asia", UTC, time(0), time(9), False),
        _window(instant, "London", LONDON, time(8), time(17), True),
        _window(instant, "New York", NEW_YORK, time(8), time(17), True),
    ]
    active = [row["name"] for row in windows if row["active"]]
    anchors = []
    for name, zone, hour, minute, kind in (
        ("London cash-open convention", LONDON, 8, 0, "EQUITY_CASH_ANCHOR"),
        ("NYSE regular cash open", NEW_YORK, 9, 30, "EQUITY_CASH_ANCHOR"),
        ("NYSE regular cash close", NEW_YORK, 16, 0, "EQUITY_CASH_ANCHOR"),
        ("Common US macro release slot", NEW_YORK, 8, 30, "MACRO_TIME_SLOT_ONLY"),
    ):
        local_date = instant.astimezone(zone).date()
        at = datetime.combine(local_date, time(hour, minute), zone)
        anchors.append({
            "name": name,
            "kind": kind,
            "timezone": zone.key,
            **_stamp(at),
            "weekday": local_date.weekday() < 5,
            "event_verified": False,
            "holiday_calendar_verified": False,
        })
    a = instant.astimezone(YEREVAN)
    return {
        "observed_at": instant.isoformat(),
        "current_time": _stamp(instant),
        "display_order": ["Asia/Yerevan", "Asia/Jakarta"],
        "session_hour_yerevan": a.hour + a.minute / 60 + a.second / 3600,
        "session_label": " + ".join(active) if active else "Between analytical windows",
        "session_score": None,
        "active_windows": active,
        "london_ny_overlap": "London" in active and "New York" in active,
        "windows": windows,
        "anchors_today": anchors,
        "next_runs": next_session_runs(instant, count=3),
        "directional_edge": "UNPROVEN",
        "notes": [
            "Crypto trades 24/7; session labels do not predict up/down.",
            "AMT is UTC+4 and WIB UTC+7; WIB is always three hours ahead.",
            "Cash anchors are conventions until the exchange calendar is checked.",
            "08:30 New York is a time slot, not confirmation of a macro release.",
        ],
    }
