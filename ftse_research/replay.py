"""Progressive, isolated replays of the real research engine.

Replay never downloads prices or opens the operational database. Fictional
scenarios inject explicitly labelled setups to teach the paper rules; recorded
replays discover setups with the production scanner and require caller-supplied
observation timestamps. Neither supplies evidence of executable historical fills.
"""

from copy import deepcopy
import json
from numbers import Real
import re

import pandas as pd

from ftse_sessions import is_session, previous_session, session_open_close
from .engine import ResearchEngine
from .models import Candle, ResearchConfig, Signal, iso, serialise, stamp
from .store import ResearchStore

DEMO_SESSION = "2026-10-16"
MAX_FRAMES = 100
MAX_BUNDLE_BYTES = 12 * 1024 * 1024


def _copy(value):
    """A JSON value copy prevents later frames or callers mutating earlier ones."""
    return json.loads(serialise(value))


def _configuration(configuration, source_kind):
    if configuration is not None and not isinstance(configuration, (dict, ResearchConfig)):
        raise ValueError("Replay configuration must be an explicit field object")
    if isinstance(configuration, ResearchConfig):
        data = configuration.to_dict()
    else:
        data = dict(configuration or {})
    expected_mode = "synthetic-demo" if source_kind == "synthetic" else "paper"
    if "mode" in data and data["mode"] != expected_mode:
        raise ValueError("Replay configuration mode conflicts with the declared source kind")
    data["mode"] = expected_mode
    return ResearchConfig.from_dict(data)


