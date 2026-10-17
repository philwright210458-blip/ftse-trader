"""Deterministic, fictional end-to-end demonstration; never market evidence."""

import math

import pandas as pd
from ftse_sessions import is_session, previous_session, session_open_close
from .models import Candle, Signal, iso, stamp

DEMO_UNIVERSE = [
    {
        "ticker": "DEMO-RISE",
        "company": "Fictional North Works",
        "index": "FTSE 100",
        "quote_unit": "GBp",
        "source": "synthetic",
    },
    {
        "ticker": "DEMO-FALL",
        "company": "Fictional Harbour Foods",
        "index": "FTSE 250",
        "quote_unit": "GBp",
        "source": "synthetic",
    },
    {
        "ticker": "DEMO-FLAT",
        "company": "Fictional Vale Services",
        "index": "FTSE 100",
        "quote_unit": "GBp",
        "source": "synthetic",
    },
]


def seed_demo(engine, as_of=None):
    if engine.config.mode != "synthetic-demo":
        raise ValueError("Demonstration requires a separate synthetic-demo configuration/database")
    if engine.store.rows("SELECT id FROM runs LIMIT 1"):
        # Idempotent reruns return the existing experiment rather than rewinding.
        return engine.snapshot(engine.store.metadata("last_engine_at"))
    now = stamp(as_of)
    day = str(now.tz_convert("Europe/London").date())
    if not is_session(day):
        day = str(previous_session(day).date())
    opening, closing = session_open_close(day)
    opening, closing = stamp(opening), stamp(closing)
    previous = str(previous_session(day).date())
    earlier = str(previous_session(previous).date())
    engine.set_universe(DEMO_UNIVERSE)
    records = []
    for i, instrument in enumerate(DEMO_UNIVERSE):
        last_close = [1060.0, 940.0, 1000.0][i]
        for session, close in ((earlier, 1000.0), (previous, last_close)):
            start, end = session_open_close(session)
            records.append(
                Candle(
                    instrument["ticker"],
                    iso(start),
                    iso(end),
                    close,
                    close + 2,
                    close - 2,
                    close,
                    100000,
                    iso(stamp(end) + pd.Timedelta(minutes=1)),
                    "1d",
                    "GBp",
                    close,
                    "synthetic-fixture",
                )
            )
        start = opening
        j = 0
        price = last_close * [1.002, 0.998, 1.0][i]
        while start < closing:
            end = min(start + pd.Timedelta(minutes=engine.config.interval_minutes), closing)
            drift = [-1.4, 1.3, 0.05][i] + math.sin(j / 2) * 0.25
            close = price + drift
            records.append(
                Candle(
                    instrument["ticker"],
                    iso(start),
                    iso(end),
                    price,
                    max(price, close) + 0.6,
                    min(price, close) - 0.6,
                    close,
                    1000 + j * 7,
                    iso(end + pd.Timedelta(minutes=1)),
                    f"{engine.config.interval_minutes}m",
                    "GBp",
                    None,
                    "synthetic-fixture",
                )
            )
            price = close
            j += 1
            start = end
    engine.ingest_candles(records)
    decision = opening + pd.Timedelta(minutes=46)
    reference = [
        row for row in records if row.ticker == "DEMO-FALL" and row.interval != "1d" and stamp(row.end) <= decision
    ][-1].close
    signal = Signal(
        "DEMO-FALL",
        "demonstration-reversal",
        "Long",
        iso(decision - pd.Timedelta(minutes=1)),
        iso(decision),
        reference - 8,
        reference + 12,
        reference,
        "GBp",
        75.0,
        "Fictional reversal fixture; not a production strategy signal",
    )
    engine.run(decision, [signal])
    engine.store.set_metadata("demonstration_note", "Fictional instruments and prices; synthetic evidence only")
    return engine.run(closing + pd.Timedelta(minutes=16), [])
