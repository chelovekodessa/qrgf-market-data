from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


def _observed(day: dt.date) -> dt.date:
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    next_month = dt.date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    day = next_month - dt.timedelta(days=1)
    return day - dt.timedelta(days=(day.weekday() - weekday) % 7)


def _easter(year: int) -> dt.date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def holidays(year: int) -> set[dt.date]:
    result = {
        _observed(dt.date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter(year) - dt.timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(dt.date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(dt.date(year, 12, 25)),
    }
    if year >= 2022:
        result.add(_observed(dt.date(year, 6, 19)))
    next_new_year = _observed(dt.date(year + 1, 1, 1))
    if next_new_year.year == year:
        result.add(next_new_year)
    return result


def is_market_session(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in holidays(day.year)


def latest_completed_session(now: dt.datetime | None = None) -> dt.date:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    eastern = current.astimezone(ZoneInfo("America/New_York"))
    day = eastern.date()
    if eastern.time() < dt.time(17, 0):
        day -= dt.timedelta(days=1)
    while not is_market_session(day):
        day -= dt.timedelta(days=1)
    return day


def effective_window(start: str, end: str, now: dt.datetime | None = None) -> tuple[str, str]:
    old_start = dt.date.fromisoformat(start)
    old_end = dt.date.fromisoformat(end)
    new_end = latest_completed_session(now)
    shift = new_end - old_end
    return (old_start + shift).isoformat(), new_end.isoformat()


def regression_check() -> None:
    utc = dt.timezone.utc
    cases = [
        (dt.datetime(2026, 8, 13, 20, 59, tzinfo=utc), dt.date(2026, 8, 12)),
        (dt.datetime(2026, 8, 13, 21, 0, tzinfo=utc), dt.date(2026, 8, 13)),
        (dt.datetime(2026, 8, 13, 21, 38, 11, tzinfo=utc), dt.date(2026, 8, 13)),
        (dt.datetime(2026, 8, 15, 18, 0, tzinfo=utc), dt.date(2026, 8, 14)),
        (dt.datetime(2026, 9, 8, 20, 0, tzinfo=utc), dt.date(2026, 9, 4)),
        (dt.datetime(2026, 12, 15, 21, 59, tzinfo=utc), dt.date(2026, 12, 14)),
        (dt.datetime(2026, 12, 15, 22, 0, tzinfo=utc), dt.date(2026, 12, 15)),
    ]
    for stamp, expected in cases:
        actual = latest_completed_session(stamp)
        if actual != expected:
            raise AssertionError(f"market-session regression: {stamp.isoformat()} -> {actual}, expected {expected}")
