"""One bounded Yahoo collection attempt; no inferred exchange quote units."""
import pandas as pd

from ftse_sessions import is_session, session_open_close
from .models import Candle, iso, stamp


def _frame(download, ticker, count):
    if not isinstance(download.columns, pd.MultiIndex):
        return download.copy() if count == 1 else pd.DataFrame()
    for level in range(download.columns.nlevels):
        if ticker in download.columns.get_level_values(level):
            return download.xs(ticker, axis=1, level=level).copy()
    return pd.DataFrame()


def collect_yahoo(universe, as_of=None, interval_minutes=15, downloader=None, intraday_period="60d"):
    """Return validated Candle records and per-ticker errors.

    Each batch performs one daily and one intraday download. No credentials,
    installs or repeated requests occur. Provider revisions remain journalled.
    Availability is the actual fetch-completion time, never the scheduler's
    earlier trigger time. ``as_of`` supports deterministic injected fixtures;
    real requests use wall-clock completion.
    """
    if not universe:
        return [], ["No instruments configured"]
    real_fetch = downloader is None
    if downloader is None:
        import yfinance as yf
        downloader = yf.download
    at = stamp(as_of)
    tickers = [item["ticker"] for item in universe]
    records, errors = [], []
    for interval, period in (("1d", "1mo"), (f"{interval_minutes}m", intraday_period)):
        try:
            data = downloader(tickers=" ".join(tickers), interval=interval, period=period,
                              progress=False, auto_adjust=False, group_by="ticker", threads=False, timeout=15)
        except Exception as exc:
            errors.append(f"{interval} collection failed: {type(exc).__name__}")
            continue
        observed = stamp() if real_fetch else at
        for item in universe:
            ticker = item["ticker"]
            frame = _frame(data, ticker, len(tickers)).rename(columns=lambda value: str(value).lower().replace(" ", "_"))
            if frame.empty:
                errors.append(f"{ticker}: no {interval} candles returned")
                continue
            valid = 0
            for start, row in frame.iterrows():
                try:
                    ts = pd.Timestamp(start)
                    if interval == "1d":
                        day = str(ts.date())
                        if not is_session(day):
                            continue
                        opening, closing = session_open_close(day)
                        ts, end = stamp(opening), stamp(closing)
                    else:
                        if ts.tzinfo is None:
                            # yfinance intraday is expected to carry exchange timezone.
                            raise ValueError("Naive intraday provider timestamp")
                        ts = stamp(ts)
                        day = str(ts.tz_convert("Europe/London").date())
                        if not is_session(day):
                            continue
                        opening, closing = session_open_close(day)
                        if ts < stamp(opening) or ts >= stamp(closing):
                            continue
                        end = min(ts + pd.Timedelta(minutes=interval_minutes), stamp(closing))
                    if end > observed:
                        continue
                    adjusted = row.get("adj_close")
                    adjusted = None if adjusted is None or pd.isna(adjusted) else float(adjusted)
                    candle = Candle(ticker, iso(ts), iso(end), float(row["open"]), float(row["high"]),
                                    float(row["low"]), float(row["close"]), float(row["volume"]),
                                    iso(observed), interval, item["quote_unit"], adjusted, "yahoo-delayed-unverified")
                    candle.validate()
                    records.append(candle)
                    valid += 1
                except (ValueError, KeyError, TypeError):
                    continue
            if not valid:
                errors.append(f"{ticker}: no valid completed {interval} candles")
    return records, errors
