"""FTSE 100 / FTSE 250 individual-share scanner.

Keeps the existing range/breakout rules and 3:1 reward:risk model while
counting independent zone tests, using completed intraday candles, and
separating the ranked shortlist from the full audit trail.
"""

import argparse
import csv
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ftse_sessions import is_session, session_open_close

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

try:
    import yfinance as yf
except ImportError:
    print("Missing dependency. Run: pip install -r requirements.txt")
    sys.exit(1)


UK_TZ = ZoneInfo("Europe/London")
UNIVERSE_PATH = Path(__file__).parent / "ftse_universe.csv"
CONFLICT_SCORE_GAP = 15.0  # Long/Short scores closer than this = genuine conflict


@dataclass
class IndexConfig:
    label: str
    min_touches: int = 3
    confirmation_closes: int = 1
    zone_tolerance_pct: float = 0.0015
    swing_lookback: int = 4
    reward_risk: float = 3.0
    min_touch_gap_bars: int = 4
    rearm_move_pct: float = 0.003
    stop_buffer_pct: float = 0.001   # minimum buffer beyond the zone edge
    atr_stop_mult: float = 1.25      # stop buffer: this many typical candle ranges (median of last 14)
    min_risk_pct: float = 0.0015     # skip setups risking less than this % of price
    min_risk_atr: float = 0.75       # ...or less than this many typical candle ranges
    min_zone_width_atr: float = 0.25  # a zone counts as at least this wide when scoring precision
    min_zone_width_pct: float = 0.0003  # ...and never narrower than this share of price


INDEX_RULES = {
    "FTSE 100": IndexConfig(label="FTSE 100"),
    "FTSE 250": IndexConfig("FTSE 250", confirmation_closes=2,
                             zone_tolerance_pct=0.0025, rearm_move_pct=0.004,
                             min_risk_pct=0.0025),
}


@dataclass
class Constituent:
    company: str
    epic: str
    yahoo_ticker: str
    index: str
    kind: str = "company"  # "company" or "trust" (investment trust / fund)


@dataclass
class SwingPoint:
    ts: pd.Timestamp
    price: float
    kind: str


@dataclass
class Zone:
    low: float
    high: float
    touches: list[SwingPoint] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    @property
    def touch_count(self) -> int:
        return len(self.touches)


@dataclass
class TradeSetup:
    company: str
    ticker: str
    index_label: str
    setup_type: str
    direction: str
    zone: Zone
    entry: float
    stop: float
    target: float
    note: str
    quality_score: float = 0.0
    conflict: bool = False
    conflict_note: str = ""
    kind: str = "company"
    last_trade: pd.Timestamp | None = None  # when the newest traded candle closed
    stale: bool = False
    stale_note: str = ""
    filter_note: str = ""   # why the entry rules (from 21 Oct 2026) reject this setup; empty = passes


def load_universe(path: Path = UNIVERSE_PATH) -> list[Constituent]:
    if not path.exists():
        print(f"Can't find {path}. Make sure ftse_universe.csv is in the same folder as this script.")
        sys.exit(1)
    with open(path, newline="", encoding="utf-8") as handle:
        return [Constituent(row["company"], row["epic"], row["yahoo_ticker"], row["index"],
                            (row.get("type") or "company").strip().lower())
                for row in csv.DictReader(handle)]