def _clocks(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError("Replay clocks must be an explicit list of timezone-aware timestamps")
    if any(value is None for value in values):
        raise ValueError("Replay clocks cannot contain missing timestamps")
    try:
        result = [stamp(value) for value in values]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Replay clocks must contain valid timezone-aware timestamps") from exc
    if not result or len(result) > MAX_FRAMES:
        raise ValueError(f"Replay requires between 1 and {MAX_FRAMES} clocks")
    if any(right <= left for left, right in zip(result, result[1:])):
        raise ValueError("Replay clocks must be strictly increasing and unique")
    return result


def _decorate_insights(snapshot, replay, title, limitations):
    """Keep standalone report/journal exports visibly separate from forward data."""
    label = "Fictional paper replay" if replay["source_kind"] == "synthetic" else "Recorded observation replay"
    extra = [
        *limitations,
        replay["signal_source"],
        "Current/default or explicitly supplied configuration is applied with current production rules; historical settings are not independently verified.",
        "This separate replay journal is retrospective and is not the operational or forward paper ledger.",
    ]
    for field in ("daily_report", "decision_journal"):
        report = snapshot.get(field)
        if not isinstance(report, dict):
            continue
        report["provenance"] = {
            **report.get("provenance", {}),
            **replay,
            "kind": replay["source_kind"] + "-replay",
            "label": label,
            "scope": "Separate replay journal through this frame clock; not forward paper observations",
        }
        report["limitations"] = list(dict.fromkeys([*report.get("limitations", []), *extra]))
        if field == "daily_report" and isinstance(report.get("text"), str):
            lines = report["text"].splitlines()
            if len(lines) > 1:
                lines[1] = label
            report["text"] = "\n".join(
                [
                    "REPLAY - " + label,
                    "Scenario: " + title,
                    "Setups: " + replay["signal_source"],
                    "Configuration: " + replay["configuration_basis"],
                    replay["rule_basis"],
                    "",
                    *lines,
                    "",
                    "Replay limitations",
                    *extra,
                ]
            )


def _run_scenario(
    *,
    scenario_id,
    title,
    description,
    source_kind,
    session,
    records,
    universe,
    clocks,
    configuration=None,
    injected_signals=(),
    limitations=(),
):
    """Run the production ledger once per observation clock in a fresh journal."""
    if source_kind not in {"synthetic", "recorded"}:
        raise ValueError("Source kind must be synthetic or recorded")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", scenario_id):
        raise ValueError("Scenario id must be a short lowercase slug")
    if not is_session(session):
        raise ValueError("Replay session must be a London trading session")
    times = _clocks(clocks)
    config = _configuration(configuration, source_kind)
    records = sorted(records, key=lambda record: (stamp(record.fetched_at), stamp(record.start), record.ticker))
    injections = sorted(injected_signals, key=lambda signal: (stamp(signal.available_at), signal.ticker))
    if source_kind == "recorded" and injections:
        raise ValueError("Recorded replays cannot inject scenario-authored setups")
    signal_source = (
        "Scenario-authored setup fixtures; production setup discovery is not being demonstrated"
        if source_kind == "synthetic"
        else "Production scanner rules applied to caller-supplied recorded observations"
    )
    provenance = {
        "source_kind": source_kind,
        "signal_source": signal_source,
        "isolated_journal": "in-memory; no operational database is opened",
        "network_access": False,
        "source_labels": sorted({record.source for record in records}),
        "configuration_basis": (
            "caller-supplied configuration" if configuration is not None else "current default ResearchConfig"
        ),
        "historical_configuration_verified": False,
        "rule_basis": "current production scanner and execution rules; historical rule versions are not reconstructed",
        "fill_model": "actual ResearchEngine synthetic paper execution rules",
    }
    frames = []
    next_record = next_signal = 0
    with ResearchStore(":memory:") as store:
        engine = ResearchEngine(store, config)
        engine.set_universe(deepcopy(universe))
        for index, clock in enumerate(times):
            visible = []
            while next_record < len(records) and stamp(records[next_record].fetched_at) <= clock:
                visible.append(records[next_record])
                next_record += 1
            if visible:
                engine.ingest_candles(visible)
            due = []
            while next_signal < len(injections) and stamp(injections[next_signal].available_at) <= clock:
                due.append(injections[next_signal])
                next_signal += 1
            store.set_metadata(
                "collection_health",
                {
                    "state": "ok" if next_record else "not-collected",
                    "last_error": None,
                    "source": source_kind,
                    "at": iso(clock),
                    "replay": True,
                },
            )
            snapshot = engine.run(clock, due if source_kind == "synthetic" else None)
            snapshot["replay"] = {
                **provenance,
                "is_replay": True,
                "not_forward_paper": True,
                "scenario_id": scenario_id,
                "frame_index": index,
                "clock": iso(clock),
                "observations_revealed": next_record,
                "synthetic_setups_revealed": next_signal,
                "session": session,
                "limitations": list(limitations),
            }
            _decorate_insights(snapshot, snapshot["replay"], title, limitations)
            frames.append({"index": index, "at": iso(clock), "snapshot": _copy(snapshot)})
    return {
        "id": scenario_id,
        "title": title,
        "description": description,
        "source_kind": source_kind,
        "session": session,
        "configuration": config.to_dict(),
        "signal_source": signal_source,
        "provenance": provenance,
        "limitations": list(limitations),
        "frames": frames,
    }


def build_bundle(scenarios):
    """Return an independent portable bundle, bounded for local browser replay."""
    scenarios = _copy(list(scenarios))
    if not scenarios or len({item.get("id") for item in scenarios}) != len(scenarios):
        raise ValueError("Replay requires scenarios with unique ids")
    for scenario in scenarios:
        if scenario.get("source_kind") not in {"synthetic", "recorded"}:
            raise ValueError("Each replay scenario requires an explicit source kind")
        _clocks([frame["at"] for frame in scenario.get("frames", [])])
        if any(frame.get("index") != index for index, frame in enumerate(scenario["frames"])):
            raise ValueError("Replay frame indexes must be contiguous from zero")
    generated_at = max(frame["at"] for scenario in scenarios for frame in scenario["frames"])
    kinds = sorted({scenario["source_kind"] for scenario in scenarios})
    bundle = {
        "schema_version": 1,
        "kind": "paper-replay",
        "generated_at": generated_at,
        "provenance": {
            "source_kinds": kinds,
            "network_access": False,
            "operational_journal_modified": False,
            "engine": "ResearchEngine",
            "purpose": "Retrospective explanation of paper rules; not live prices or proof of returns",
            "synthetic_setup_disclosure": "Fictional scenarios contain labelled authored setups; recorded scenarios do not",
        },
        "scenarios": scenarios,
    }
    if len(serialise(bundle).encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise ValueError("Replay bundle exceeds the 12 MiB browser limit; supply fewer clocks or instruments")
    return bundle


def _normalise_record(record):
    supplied_available = None
    has_supplied_available = False
    if isinstance(record, Candle):
        candle = record
    elif isinstance(record, dict):
        data = dict(record)
        has_supplied_available = "available_at" in data
        supplied_available = data.pop("available_at", None)
        try:
            candle = Candle(**data)
        except TypeError as exc:
            raise ValueError("Recorded candle has missing or unknown fields") from exc
    else:
        raise ValueError("Recorded candles must be Candle records or explicit field objects")
    for name in ("ticker", "source", "interval", "quote_unit"):
        if not isinstance(getattr(candle, name), str) or not getattr(candle, name).strip():
            raise ValueError(f"Recorded candle {name} must be an explicit non-empty string")
    if any(getattr(candle, name) is None for name in ("start", "end", "fetched_at")):
        raise ValueError("Recorded candle requires explicit start, end and fetched_at timestamps")
    for name in ("open", "high", "low", "close", "volume"):
        value = getattr(candle, name)
        if not isinstance(value, Real) or isinstance(value, bool):
            raise ValueError(f"Recorded candle {name} must be numeric")
    if candle.adjusted_close is not None and (
        not isinstance(candle.adjusted_close, Real) or isinstance(candle.adjusted_close, bool)
    ):
        raise ValueError("Recorded adjusted close must be numeric or null")
    try:
        candle.validate()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Recorded candle has invalid prices, volume or timezone-aware timestamps") from exc
    if stamp(candle.fetched_at) < stamp(candle.end):
        raise ValueError("Recorded replay requires completed observations; fetched_at cannot predate candle end")
    if has_supplied_available:
        if supplied_available is None:
            raise ValueError("Recorded available_at cannot be null when supplied")
        try:
            matches = stamp(supplied_available) == max(stamp(candle.end), stamp(candle.fetched_at))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Recorded available_at must be a valid timezone-aware timestamp") from exc
        if not matches:
            raise ValueError("Recorded available_at conflicts with the original observation timestamp")
    return candle


def replay_recorded(
    records,
    universe,
    session,
    *,
    clocks=None,
    title="Recorded observation replay",
    scenario_id="recorded",
    configuration=None,
):
    """Replay explicitly supplied recorded prices, preserving actual availability.

    No timestamps, candle gaps, adjusted closes or quote units are inferred.
    Caller assertions about capture provenance are disclosed, not certified.
    """
    if not isinstance(records, (list, tuple)) or not isinstance(universe, (list, tuple)):
        raise ValueError("Recorded candles and universe must be explicit lists")
    if not isinstance(session, str):
        raise ValueError("Recorded session must be an explicit exchange date string")
    if not isinstance(title, str) or not isinstance(scenario_id, str):
        raise ValueError("Replay title and scenario id must be strings")
    records = [_normalise_record(record) for record in records]
    if not records:
        raise ValueError("Recorded replay requires explicit observed candles")
    sources = {record.source for record in records}
    if len(sources) != 1 or any(
        not value.strip() or re.search(r"synthetic|demo|fiction|fixture|test", value, re.I) for value in sources
    ):
        raise ValueError("Recorded replay requires one consistent non-synthetic source label")
    if not is_session(session):
        raise ValueError("Replay session must be a London trading session")
    instruments = list(universe)
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("ticker"), str)
        or not item["ticker"].strip()
        or not isinstance(item.get("quote_unit"), str)
        or item.get("quote_unit") not in {"GBp", "GBP"}
        for item in instruments
    ):
        raise ValueError("Replay universe requires object records with ticker and explicit GBp/GBP quote unit")
    metadata = {item.get("ticker"): item for item in instruments}
    if len(metadata) != len(instruments) or not metadata or None in metadata:
        raise ValueError("Replay universe must contain unique explicit ticker records")
    if any(
        item.get("source_kind", "recorded") != "recorded"
        or not isinstance(item.get("source", ""), str)
        or re.search(r"synthetic|demo|fiction|fixture", item.get("source", ""), re.I)
        for item in instruments
    ):
        raise ValueError("Recorded universe metadata cannot declare synthetic instruments")
    opening, closing = (stamp(value) for value in session_open_close(session))
    has_session = False
    config = _configuration(configuration, "recorded")
    for record in records:
        instrument = metadata.get(record.ticker)
        if instrument is None or instrument.get("quote_unit") != record.quote_unit:
            raise ValueError("Recorded candles must match explicit universe tickers and quote units")
        if record.interval != "1d" and record.interval != f"{config.interval_minutes}m":
            raise ValueError("Recorded intraday candle interval conflicts with replay configuration")
        record_day = str(stamp(record.start).tz_convert("Europe/London").date())
        if not is_session(record_day):
            raise ValueError("Recorded candle date is not a London trading session")
        record_open, record_close = (stamp(value) for value in session_open_close(record_day))
        if record.interval == "1d":
            if stamp(record.start) != record_open or stamp(record.end) != record_close:
                raise ValueError("Daily candle boundaries must match the declared exchange session")
        else:
            elapsed = (stamp(record.start) - record_open).total_seconds() / 60
            expected_end = min(stamp(record.start) + pd.Timedelta(minutes=config.interval_minutes), record_close)
            if (
                elapsed < 0
                or stamp(record.start) >= record_close
                or elapsed % config.interval_minutes != 0
                or stamp(record.end) != expected_end
            ):
                raise ValueError("Recorded intraday boundaries must match the configured exchange candle grid")
        if opening <= stamp(record.start) < closing and record.interval != "1d":
            has_session = True
        if stamp(record.start) >= closing:
            raise ValueError("Recorded replay cannot mix future-session candles")
    if not has_session:
        raise ValueError("Recorded replay requires intraday observations from its declared session")
    if clocks is None:
        observations = {
            stamp(record.fetched_at)
            for record in records
            if record.interval != "1d" and opening <= stamp(record.start) < closing
        }
        clocks = sorted({opening - pd.Timedelta(minutes=15), *observations})
    times = _clocks(clocks)
    if any(clock.tz_convert("Europe/London").date() != pd.Timestamp(session).date() for clock in times):
        raise ValueError("Replay clocks must belong to the declared London session date")
    limitations = [
        "Retrospective reconstruction from caller-supplied observation timestamps; capture provenance is not independently verified.",
        "Original missing bars and source delays are retained. Prices, adjusted closes and times are never filled in.",
        "Historical universe membership, executable quotes, liquidity and borrow availability are not reconstructed.",
        "Production scanner discovery is rerun on recorded observations; later rule changes may differ from original decisions.",
        "Current/default or explicitly supplied configuration is applied, not verified historical strategy or risk settings.",
    ]
    return _run_scenario(
        scenario_id=scenario_id,
        title=title,
        description="Recorded observations revealed at their supplied availability times in a separate paper journal.",
        source_kind="recorded",
        session=session,
        records=records,
        universe=instruments,
        clocks=times,
        configuration=configuration,
        limitations=limitations,
    )


