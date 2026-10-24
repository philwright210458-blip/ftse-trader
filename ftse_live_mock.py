"""Live mock trader: records paper trades at the moment the scanner flags them.

Each full or watch scan calls `record_live_mock` straight after saving its
setups. New ideas are entered in the order they trigger (best score first
within one scan), up to a daily limit, one trade per share per day, and (from
22 Oct 2026) at most one new trade from any one scan, so the day's trades come
from different moments rather than all from the first scan. From 26 Oct 2026
the limit is on trades OPEN at the same time (default 5): a trade that has
stopped out or hit its target frees its slot for a later scan, with a ceiling
of 10 trades a day. The entry
is written to scan_data/live_mock_trades.csv immediately and committed with
the scan, so the decision is fixed before anyone knows how the price moves.

The trade itself is filled and scored later by ftse_outcomes.py using the same
rules as every other idea: entry at the open of the first 15-minute candle
after the scan finished, stop counted first if stop and target fall in the
same candle, dealing costs and stamp duty deducted.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

LIVE_FILENAME = "live_mock_trades.csv"
LIVE_COLUMNS = ["day", "signal_time", "data_time", "mode", "trade_no", "company", "ticker", "index", "setup",
                "direction", "score", "entry", "stop", "target", "risk_pct", "key"]
DEFAULT_MAX_PER_DAY = 10
DEFAULT_MAX_OPEN = 5
CLOSED_STATUSES = ("Stopped out", "Target hit", "Closed at end of day", "Skipped")   # anything else counts as open
DEFAULT_LAST_ENTRY = "15:30"


def setup_key(data_time: str, ticker: str, direction: str, run_time: str, mode: str) -> str:
    """Same key the outcome tracker builds from setups.csv, so the two files line up."""
    return f"{data_time}|{ticker}|{direction}|{run_time}|{mode}"


def read_live(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=LIVE_COLUMNS)
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def still_open(progress_rows: list[dict]) -> int:
    """How many of today's trades are still open (or can't be checked, which counts as open to be safe)."""
    return sum(not str(row.get("progress", {}).get("status", "")).startswith(CLOSED_STATUSES) for row in progress_rows)


def record_live_mock(data_dir: Path, info, candidates, max_per_day: int = DEFAULT_MAX_PER_DAY,
                     last_entry: str = DEFAULT_LAST_ENTRY, max_per_scan: int | None = None,
                     max_open: int | None = None, open_now: int = 0) -> list[dict]:
    """Enter new live mock trades from this scan. Returns the rows added.

    `info` is the scanner's RunInfo; `candidates` are this scan's non-conflicting
    setups (already de-duplicated). Nothing is entered when the scan ran on a
    different day from its data (e.g. a weekend re-run) or after `last_entry`.
    """
    run_day, run_clock = info.run_time[:10], info.run_time[11:16]
    if run_day != info.day or run_clock > last_entry:
        return []
    path = Path(data_dir) / LIVE_FILENAME
    existing = read_live(path)
    today = existing[existing["day"] == info.day]
    slots = max_per_day - len(today)
    if max_open is not None and max_open > 0:
        slots = min(slots, max_open - open_now)  # a closed trade frees its slot
    if max_per_scan is not None and max_per_scan > 0:
        slots = min(slots, max_per_scan)        # spread the day's trades over different scans
    if slots <= 0:
        return []
    taken = set(today["ticker"])
    data_time = info.data_time.isoformat()
    rows = []
    for setup in sorted(candidates, key=lambda s: s.quality_score, reverse=True):
        if setup.conflict or getattr(setup, "stale", False) or setup.ticker in taken:
            continue
        taken.add(setup.ticker)
        rows.append({
            "day": info.day, "signal_time": info.run_time, "data_time": data_time, "mode": info.mode,
            "trade_no": len(today) + len(rows) + 1, "company": setup.company, "ticker": setup.ticker,
            "index": setup.index_label, "setup": setup.setup_type, "direction": setup.direction,
            "score": setup.quality_score, "entry": round(setup.entry, 2), "stop": round(setup.stop, 2),
            "target": round(setup.target, 2), "risk_pct": round(abs(setup.entry - setup.stop) / setup.entry * 100, 3),
            "key": setup_key(data_time, setup.ticker, setup.direction, info.run_time, info.mode)})
        if len(rows) >= slots:
            break
    if rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=LIVE_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
    return rows


# ----------------------------------------------------------------------
# Progress during the day (for the "Today's Mock Trades" sheet of the scan workbook)
# ----------------------------------------------------------------------

DEALING_GBP, STAMP_GBP = 0.10, 0.50       # per £100: dealing on every trade, stamp duty on buys


MARKET_CLOSE = "16:30"                      # UK; same-day mock trades are closed at the last price before this


def mock_progress(row: dict, data: pd.DataFrame | None, minutes: int = 15) -> dict:
    """Provisional state of one live mock trade from completed candles: open, stopped out, target hit, or
    closed at the end of the day once the day's last candle (ending 16:30) is in.

    Same rules as the outcome tracker: filled at the open of the first candle that starts after the
    signal, stop counted first if stop and target fall in the same candle. "At" is the time the candle
    ended (a 15:30-15:45 candle shows 15:45). The 17:05 outcome run is final."""
    result = {"fill": None, "status": "Waiting for entry candle", "time": "", "price": None,
              "gross": None, "costs": None, "net": None}
    if data is None or data.empty:
        result["status"] = "Price check unavailable"
        return result
    signal = pd.Timestamp(row["signal_time"])
    signal = signal.tz_localize(data.index.tz) if signal.tzinfo is None and data.index.tz is not None else signal
    after = data[data.index >= signal]
    if after.empty:
        return result
    long = row["direction"] == "Long"
    stop, target = float(row["stop"]), float(row["target"])
    fill = float(after["open"].iloc[0])
    result["fill"] = round(fill, 2)
    if (long and (fill <= stop or fill >= target)) or (not long and (fill >= stop or fill <= target)):
        result["status"] = "Skipped: opened past the stop or target"
        return result
    status, price, when = "Open", float(after["close"].iloc[-1]), after.index[-1]
    for stamp, bar in after.iterrows():
        o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
        hit_stop, hit_target = (l <= stop, h >= target) if long else (h >= stop, l <= target)
        if hit_stop:
            status, price, when = "Stopped out", (min(o, stop) if long else max(o, stop)), stamp
            break
        if hit_target:
            status, price, when = "Target hit", (max(o, target) if long else min(o, target)), stamp
            break
    ended = when + pd.Timedelta(minutes=minutes)
    if status == "Open" and ended.strftime("%H:%M") >= MARKET_CLOSE:
        status = "Closed at end of day"
    gross = ((price - fill) if long else (fill - price)) / fill * 100
    costs = DEALING_GBP + (STAMP_GBP if long else 0.0)
    result.update(status=status, time=ended.strftime("%H:%M"), price=round(price, 2), gross=round(gross, 2),
                  costs=costs, net=round(gross - costs, 2))
    return result
