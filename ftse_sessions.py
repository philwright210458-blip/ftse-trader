"""London exchange sessions, including holidays, DST and early closes.

Session labels are exchange-calendars timestamps (midnight session date).
Actual open/close timestamps are always returned in Europe/London.
"""
from functools import lru_cache
from zoneinfo import ZoneInfo
import pandas as pd
import exchange_calendars as xcals

UK_TZ = ZoneInfo("Europe/London")


@lru_cache(maxsize=1)
def calendar():
    # A bounded explicit range keeps historical imports and future scheduling
    # consistent rather than depending on the library's moving default window.
    return xcals.get_calendar("XLON", start="1990-01-01", end="2035-12-31")


def _day(day):
    value = pd.Timestamp(day)
    if value.tzinfo is not None:
        value = value.tz_convert(UK_TZ).tz_localize(None)
    return value.normalize()


def is_session(day) -> bool:
    return bool(calendar().is_session(_day(day)))


def session_for(day):
    value = _day(day)
    return calendar().date_to_session(value) if is_session(value) else None


def previous_session(day):
    """The latest exchange session strictly before the given UK date."""
    return calendar().date_to_session(_day(day) - pd.Timedelta(days=1), direction="previous")


def session_open_close(day):
    session = session_for(day)
    if session is None:
        raise ValueError(f"{_day(day).date()} is not a London exchange session")
    return (calendar().session_open(session).tz_convert(UK_TZ),
            calendar().session_close(session).tz_convert(UK_TZ))


def sessions_from(day, count):
    if count < 1:
        raise ValueError("Session count must be positive")
    session = session_for(day)
    if session is None:
        raise ValueError("Holding period must start on an exchange session")
    start = calendar().sessions.get_loc(session)
    return calendar().sessions[start:start + count]