def _time(clock):
    return pd.Timestamp(f"{DEMO_SESSION} {clock}", tz="Europe/London")


def _daily(ticker, yesterday_close):
    previous = str(previous_session(DEMO_SESSION).date())
    earlier = str(previous_session(previous).date())
    result = []
    for day, price in ((earlier, 1000.0), (previous, yesterday_close)):
        opening, closing = session_open_close(day)
        result.append(
            Candle(
                ticker,
                iso(opening),
                iso(closing),
                price,
                price + 1,
                price - 1,
                price,
                10000,
                iso(stamp(closing) + pd.Timedelta(minutes=1)),
                "1d",
                "GBp",
                price,
                "synthetic-replay",
            )
        )
    return result


def _bar(ticker, clock, opening, close, *, high=None, low=None, fetched=None):
    start = _time(clock)
    end = start + pd.Timedelta(minutes=15)
    return Candle(
        ticker,
        iso(start),
        iso(end),
        opening,
        high if high is not None else max(opening, close) + 1,
        low if low is not None else min(opening, close) - 1,
        close,
        1000,
        iso(_time(fetched) if fetched else end + pd.Timedelta(minutes=1)),
        "15m",
        "GBp",
        None,
        "synthetic-replay",
    )


def _fictional(scenario_id, title, description, ticker, yesterday, bars, clocks, signal):
    return _run_scenario(
        scenario_id=scenario_id,
        title=title,
        description=description,
        source_kind="synthetic",
        session=DEMO_SESSION,
        records=[*_daily(ticker, yesterday), *bars],
        universe=[
            {
                "ticker": ticker,
                "company": title + " (fictional)",
                "index": "FTSE 100",
                "quote_unit": "GBp",
                "source": "synthetic",
            }
        ],
        clocks=[_time(clock) for clock in clocks],
        injected_signals=[signal],
        limitations=[
            "Entirely fictional prices and instruments; no market performance evidence.",
            "Setup is authored for this scenario, not discovered by the production scanner.",
            "The actual production paper engine applies unchanged default limits, £100 notional and costs.",
        ],
    )