def fetch_data(ticker: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(ticker, interval=interval, period=period, progress=False, auto_adjust=False)
    if df.empty:
        raise RuntimeError(f"No data returned for {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df.index.name = "datetime"
    return df


def fetch_many(tickers: list[str], interval: str, period: str, chunk: int = 50,
               pause: float = 1.0) -> dict[str, pd.DataFrame]:
    """Download many tickers at once (much faster than one at a time). Missing tickers are left out,
    so the caller can fall back to fetch_data for them."""
    result: dict[str, pd.DataFrame] = {}
    for first in range(0, len(tickers), chunk):
        batch = tickers[first:first + chunk]
        try:
            raw = yf.download(batch, interval=interval, period=period, progress=False, auto_adjust=False,
                              group_by="ticker", threads=True)
        except Exception as error:
            print(f"  batch download failed ({error}); those shares will be fetched one by one")
            continue
        if raw is None or raw.empty:
            continue
        for ticker in batch:
            try:
                frame = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            frame = frame.dropna(how="all")
            if frame.empty or "Close" not in frame.columns:
                continue
            frame = frame.rename(columns=str.lower)
            frame.index.name = "datetime"
            result[ticker] = frame
        if first + chunk < len(tickers):
            time.sleep(pause)
    return result


def interval_minutes(interval: str) -> int | None:
    unit = interval[-1].lower()
    try:
        number = int(interval[:-1])
    except ValueError:
        return None
    return number if unit == "m" else number * 60 if unit == "h" else None


def completed_candles(df: pd.DataFrame, interval: str, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Return completed bars in market time, including single-bar downloads.

    A naive provider index is explicitly interpreted as London market time.
    Historical bars are retained; forming/future bars are never observations.
    """
    result = df.copy()
    result.index = pd.DatetimeIndex(result.index)
    result.index = (result.index.tz_localize(UK_TZ) if result.index.tz is None
                    else result.index.tz_convert(UK_TZ))
    result = result[~result.index.duplicated(keep="last")].sort_index()
    minutes = interval_minutes(interval)
    if minutes is None:
        return result
    now = pd.Timestamp.now(tz=UK_TZ) if now is None else pd.Timestamp(now)
    now = now.tz_localize(UK_TZ) if now.tzinfo is None else now.tz_convert(UK_TZ)
    return result[result.index + pd.Timedelta(minutes=minutes) <= now].copy()


def candle_lag_minutes(df: pd.DataFrame, interval: str) -> float | None:
    """Minutes since the newest candle in the raw download *ended*.

    Negative: the newest candle is still forming, so the feed is live.
    Positive during market hours: the feed is behind by about that long."""
    minutes = interval_minutes(interval)
    if minutes is None or df.empty:
        return None
    last_start = df.index[-1]
    now = pd.Timestamp.now(tz=last_start.tz) if last_start.tzinfo else pd.Timestamp.now()
    return (now - (last_start + pd.Timedelta(minutes=minutes))).total_seconds() / 60


def market_open_now() -> bool:
    """Use actual London exchange hours, holidays and shortened sessions."""
    now = pd.Timestamp.now(tz=UK_TZ)
    if not is_session(now):
        return False
    opening, closing = session_open_close(now)
    return opening <= now < closing


def describe_data_lag(lags: list[float]) -> str:
    """One-line verdict on whether Yahoo's feed is live. Uses the median across
    tickers, because a single illiquid share can go quiet for a while."""
    if not lags or not market_open_now():
        return ""
    middle = float(pd.Series(lags).median())
    if middle < 0:
        return "Data check: newest candle still forming for most tickers, so the feed looks live"
    # A live feed that publishes only finished candles reads 0-15 min, exactly like a feed
    # that runs 15 min behind, so anything up to ~17 min proves nothing either way.
    if middle > 17:
        return (f"Data check: newest candle ended about {middle:.0f} min ago for most tickers, which is more "
                f"than one candle late, so the feed is behind. Check your broker's live price")
    return (f"Data check: newest candle ended about {middle:.0f} min ago. Inconclusive: this looks the same "
            f"for a live feed and a 15-minute-delayed one. Compare the latest price with your broker")


def drop_untraded_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Remove zero-volume candles (no trades). Illiquid names print flat
    placeholder bars that otherwise create fake swing highs/lows."""
    if "volume" in df.columns:
        volume = pd.to_numeric(df["volume"], errors="coerce")
        return df[volume > 0].copy()
    return df


def typical_true_range(df: pd.DataFrame, length: int = 14) -> float:
    """Median true range of the last `length` candles.

    The median ignores one-off giants (the opening-auction candle, an
    overnight gap) that would drag an average, and with it every stop, far
    wider than the share's normal movement."""
    previous_close = df["close"].shift(1)
    true_range = pd.concat([df["high"] - df["low"],
                            (df["high"] - previous_close).abs(),
                            (df["low"] - previous_close).abs()], axis=1).max(axis=1)
    value = true_range.tail(length).median()
    return float(value) if pd.notna(value) else 0.0


def find_swing_points(df: pd.DataFrame, lookback: int) -> list[SwingPoint]:
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    result: list[SwingPoint] = []
    for index in range(lookback, len(df) - lookback):
        if highs[index] == highs[index - lookback:index + lookback + 1].max():
            result.append(SwingPoint(df.index[index], float(highs[index]), "high"))
        if lows[index] == lows[index - lookback:index + lookback + 1].min():
            result.append(SwingPoint(df.index[index], float(lows[index]), "low"))
    return result


def cluster_zones(points: list[SwingPoint], tolerance_pct: float) -> list[Zone]:
    """Cluster with a fixed seed band, avoiding chain-merged ranges."""
    zones: list[Zone] = []
    for point in sorted(points, key=lambda item: item.price):
        for zone in zones:
            if abs(point.price - zone.touches[0].price) <= zone.touches[0].price * tolerance_pct:
                zone.low, zone.high = min(zone.low, point.price), max(zone.high, point.price)
                zone.touches.append(point)
                break
        else:
            zones.append(Zone(point.price, point.price, [point]))
    return zones


def meaningful_touches(zone: Zone, df: pd.DataFrame, cfg: IndexConfig) -> list[SwingPoint]:
    """Count only independently re-tested levels.

    A repeat must be at least four candles later and have a meaningful move
    away from the preceding support/resistance touch before it is counted.
    """
    candidates = sorted(zone.touches, key=lambda point: point.ts)
    if not candidates:
        return []
    accepted = [candidates[0]]
    for point in candidates[1:]:
        previous = accepted[-1]
        between = df.loc[(df.index > previous.ts) & (df.index <= point.ts)]
        if len(between) < cfg.min_touch_gap_bars or between.empty:
            continue
        if previous.kind == "low":
            rearmed = float(between["high"].max()) >= zone.high * (1 + cfg.rearm_move_pct)
        else:
            rearmed = float(between["low"].min()) <= zone.low * (1 - cfg.rearm_move_pct)
        if rearmed:
            accepted.append(point)
    return accepted


def qualify_zones(zones: list[Zone], df: pd.DataFrame, cfg: IndexConfig) -> list[Zone]:
    result = []
    for zone in zones:
        touches = meaningful_touches(zone, df, cfg)
        if len(touches) >= cfg.min_touches:
            prices = [point.price for point in touches]
            result.append(Zone(min(prices), max(prices), touches))
    return result


def near_zone_boundary(price: float, zone: Zone, proximity_pct: float = 0.002) -> str | None:
    band = zone.mid * proximity_pct
    candidates = [("bottom", abs(price - zone.low)), ("top", abs(price - zone.high))]
    candidates = [candidate for candidate in candidates if candidate[1] <= band]
    return min(candidates, key=lambda candidate: candidate[1])[0] if candidates else None


def detect_breakout(df: pd.DataFrame, zone: Zone, confirmation_closes: int) -> str | None:
    recent = df.tail(confirmation_closes + 3)
    if len(recent) < confirmation_closes + 1:
        return None
    closes = recent["close"].to_numpy()
    if any(close < zone.low for close in closes[:-confirmation_closes]) and all(close > zone.low for close in closes[-confirmation_closes:]):
        return "Long"
    if any(close > zone.high for close in closes[:-confirmation_closes]) and all(close < zone.high for close in closes[-confirmation_closes:]):
        return "Short"
    return None


def _is_sane(direction: str, entry: float, stop: float, target: float) -> bool:
    return (direction == "Long" and stop < entry < target) or (direction == "Short" and target < entry < stop)


def quality_score(setup: TradeSetup, as_of: pd.Timestamp | None = None, min_width: float = 0.0) -> float:
    """Score 0–100: independent tests, proximity, precision, recency, setup."""
    zone = setup.zone
    touches = min(zone.touch_count, 6) * 7.0
    edge = zone.low if setup.direction == "Long" else zone.high
    proximity = max(0.0, 25.0 - abs(setup.entry - edge) / zone.mid / 0.002 * 25.0)
    width = max(zone.high - zone.low, min_width)  # a zero-width zone is one price tick, not extra precision
    precision = max(0.0, 15.0 - width / zone.mid / 0.002 * 15.0)
    setup_bonus = 10.0 if setup.setup_type == "Breakout" else 5.0
    recency = 0.0
    if as_of is not None and zone.touches:
        age_hours = max(0.0, (as_of - max(point.ts for point in zone.touches)).total_seconds() / 3600)
        recency = max(0.0, 8.0 - age_hours / 24)
    return round(min(100.0, touches + proximity + precision + setup_bonus + recency), 1)


def build_setups(company: str, ticker: str, label: str, df: pd.DataFrame,
                 zones: list[Zone], cfg: IndexConfig) -> list[TradeSetup]:
    setups: list[TradeSetup] = []
    price = float(df["close"].iloc[-1])
    typical = typical_true_range(df)
    min_risk = max(cfg.min_risk_pct * price, cfg.min_risk_atr * typical)
    min_width = max(cfg.min_zone_width_atr * typical, cfg.min_zone_width_pct * price)
    for zone in zones:
        buffer = max(cfg.stop_buffer_pct * zone.mid, cfg.atr_stop_mult * typical)
        long_stop, short_stop = zone.low - buffer, zone.high + buffer
        candidates: list[tuple[str, str, str, float]] = []
        edge = near_zone_boundary(price, zone)
        if edge == "bottom":
            candidates.append(("Range", "Long", "Price at zone bottom", long_stop))
        elif edge == "top":
            candidates.append(("Range", "Short", "Price at zone top", short_stop))
        direction = detect_breakout(df, zone, cfg.confirmation_closes)
        if direction == "Long":
            candidates.append(("Breakout", "Long", "Reclaimed above zone low after breakdown", long_stop))
        elif direction == "Short":
            candidates.append(("Breakout", "Short", "Reclaimed below zone high after breakout", short_stop))
        for setup_type, side, text, stop in candidates:
            risk = abs(price - stop)
            target = price + cfg.reward_risk * risk if side == "Long" else price - cfg.reward_risk * risk
            if risk < min_risk or not _is_sane(side, price, stop, target):
                continue
            setup = TradeSetup(company, ticker, label, setup_type, side, zone, price, stop, target,
                               f"{text} (meaningful touches={zone.touch_count})")
            setup.quality_score = quality_score(setup, df.index[-1], min_width)
            setups.append(setup)
    return setups


def dedupe_setups(setups: list[TradeSetup]) -> list[TradeSetup]:
    """Best setup per company and direction; one that passes the entry rules beats one that doesn't."""
    best: dict[tuple[str, str], TradeSetup] = {}

    def rank(item: TradeSetup):
        return (not item.conflict, item.quality_score, -(item.zone.high - item.zone.low))

    for setup in setups:
        key = (setup.company, setup.direction)
        current = best.get(key)
        if current is None or rank(setup) > rank(current):
            best[key] = setup
    return list(best.values())


# ----------------------------------------------------------------------
# Entry rules (switched on 21 Oct 2026, after backtests on 20 Oct showed them neutral)
# ATR here = typical_true_range: the median true range of the last 14 candles.
# ----------------------------------------------------------------------

RECLAIM_DISPLACEMENT_ATR = 0.5     # reclaim: close at least this far back across the zone edge
REJECTION_MIN_RANGE_ATR = 0.75     # range: the signal candle's high-low range is at least this
REJECTION_WICK_SHARE = 0.4         # range: wick on the level's side is at least this share of the candle
REJECTION_CLOSE_LOCATION = 0.6     # range: close in the top 40% of the candle for buys (bottom 40% for sells)


def entry_rule_note(setup: TradeSetup, df: pd.DataFrame) -> str:
    """Empty if the setup's signal candle passes its entry rule, otherwise the reason it fails."""
    if len(df) < 15:
        return "Not enough candles to check the entry rule."
    atr = typical_true_range(df)
    bar = df.iloc[-1]
    o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
    long = setup.direction == "Long"
    if atr <= 0:
        return "No typical candle range to measure against."
    if setup.setup_type == "Breakout":
        need = RECLAIM_DISPLACEMENT_ATR * atr
        ok = c >= setup.zone.low + need if long else c <= setup.zone.high - need
        return "" if ok else f"Reclaim close less than {RECLAIM_DISPLACEMENT_ATR} ATR back across the zone edge."
    span = h - l
    if span <= 0 or span < REJECTION_MIN_RANGE_ATR * atr:
        return f"No rejection candle: candle smaller than {REJECTION_MIN_RANGE_ATR} ATR."
    if long:
        wick, location, touched = (min(o, c) - l) / span, (c - l) / span, l <= setup.zone.high
    else:
        wick, location, touched = (h - max(o, c)) / span, (h - c) / span, h >= setup.zone.low
    if not touched:
        return "No rejection candle: the candle did not reach the zone."
    if wick < REJECTION_WICK_SHARE or location < REJECTION_CLOSE_LOCATION:
        return "No rejection candle: wick too short or close not far enough from the level."
    return ""


def apply_entry_rules(setups: list[TradeSetup]) -> int:
    """Exclude (as conflicts) setups that fail the entry rules, and every setup on a share seen both ways.

    Call after annotate_conflicts. Returns how many setups were excluded by these rules."""
    directions: dict[str, set[str]] = {}
    for setup in setups:
        directions.setdefault(setup.company, set()).add(setup.direction)
    excluded = 0
    for setup in setups:
        if len(directions[setup.company]) > 1:
            note = "Level seen both ways (a buy and a sell) in this scan; skipped."
        else:
            note = setup.filter_note
        if note:
            excluded += int(not setup.conflict)
            setup.conflict, setup.conflict_note = True, note
    return excluded


def annotate_conflicts(setups: list[TradeSetup]) -> list[TradeSetup]:
    """Flag companies that show both Long and Short ideas.

    A genuine conflict (both sides score within CONFLICT_SCORE_GAP) excludes
    both. If one side is clearly stronger, only the weaker side is excluded,
    so a weak opposite-direction zone can't veto a strong setup."""
    by_company: dict[str, list[TradeSetup]] = {}
    for setup in setups:
        setup.conflict, setup.conflict_note = False, ""
        by_company.setdefault(setup.company, []).append(setup)
    for items in by_company.values():
        best: dict[str, TradeSetup] = {}
        for setup in items:
            if setup.direction not in best or setup.quality_score > best[setup.direction].quality_score:
                best[setup.direction] = setup
        if len(best) < 2:
            continue
        long_best, short_best = best["Long"], best["Short"]
        if abs(long_best.quality_score - short_best.quality_score) < CONFLICT_SCORE_GAP:
            for setup in items:
                setup.conflict = True
                setup.conflict_note = f"Long and Short score within {CONFLICT_SCORE_GAP:.0f} points; excluded from Top Trades."
        else:
            winner = long_best if long_best.quality_score > short_best.quality_score else short_best
            for setup in items:
                if setup.direction != winner.direction:
                    setup.conflict = True
                    setup.conflict_note = f"Weaker than the opposite {winner.direction} setup; excluded from Top Trades."
    return setups


def mark_stale(setups: list[TradeSetup], newest_close: pd.Timestamp, max_minutes: float) -> int:
    """Flag setups whose newest *traded* candle is well behind the freshest candle in
    this scan. A thin share that hasn't traded for hours still shows its old last
    price as 'current', and can look like it is sitting exactly at a zone edge."""
    count = 0
    for setup in setups:
        setup.stale, setup.stale_note = False, ""
        if setup.last_trade is None:
            continue
        behind = (newest_close - setup.last_trade).total_seconds() / 60
        if behind > max_minutes:
            setup.stale = True
            setup.stale_note = (f"Stale: last traded candle closed {behind:.0f} min before the newest candle in this scan; "
                                f"excluded from Top Trades.")
            count += 1
    return count


# ----------------------------------------------------------------------
# History: one small CSV pair that every run appends to
#   scan_data/runs.csv    one row per scan (which candle it saw, which mode)
#   scan_data/setups.csv  one row per setup flagged in each scan
# The Excel workbook is rebuilt from these after every run, so there is a
# single file to open instead of one per run.
# ----------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
DEFAULT_DATA_DIR = SCRIPT_DIR / "scan_data"
DEFAULT_WORKBOOK = SCRIPT_DIR / "ftse_scan_latest.xlsx"

RUN_COLUMNS = ["day", "as_of", "data_time", "run_time", "mode", "scanned", "failed", "flagged"]
SETUP_COLUMNS = ["day", "as_of", "data_time", "run_time", "mode", "company", "ticker", "index",
                 "setup", "direction", "score", "entry", "stop", "target", "risk_pct",
                 "zone_low", "zone_high", "touches", "conflict"]
NUMERIC_COLUMNS = ["score", "entry", "stop", "target", "risk_pct", "zone_low", "zone_high", "touches"]


@dataclass
class RunInfo:
    data_time: pd.Timestamp   # start of the last completed candle seen (UK time)
    as_of: str                # HH:MM when that candle closed, e.g. "10:15"
    day: str                  # UK date of that candle
    run_time: str
    mode: str                 # "full" or "watch"
    scanned: int
    failed: int
    data_check: str = ""


def make_run_info(data_time: pd.Timestamp, interval: str, mode: str, scanned: int, failed: int) -> RunInfo:
    stamp = data_time.tz_convert(UK_TZ) if data_time.tzinfo else data_time.tz_localize(UK_TZ)
    closes = stamp + pd.Timedelta(minutes=interval_minutes(interval) or 0)
    return RunInfo(stamp, closes.strftime("%H:%M"), stamp.strftime("%Y-%m-%d"),
                   datetime.now(UK_TZ).strftime("%Y-%m-%d %H:%M:%S"), mode, scanned, failed)


def _append_rows(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def _read_table(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    for column in NUMERIC_COLUMNS:
        if column in table.columns:
            table[column] = pd.to_numeric(table[column], errors="coerce")
    if "conflict" in table.columns:
        table["conflict"] = table["conflict"].str.lower().eq("true")
    return table


def record_run(data_dir: Path, info: RunInfo, selected: list[TradeSetup]) -> None:
    _append_rows(data_dir / "runs.csv", RUN_COLUMNS, [{
        "day": info.day, "as_of": info.as_of, "data_time": info.data_time.isoformat(),
        "run_time": info.run_time, "mode": info.mode, "scanned": info.scanned,
        "failed": info.failed, "flagged": len(selected)}])
    rows = [{
        "day": info.day, "as_of": info.as_of, "data_time": info.data_time.isoformat(),
        "run_time": info.run_time, "mode": info.mode, "company": s.company, "ticker": s.ticker,
        "index": s.index_label, "setup": s.setup_type, "direction": s.direction,
        "score": s.quality_score, "entry": round(s.entry, 2), "stop": round(s.stop, 2),
        "target": round(s.target, 2), "risk_pct": round(abs(s.entry - s.stop) / s.entry * 100, 3),
        "zone_low": round(s.zone.low, 2), "zone_high": round(s.zone.high, 2),
        "touches": s.zone.touch_count, "conflict": s.conflict} for s in selected]
    if rows:
        _append_rows(data_dir / "setups.csv", SETUP_COLUMNS, rows)


def is_repeat_candle(runs: pd.DataFrame, info: RunInfo) -> bool:
    """True if the latest recorded scan already saw this exact candle."""
    if runs.empty:
        return False
    last = runs.iloc[-1]
    return last["day"] == info.day and last["as_of"] == info.as_of


def latest_full_watchlist(setups: pd.DataFrame, runs: pd.DataFrame, day: str, limit: int) -> list[str] | None:
    """Tickers from today's most recent full scan, best score first.
    None means there has been no full scan today."""
    full_runs = runs[(runs["day"] == day) & (runs["mode"] == "full")]
    if full_runs.empty:
        return None
    latest = sorted(full_runs["as_of"])[-1]
    rows = setups[(setups["day"] == day) & (setups["as_of"] == latest)
                  & (setups["mode"] == "full") & (~setups["conflict"])]
    rows = rows.sort_values("score", ascending=False)
    return list(dict.fromkeys(rows["ticker"]))[:limit]


def persistence(setups: pd.DataFrame, runs: pd.DataFrame, day: str) -> dict[tuple[str, str], tuple[str, str]]:
    """(ticker, direction) -> ("seen/possible", first as_of) for one day.

    'possible' counts every scan from the first sighting onward, so 4/5 means
    it was flagged in 4 of the 5 scans since it first appeared."""
    todays_scans = sorted(set(runs.loc[runs["day"] == day, "as_of"]))
    flagged = setups[(setups["day"] == day) & (~setups["conflict"])]
    result = {}
    for (ticker, direction), group in flagged.groupby(["ticker", "direction"]):
        seen = sorted(set(group["as_of"]))
        possible = [t for t in todays_scans if t >= seen[0]]
        result[(ticker, direction)] = (f"{len(seen)}/{len(possible)}", seen[0])
    return result


# ----------------------------------------------------------------------
# Workbook (rebuilt from history after every run)
# ----------------------------------------------------------------------

HEADERS = ["Rank", "Company", "Ticker", "Type", "Index", "Setup", "Direction", "Quality Score", "Conflict",
           "Entry", "Stop", "Target", "Reward:Risk", "Risk %", "Zone Low", "Zone High",
           "Meaningful Touches", "Last trade", "Note"]
TOP_HEADERS = HEADERS[:-1] + ["Seen today", "First seen (as of)", "Note"]
LONG_FILL, SHORT_FILL, CONFLICT_FILL, STALE_FILL = "C6EFCE", "FFC7CE", "FFF2CC", "E7E6E6"


def report_timestamp(now: datetime | None = None) -> str:
    stamp = now.astimezone(UK_TZ) if now else datetime.now(UK_TZ)
    return f"Scan generated {stamp.strftime('%Y-%m-%d %H:%M')} UK time"


def _style_header(ws, headers: list[str], widths: dict[int, int] | None = None) -> None:
    ws.append(headers)
    for cell in ws[ws.max_row]:
        cell.font, cell.fill = Font(name="Arial", bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(horizontal="center")
    for number, header in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(number)].width = (widths or {}).get(number, max(11, min(48, len(header) + 4)))
    ws.freeze_panes = "A3"  # a note row is inserted above the header later


def _fill_row(ws, fill: str) -> None:
    for cell in ws[ws.max_row]:
        cell.font, cell.fill = Font(name="Arial"), PatternFill("solid", fgColor=fill)


def _direction_fill(direction: str, conflict: bool = False) -> str:
    return CONFLICT_FILL if conflict else (LONG_FILL if direction == "Long" else SHORT_FILL)


def _kind_label(kind: str | None) -> str:
    return "Trust" if kind == "trust" else "Company"


def _clock(stamp: pd.Timestamp | None) -> str:
    if stamp is None:
        return ""
    return (stamp.tz_convert(UK_TZ) if stamp.tzinfo else stamp).strftime("%H:%M")


def _append_setup(ws, setup: TradeSetup, rank: int | str = "", extra: tuple = ()) -> None:
    rr = abs(setup.target - setup.entry) / abs(setup.entry - setup.stop) if setup.entry != setup.stop else 0
    row = [rank, setup.company, setup.ticker, _kind_label(setup.kind), setup.index_label, setup.setup_type, setup.direction,
           setup.quality_score, "Yes" if setup.conflict else "No", round(setup.entry, 2), round(setup.stop, 2),
           round(setup.target, 2), round(rr, 2), round(abs(setup.entry - setup.stop) / setup.entry * 100, 3),
           round(setup.zone.low, 2), round(setup.zone.high, 2), setup.zone.touch_count, _clock(setup.last_trade),
           f"{setup.note} {setup.conflict_note} {setup.stale_note}".strip()]
    ws.append(row[:-1] + list(extra) + row[-1:])
    _fill_row(ws, STALE_FILL if setup.stale else _direction_fill(setup.direction, setup.conflict))


def _add_note(ws, text: str) -> None:
    ws.insert_rows(1)
    ws.cell(1, 1, text)
    ws.cell(1, 1).font = Font(name="Arial", italic=True, size=9)


MOCK_HEADERS = ["#", "Signal time", "Company", "Ticker", "Setup", "Direction", "Entry (filled)", "Stop", "Target",
                "Status so far", "At", "Latest or exit price", "Before costs per £100 (£)", "Costs (£)",
                "After costs per £100 (£)"]


def _write_mock_sheet(ws, rows: list[dict], info: "RunInfo") -> None:
    """Today's live mock trades with their progress so far, so they stay visible after they leave Top Trades."""
    _style_header(ws, MOCK_HEADERS, {1: 5, 2: 11, 3: 30, 4: 10})
    money = '"£"#,##0.00;[Red]-"£"#,##0.00'
    for row in rows:
        p = row.get("progress", {})
        ws.append([int(row["trade_no"]), str(row["signal_time"])[11:16], row["company"], row["ticker"],
                   "Reclaim" if row["setup"] == "Breakout" else row["setup"],
                   "Buy" if row["direction"] == "Long" else "Sell", p.get("fill"), float(row["stop"]),
                   float(row["target"]), p.get("status", ""), p.get("time", ""), p.get("price"), p.get("gross"),
                   p.get("costs"), p.get("net")])
        for column in (13, 14, 15):
            ws.cell(ws.max_row, column).number_format = money
        status = p.get("status", "")
        colour = "D9EAD3" if status == "Target hit" else "F4CCCC" if status == "Stopped out" else None
        if colour:
            for cell in ws[ws.max_row]:
                cell.fill = PatternFill("solid", fgColor=colour)
    if not rows:
        ws.append(["", "", "No mock trades yet today."])
    _add_note(ws, f"Mock trades made today (up to 3), with progress from completed 15-minute candles up to {info.as_of} UK. "
                  "Provisional: Yahoo prices can be up to 15 minutes behind. The final result is worked out at 17:05 "
                  "in ftse_outcomes.xlsx (Live Mock Trades sheet). Costs per £100: 10p dealing, plus 50p stamp duty on buys.")


def _write_today_sheet(ws, setups: pd.DataFrame, runs: pd.DataFrame, day: str, kinds: dict[str, str]) -> None:
    labels = sorted(set(runs.loc[runs["day"] == day, "as_of"]))
    widths = {1: 30, 2: 10, 3: 10, 4: 11, 5: 9}
    _style_header(ws, ["Company", "Ticker", "Type", "Direction", "Seen"] + labels, widths)
    for number in range(6, 6 + len(labels)):
        ws.column_dimensions[get_column_letter(number)].width = 9
    ws.freeze_panes = "F3"
    flagged = setups[(setups["day"] == day) & (~setups["conflict"])]
    if flagged.empty:
        ws.append(["No non-conflicting setups recorded yet today"])
    else:
        table = flagged.pivot_table(index=["company", "ticker", "direction"], columns="as_of",
                                    values="score", aggfunc="max").reindex(columns=labels)
        table["_count"] = table[labels].notna().sum(axis=1)
        table["_latest"] = table[labels].ffill(axis=1).iloc[:, -1]
        table = table.sort_values(["_count", "_latest"], ascending=False)
        for (company, ticker, direction), row in table.iterrows():
            scores = [None if pd.isna(row[label]) else float(row[label]) for label in labels]
            first = next(index for index, score in enumerate(scores) if score is not None)
            seen = sum(score is not None for score in scores)
            ws.append([company, ticker, _kind_label(kinds.get(ticker)), direction, f"{seen}/{len(labels) - first}"] + scores)
            _fill_row(ws, _direction_fill(direction))
    _add_note(ws, "Quality score in each scan. Columns are the time each scan's latest completed candle closed (UK). "
                  "Blank = not flagged in that scan. Conflicting setups are left out.")


def _write_history_sheet(ws, setups: pd.DataFrame, kinds: dict[str, str], days: int = 3) -> None:
    headers = ["As of", "Day", "Mode", "Company", "Ticker", "Type", "Direction", "Setup", "Quality Score",
               "Conflict", "Entry", "Stop", "Target", "Risk %", "Zone Low", "Zone High", "Touches"]
    _style_header(ws, headers)
    if setups.empty:
        ws.append(["No history yet"])
    else:
        recent = setups[setups["day"].isin(sorted(set(setups["day"]))[-days:])]
        recent = recent.sort_values(["day", "as_of", "score"], ascending=[False, False, False])
        for _, row in recent.iterrows():
            ws.append([row["as_of"], row["day"], row["mode"], row["company"], row["ticker"], _kind_label(kinds.get(row["ticker"])),
                       row["direction"], row["setup"], row["score"], "Yes" if row["conflict"] else "No", row["entry"], row["stop"],
                       row["target"], row["risk_pct"], row["zone_low"], row["zone_high"], int(row["touches"])])
            _fill_row(ws, _direction_fill(row["direction"], row["conflict"]))
    _add_note(ws, f"Every flagged setup from the last {days} scanning day(s), newest first.")


def build_workbook(info: RunInfo, all_setups: list[TradeSetup], selected: list[TradeSetup],
                   ranked: list[TradeSetup], setups_history: pd.DataFrame, runs: pd.DataFrame,
                   kinds: dict[str, str] | None = None, mock_rows: list[dict] | None = None) -> Workbook:
    kinds = kinds or {}
    workbook = Workbook()
    top = workbook.active
    top.title = "Top Trades"
    mocks, today, audit, history = (workbook.create_sheet(name) for name in
                                    ("Today's Mock Trades", "Today", "All Setups", "History"))
    _write_mock_sheet(mocks, mock_rows or [], info)

    seen = persistence(setups_history, runs, info.day)
    _style_header(top, TOP_HEADERS)
    if ranked:
        for rank, setup in enumerate(ranked, 1):
            seen_text, first = seen.get((setup.ticker, setup.direction), ("1/1", info.as_of))
            _append_setup(top, setup, rank, (seen_text, first))
    else:
        top.append(["", "No non-conflicting qualifying setups found"] + [""] * (len(TOP_HEADERS) - 2))
    _add_note(top, f"{report_timestamp()} | {info.mode} scan, data to {info.as_of} UK | {len(all_setups)} raw setup(s) | "
                   f"{len(selected)} after de-duplication | {sum(s.stale for s in all_setups)} stale excluded | {len(ranked)} shown | prices mostly in pence"
                   + (f" | {info.data_check}" if info.data_check else ""))

    _write_today_sheet(today, setups_history, runs, info.day, kinds)

    _style_header(audit, HEADERS)
    for setup in sorted(all_setups, key=lambda item: (item.company, -item.quality_score)):
        _append_setup(audit, setup)
    _add_note(audit, f"Every raw setup from this scan ({info.mode}, data to {info.as_of} UK), before de-duplication. "
                     f"Grey rows are stale (their last trade is too far behind the freshest candle).")

    _write_history_sheet(history, setups_history, kinds)
    return workbook


def todays_mock_trades(data_dir: Path, info: "RunInfo", interval: str) -> list[dict]:
    """Today's live mock trades, each with its progress so far (one small price download per trade)."""
    from ftse_live_mock import LIVE_FILENAME, mock_progress, read_live
    live = read_live(Path(data_dir) / LIVE_FILENAME)
    rows = live[live["day"] == info.day].to_dict("records") if not live.empty else []
    for row in rows:
        try:
            data = drop_untraded_bars(completed_candles(fetch_data(row["ticker"], interval, "5d"), interval))
        except Exception:
            data = None
        row["progress"] = mock_progress(row, data)
    return rows


def save_workbook(workbook: Workbook, path: Path) -> Path:
    """Save, falling back to a new name if the file is open in Excel (Windows locks it)."""
    try:
        workbook.save(path)
        return path
    except PermissionError:
        alternative = path.with_name(f"{path.stem}_{datetime.now(UK_TZ).strftime('%H%M%S')}{path.suffix}")
        workbook.save(alternative)
        print(f"\n'{path.name}' is open in Excel, so this scan was saved as '{alternative.name}' instead.\n"
              f"Close the workbook so the next scan can update it.")
        return alternative


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------

def run_scan(universe: list[Constituent], interval: str, period: str, delay: float, batch: bool = False):
    all_setups: list[TradeSetup] = []
    failures: list[str] = []
    latest: pd.Timestamp | None = None
    lags: list[float] = []
    prefetched = fetch_many([c.yahoo_ticker for c in universe], interval, period) if batch else {}
    if batch:
        print(f"  downloaded {len(prefetched)}/{len(universe)} shares in batches; fetching the rest one by one")
    for number, company in enumerate(universe, 1):
        try:
            cfg = INDEX_RULES[company.index]
            raw = prefetched.get(company.yahoo_ticker)
            if raw is None:
                raw = fetch_data(company.yahoo_ticker, interval, period)
                time.sleep(delay)
            lag = candle_lag_minutes(raw, interval)
            if lag is not None:
                lags.append(lag)
            data = drop_untraded_bars(completed_candles(raw, interval))
            if len(data) <= cfg.swing_lookback * 2:
                raise RuntimeError("Not enough completed candles")
            if latest is None or data.index[-1] > latest:
                latest = data.index[-1]
            zones = qualify_zones(cluster_zones(find_swing_points(data, cfg.swing_lookback), cfg.zone_tolerance_pct), data, cfg)
            setups = build_setups(company.company, company.yahoo_ticker, company.index, data, zones, cfg)
            closes = data.index[-1] + pd.Timedelta(minutes=interval_minutes(interval) or 0)
            for setup in setups:
                setup.kind, setup.last_trade = company.kind, closes
                setup.filter_note = entry_rule_note(setup, data)
            all_setups.extend(setups)
            if setups:
                print(f"  [{number}/{len(universe)}] {company.company} ({company.yahoo_ticker}) -> {len(setups)} setup(s)")
        except Exception as error:
            failures.append(f"{company.company} ({company.yahoo_ticker}): {error}")
        if number % 25 == 0:
            print(f"  ...{number}/{len(universe)} scanned, {len(all_setups)} setup(s) found so far")
    return all_setups, failures, latest, lags


def main() -> None:
    parser = argparse.ArgumentParser(description="FTSE 100/250 individual share scanner")
    parser.add_argument("--interval", default="15m", help="Candle interval, e.g. 15m, 1h (default: 15m)")
    parser.add_argument("--period", default="60d", help="History window, e.g. 60d (max for 15m on Yahoo)")
    parser.add_argument("--watch", action="store_true",
                        help="Re-check only the shortlist from today's latest full scan (fast). "
                             "Records nothing if no new candle has formed since the last scan.")
    parser.add_argument("--watch-top", type=int, default=15,
                        help="How many shortlisted tickers to watch (default: 15; 0 = every share, no full scan needed)")
    parser.add_argument("--out", default=str(DEFAULT_WORKBOOK), help="Workbook to update (default: ftse_scan_latest.xlsx next to this script)")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Folder for the run history CSVs (default: scan_data next to this script)")
    parser.add_argument("--limit", type=int, default=None, help="Only scan the first N companies (full scans; for testing)")
    parser.add_argument("--index", choices=["FTSE 100", "FTSE 250"], default=None, help="Scan only one index (full scans)")
    parser.add_argument("--universe", default=str(UNIVERSE_PATH), help="Path to the constituent CSV")
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds to pause between tickers (default: 0.3)")
    parser.add_argument("--max-stale", type=int, default=30,
                        help="Exclude setups whose last traded candle is more than this many minutes behind the "
                             "freshest candle in the scan; 0 turns the guard off (default: 30)")
    parser.add_argument("--exclude-trusts", action="store_true",
                        help="Leave out investment trusts and funds (the 'type' column in ftse_universe.csv)")
    parser.add_argument("--live-max-per-day", type=int, default=3,
                        help="Live mock trader: most new paper trades entered per day, in the order they trigger (default: 3; 0 turns it off)")
    parser.add_argument("--live-max-per-scan", type=int, default=1,
                        help="Live mock trader: most new paper trades from any one scan (default: 1), so the day's "
                             "trades come from different moments rather than all at once")
    parser.add_argument("--one-by-one", action="store_true",
                        help="Download shares one at a time instead of in batches (slower; for troubleshooting)")
    parser.add_argument("--no-entry-rules", action="store_true",
                        help="Turn off the entry rules added on 21 Oct 2026 (seen-both-ways, rejection candle, reclaim distance)")
    parser.add_argument("--top", type=int, default=10, help="Show top N non-conflicting setups by quality score; 0 means no cap (default: 10)")
    args = parser.parse_args()

    data_dir, workbook_path = Path(args.data_dir), Path(args.out)
    runs = _read_table(data_dir / "runs.csv", RUN_COLUMNS)
    setups_history = _read_table(data_dir / "setups.csv", SETUP_COLUMNS)
    universe = load_universe(Path(args.universe))
    kinds = {company.yahoo_ticker: company.kind for company in universe}
    if args.exclude_trusts:
        if not any(company.kind == "trust" for company in universe):
            print("Note: no 'type' column with trusts in the universe file, so --exclude-trusts has no effect.")
        universe = [company for company in universe if company.kind != "trust"]
    mode = "watch" if args.watch else "full"

    if args.watch and args.watch_top <= 0:
        print(f"Watching all {len(universe)} companies...")
    elif args.watch:
        today = datetime.now(UK_TZ).strftime("%Y-%m-%d")
        tickers = latest_full_watchlist(setups_history, runs, today, args.watch_top)
        if tickers is None:
            print("No full scan found for today. Run a full scan first:  python ftse_trade_scanner.py")
            sys.exit(1)
        if not tickers:
            print("Today's full scan flagged nothing to watch.")
            return
        universe = [company for company in universe if company.yahoo_ticker in set(tickers)]
        print(f"Watching {len(universe)} shortlisted companies from today's latest full scan...")
    else:
        if args.index:
            universe = [company for company in universe if company.index == args.index]
        if args.limit:
            universe = universe[:args.limit]
        print(f"Scanning {len(universe)} companies ({'/'.join(sorted(set(company.index for company in universe)))})...")

    all_setups, failures, latest, lags = run_scan(universe, args.interval, args.period, args.delay,
                                                   batch=not args.one_by_one)
    if latest is None:
        print("No usable data came back (market closed, or Yahoo unavailable). Nothing recorded.")
        sys.exit(1)

    info = make_run_info(latest, args.interval, mode, len(universe), len(failures))
    info.data_check = describe_data_lag(lags)
    if args.watch and args.watch_top <= 0 and info.day != datetime.now(UK_TZ).strftime("%Y-%m-%d"):
        print(f"\nNo candle from today yet (newest data is from {info.day}). Nothing recorded.")
        return
    if args.watch and is_repeat_candle(runs, info):
        print(f"\nNo new candle since the last scan (data still to {info.as_of}). Nothing recorded.")
        return

    stale_count = 0
    minutes = interval_minutes(args.interval)
    if minutes and args.max_stale > 0:
        stale_count = mark_stale(all_setups, info.data_time + pd.Timedelta(minutes=minutes), args.max_stale)
    fresh = [setup for setup in all_setups if not setup.stale]
    annotate_conflicts(fresh)
    rule_excluded = 0 if args.no_entry_rules else apply_entry_rules(fresh)
    selected = dedupe_setups(fresh)
    ranked = sorted((s for s in selected if not s.conflict), key=lambda s: s.quality_score, reverse=True)
    if args.top > 0:
        ranked = ranked[:args.top]

    record_run(data_dir, info, selected)
    live_rows = []
    if args.live_max_per_day > 0:
        from ftse_live_mock import record_live_mock
        live_rows = record_live_mock(data_dir, info, [s for s in selected if not s.conflict], args.live_max_per_day,
                                     max_per_scan=args.live_max_per_scan)
    runs = _read_table(data_dir / "runs.csv", RUN_COLUMNS)
    setups_history = _read_table(data_dir / "setups.csv", SETUP_COLUMNS)
    mock_rows = todays_mock_trades(data_dir, info, args.interval)
    saved = save_workbook(build_workbook(info, all_setups, selected, ranked, setups_history, runs, kinds, mock_rows),
                          workbook_path)

    print(f"\nScanned {len(universe)} companies ({mode}), data to {info.as_of} UK.")
    if stale_count:
        print(f"{stale_count} setup(s) excluded as stale (last trade more than {args.max_stale} min behind the freshest candle).")
    print(f"{len(all_setups) - stale_count} fresh setup(s) -> {len(selected)} after de-duplication; "
          f"{sum(s.conflict for s in selected)} conflicting/weaker excluded from Top Trades.")
    if rule_excluded:
        print(f"{rule_excluded} setup(s) excluded by the entry rules (seen both ways, no rejection candle, or reclaim too shallow).")
    if info.data_check:
        print(info.data_check)
    if failures:
        print(f"{len(failures)} ticker(s) failed to fetch:")
        for failure in failures[:15]:
            print(f"  - {failure}")
        if len(failures) > 15:
            print(f"  ...and {len(failures) - 15} more")
    for row in live_rows:
        print(f"Live mock trade #{row['trade_no']} today: {row['direction']} {row['company']} ({row['ticker']}), "
              f"entry about {row['entry']}, stop {row['stop']}, target {row['target']}. Filled at the next candle's open.")
    print(f"\nWorkbook updated: {saved}")


if __name__ == "__main__":
    main()