def build_demo_replay():
    """Three fixed-date fictional scenarios using the unchanged paper engine."""
    ticker = "REPLAY-RISE"
    reversal = _fictional(
        "riser-reversal-short-disabled",
        "Riser reverses, short remains disabled",
        "Yesterday's 6% riser falls after the open. A labelled fixture proposes a short, which the default research policy skips.",
        ticker,
        1060,
        [
            _bar(ticker, "08:00", 1062, 1058),
            _bar(ticker, "08:15", 1058, 1051),
            _bar(ticker, "08:30", 1051, 1046),
            _bar(ticker, "08:45", 1046, 1043),
            _bar(ticker, "09:00", 1043, 1036),
        ],
        ["07:45", "08:00", "08:16", "08:31", "08:46", "09:01", "09:16"],
        Signal(
            ticker,
            "replay-authored-reversal",
            "Short",
            iso(_time("08:45")),
            iso(_time("08:46")),
            1058,
            1022,
            1046,
            "GBp",
            80,
            "Scenario-authored short setup; production discovery is not demonstrated",
        ),
    )
    ticker = "REPLAY-FALL"
    rebound = _fictional(
        "faller-rebound-long",
        "Faller rebounds into a paper long",
        "Yesterday's 6% faller rebounds. The authored setup waits for a future eligible bar and then exits through the actual target and cost rules.",
        ticker,
        940,
        [
            _bar(ticker, "08:00", 938, 941),
            _bar(ticker, "08:15", 941, 945),
            _bar(ticker, "08:30", 945, 949),
            _bar(ticker, "08:45", 949, 951),
            _bar(ticker, "09:00", 951, 953),
            _bar(ticker, "09:15", 953, 963),
            _bar(ticker, "09:30", 963, 970),
        ],
        ["07:45", "08:00", "08:16", "08:31", "08:46", "09:01", "09:16", "09:31", "09:46"],
        Signal(
            ticker,
            "replay-authored-rebound",
            "Long",
            iso(_time("08:45")),
            iso(_time("08:46")),
            937,
            967,
            949,
            "GBp",
            80,
            "Scenario-authored long setup; production discovery is not demonstrated",
        ),
    )
    ticker = "REPLAY-GAP"
    delayed = _fictional(
        "missing-bar-outcome-withheld",
        "A data gap withholds the outcome",
        "One observation arrives late, and a subsequent interval is missing. A later apparent target cannot close the position across that unknown interval.",
        ticker,
        940,
        [
            _bar(ticker, "08:00", 1000, 1001),
            _bar(ticker, "08:15", 1001, 1003, fetched="08:52"),
            _bar(ticker, "08:30", 1003, 1005),
            # 08:45 is deliberately absent. Its stop/target path is unknown.
            _bar(ticker, "09:00", 1010, 1024, high=1030),
        ],
        ["07:45", "08:00", "08:16", "08:31", "08:46", "08:52", "09:16", "10:00", "16:51"],
        Signal(
            ticker,
            "replay-authored-gap",
            "Long",
            iso(_time("08:15")),
            iso(_time("08:16")),
            990,
            1020,
            1001,
            "GBp",
            80,
            "Scenario-authored setup before a later missing data interval",
        ),
    )
    return build_bundle([reversal, rebound, delayed])
