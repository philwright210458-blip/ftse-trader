"""Read-only explanations over the complete research journal.

Reports use observation time for knowledge and cash-flow recognition. They do
not rerun a strategy, mutate a journal or turn synthetic fills into live trades.
"""

from collections import Counter
import math
import re
import json

from ftse_sessions import is_session, session_open_close
from .models import ResearchConfig, iso, key, stamp

JOURNAL_LIMIT = 150
STATES = ("pending", "open", "closed", "skipped", "missing", "missed")


def _day(value):
    return str(stamp(value).tz_convert("Europe/London").date())


def _number(value):
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _ticker(value):
    return re.sub(r"[^A-Za-z0-9.\-^]", "", str(value or ""))[:40] or None


def _count(number, singular, plural=None):
    return f"{number} {singular if number==1 else plural or singular+'s'}"


def _known(value, at):
    return bool(value) and stamp(value) <= at


def _money(value):
    return "unavailable" if value is None else f"£{value:,.2f}"


def _state_explanation(state, reason="", direction=""):
    reason = str(reason).lower()
    if "short" in reason and "disabled" in reason:
        return "Synthetic short research is disabled. No paper position was opened; a previous rise alone does not justify a short."
    if "configuration" in reason:
        return "The configuration changed after this signal. The old decision was rejected and a new observation is required."
    if "capital" in reason or "unallocated" in reason:
        return "The available virtual capital could not cover this position and its entry charges. No paper position was opened."
    if "maximum" in reason and "position" in reason:
        return "The maximum number of virtual positions was already open. This idea did not receive another position."
    if "loss limit" in reason:
        return "The configured daily virtual loss limit blocked this entry. The system does not force a trade to meet a return target."
    if "gap" in reason and state == "skipped":
        return "The first eligible price had crossed the original stop or target, so the paper entry was rejected."
    if "zero" in reason and "volume" in reason:
        return "The first eligible interval had no recorded traded volume. The system did not treat its placeholder price as a fill."
    if "re-entry" in reason or "already open" in reason:
        return "The position or re-entry rule blocked another trade in this instrument and strategy."
    return {
        "pending": "The signal is waiting for its first eligible future candle interval. It cannot fill at a price from before the signal became available.",
        "open": "A paper position was filled at the first eligible future candle open. Its price is synthetic and is only observed when the completed candle arrives.",
        "closed": "The paper position has a recorded completed-candle exit. Its net result includes the configured fees and purchase tax.",
        "skipped": "This signal was rejected by a configured paper-trading rule. No position was opened.",
        "missing": "The required entry interval is missing. A later price cannot silently replace the first eligible fill interval.",
        "missed": "The eligible entry interval arrived too late or outside the signal window. No retrospective paper entry was invented.",
    }.get(state, "A research decision was recorded; its state is not recognised by this report.")


def _watch_explanation(watch):
    if watch.get("stale"):
        if "three" in str(watch.get("reason", "")).lower():
            return "The opening view needs three completed traded candles before describing a consistent direction. No reversal is assumed."
        return "Usable current-session observations are missing or stale. This view does not establish a fresh executable price."
    status = watch.get("status")
    if status in ("reversal", "continuation"):
        return f"Three completed closes and movement from today's open agree with a {status} of the previous session's direction. This describes observations and does not guarantee a profitable trade."
    if "three" in str(watch.get("reason", "")).lower() or "waiting" in str(watch.get("reason", "")).lower():
        return "The opening view needs three completed traded candles before describing a consistent direction. Waiting is an expected outcome."
    return "The completed candles do not yet qualify as a consistent opening reversal or continuation. A prior-day move alone is not an entry signal."


def build_insights(engine, snapshot, as_of=None):
    """Build journal and daily aggregates without calling ``engine.snapshot``.

    Bounded snapshot arrays are presentation inputs only. All daily totals come
    from complete stored records. Retrospective state reconstruction is labelled
    because signal rows themselves are mutable and original prose is not versioned.
    """
    at = stamp(as_of or snapshot.get("generated_at"))
    session = _day(at)
    import pandas as pd

    day_start = pd.Timestamp(session, tz="Europe/London")
    day_end = day_start + pd.DateOffset(days=1)
    store = engine.store
    events = [item for item in store.payloads("events", "ORDER BY at,id") if _known(item.get("at"), at)]
    runs = [item for item in store.payloads("runs", "ORDER BY completed_at,id") if _known(item.get("completed_at"), at)]
    watches = [
        {**json.loads(item["payload"]), **{name: item[name] for name in ("id", "run_id", "ticker", "session", "at")}}
        for item in store.rows("SELECT * FROM watches WHERE session=? AND at<=? ORDER BY at,id", (session, iso(at)))
    ]
    signals = [
        item for item in store.payloads("signals", "ORDER BY available_at,id") if _known(item.get("available_at"), at)
    ]
    trades = [
        item for item in store.payloads("trades", "ORDER BY entry_at,id") if _known(item.get("entry_observed_at"), at)
    ]
    last_engine = store.metadata("last_engine_at")
    historical = bool(last_engine and at < stamp(last_engine))
    mode = snapshot.get("mode", engine.config.mode)
    provenance = {
        "kind": "synthetic" if mode == "synthetic-demo" else "recorded-paper",
        "mode": mode,
        "label": "Synthetic demonstration" if mode == "synthetic-demo" else "Recorded paper research",
        "scope": "Complete stored journal through the stated observation time; no real orders",
        "historical_reconstruction": historical,
        "live_execution": False,
    }
    limitations = [
        "Paper prices are synthetic candle fills, not verified executable quotes or real orders.",
        "Observation timestamps show when evidence was available; candle execution times remain separate.",
        "A profitable or short sample does not establish an edge or a reliable daily return.",
    ]
    if historical:
        limitations.append(
            "Historical decision states are reconstructed from recorded events. Original signal wording was not versioned, so later reasons are withheld."
        )

    signal_states = {}
    signal_by_id = {item["id"]: item for item in signals}
    event_states = {key(item["id"], state): (item["id"], state) for item in signals for state in STATES}
    for item in signals:
        updated = item.get("updated_at")
        signal_states[item["id"]] = item.get("status", "pending") if not updated or _known(updated, at) else "pending"
    for event in events:
        if event["id"] in event_states:
            identifier, state = event_states[event["id"]]
            signal_states[identifier] = state
    # The trade ledger independently establishes open/closed observations even
    # when a source journal predates signal-state event recording.
    for trade in trades:
        if trade.get("signal_id") in signal_states:
            signal_states[trade["signal_id"]] = "closed" if _known(trade.get("exit_observed_at"), at) else "open"

    today_signals = [item for item in signals if _day(item["available_at"]) == session]
    decision_counts = Counter(signal_states[item["id"]] for item in today_signals)
    decisions = {state: decision_counts[state] for state in STATES}
    decisions.update(
        total=len(today_signals),
        reconciled=sum(decision_counts[state] for state in STATES) == len(today_signals),
        basis="Signals first available on this observation day; later states are excluded",
    )

    closed = [item for item in trades if _known(item.get("exit_observed_at"), at)]
    opened = [item for item in trades if not _known(item.get("exit_observed_at"), at)]
    today_closed = [item for item in closed if _day(item["exit_observed_at"]) == session]
    today_entries = [item for item in trades if _day(item["entry_at"]) == session]
    carried = [item for item in opened if _day(item["entry_at"]) < session]

    def entry_charges(trade):
        return float(trade.get("entry_commission", 0)) + float(trade.get("entry_stamp_duty", 0))

    def pnl_sum(rows):
        values = [_number(item.get("pnl")) for item in rows]
        return sum(values) if all(value is not None for value in values) else None

    net_today = pnl_sum(today_closed)
    total_pnl = pnl_sum(closed)
    balance = (
        engine.config.capital + total_pnl - sum(entry_charges(item) for item in opened)
        if total_pnl is not None
        else None
    )
    entries_observed_today = [item for item in trades if _day(item["entry_observed_at"]) == session]
    change = (
        (
            net_today
            + sum(entry_charges(item) for item in today_closed)
            - sum(entry_charges(item) for item in entries_observed_today)
        )
        if net_today is not None
        else None
    )
    complete_costs = all(
        all(_number(item.get(name)) is not None for name in ("pnl", "gross_pnl", "costs", "entry_commission"))
        for item in today_closed
    )
    gross = sum(item["gross_pnl"] for item in today_closed) if complete_costs else None
    duty = sum(item.get("entry_stamp_duty", 0) for item in today_closed) if complete_costs else None
    commission = (
        sum(
            item["entry_commission"] + item.get("configuration", {}).get("commission_per_side", 0)
            for item in today_closed
        )
        if complete_costs
        else None
    )
    friction = sum(item["costs"] for item in today_closed) - duty - commission if complete_costs else None
    bridge_ok = bool(complete_costs and net_today is not None and abs(gross - commission - duty - net_today) < 1e-7)
    profitable = sum(item.get("pnl", 0) > 1e-9 for item in today_closed)
    losing = sum(item.get("pnl", 0) < -1e-9 for item in today_closed)
    marks = []
    equity = balance
    for trade in opened:
        config = ResearchConfig.from_dict(trade.get("configuration", engine.config.to_dict()))
        bars = [
            bar
            for bar in store.candles(trade["ticker"], f"{config.interval_minutes}m", at)
            if bar["volume"] > 0
            and stamp(bar["end"]) <= at
            and stamp(bar["end"]) >= stamp(trade["entry_at"])
            and bar["quote_unit"] == trade["quote_unit"]
        ]
        latest = bars[-1] if bars else None
        clock = min(at, stamp(session_open_close(session)[1])) if is_session(session) else at
        age = max(0.0, (clock - stamp(latest["end"])).total_seconds() / 60) if latest else None
        stale = latest is None or age > config.max_data_lag_minutes or _day(latest["start"]) != session
        unrealised = None
        if latest:
            exit_price = engine._friction(latest["close"], trade["direction"], False, config)
            factor = 100 if trade["quote_unit"] == "GBp" else 1
            unrealised = (1 if trade["direction"] == "Long" else -1) * (exit_price - trade["entry_price"]) * trade[
                "quantity"
            ] / factor - config.commission_per_side
        marks.append(
            {
                "trade_id": trade["id"],
                "ticker": _ticker(trade["ticker"]),
                "price_at": latest["end"] if latest else None,
                "available_at": latest["available_at"] if latest else None,
                "age_minutes": age,
                "stale": stale,
                "interval_minutes": config.interval_minutes,
                "maximum_mark_age_minutes": config.max_data_lag_minutes,
                "unrealised_after_estimated_exit_gbp": unrealised,
            }
        )
        equity = equity + unrealised if equity is not None and unrealised is not None else None
    metrics = {
        "entries_today": len(today_entries),
        "entries_observed_today": len(entries_observed_today),
        "closed_today": len(today_closed),
        "market_exits_today": sum(_day(item["exit_at"]) == session for item in closed),
        "profitable": profitable,
        "losing": losing,
        "flat": len(today_closed) - profitable - losing,
        "net_closed_pnl_gbp": net_today,
        "gross_fill_pnl_gbp": gross,
        "dealing_commission_gbp": commission,
        "stamp_duty_gbp": duty,
        "friction_gbp": friction,
        "cost_bridge_reconciled": bridge_ok,
        "costs_complete": complete_costs,
        "realised_balance_gbp": balance,
        "realised_balance_change_gbp": change,
        "entry_charges_observed_today_gbp": sum(entry_charges(item) for item in entries_observed_today),
        "entry_charges_reclassified_on_close_gbp": sum(entry_charges(item) for item in today_closed),
        "open_positions": len(opened),
        "carried_positions": len(carried),
        "open_exposure_gbp": sum(item.get("entry_notional", item.get("position_size", 0)) for item in opened),
        "equity_estimate_gbp": equity,
        "equity_stale": any(item["stale"] for item in marks),
        "marks": marks,
        "cashflow_basis": "Exit observation day; entry fees are charged when entry is observed, then reclassified into full trade P/L at close",
        "gross_basis": "Price P/L after spread and slippage, before commission and purchase tax; friction must not be deducted twice",
        "entry_cohort_basis": "Synthetic candle entry day, only for positions already observed by as_of",
    }

    today_watches = [item for item in watches if item.get("session") == session and _day(item["at"]) == session]
    latest_watch = {}
    for item in today_watches:
        latest_watch[item["ticker"]] = item
    # A missing current-day watch record must not inherit yesterday's fresh state.
    universe = store.universe()
    latest = [dict(item) for item in latest_watch.values()]
    for item in latest:
        candle_end = item.get("last_candle_at")
        clock = min(at, stamp(session_open_close(session)[1])) if is_session(session) else at
        item["stale"] = (
            bool(item.get("stale", True))
            or not candle_end
            or (clock - stamp(candle_end)).total_seconds() / 60 > engine.config.max_data_lag_minutes
        )
    watched = {item["ticker"] for item in latest}
    if historical:
        universe = [{"ticker": ticker} for ticker in sorted(watched)]
    watch = {
        "total": len(universe),
        "observed": len(latest),
        "movers": sum(bool(item.get("is_mover")) for item in latest),
        "risers": sum((item.get("previous_change_pct") or 0) > 0 and item.get("is_mover", False) for item in latest),
        "fallers": sum((item.get("previous_change_pct") or 0) < 0 and item.get("is_mover", False) for item in latest),
        **{
            name: sum(item.get("status") == name for item in latest)
            for name in ("reversal", "continuation", "undecided")
        },
        "unavailable": sum(item.get("status") == "unavailable" for item in latest)
        + len([item for item in universe if item["ticker"] not in watched]),
        "stale": sum(bool(item.get("stale", True)) for item in latest)
        + len([item for item in universe if item["ticker"] not in watched]),
        "universe_basis": (
            "Historically observed instruments only; full historic membership is not reconstructed"
            if historical
            else "Current configured universe"
        ),
    }
    candles = store.rows(
        "SELECT ticker,start,end,available_at,volume FROM candles WHERE interval=? AND start>=? AND start<? AND available_at<=? ORDER BY start,ticker",
        (f"{engine.config.interval_minutes}m", iso(day_start), iso(day_end), iso(at)),
    )
    today_candles = [item for item in candles if _day(item["start"]) == session]
    delays = [
        max(0.0, (stamp(item["available_at"]) - stamp(item["end"])).total_seconds() / 60) for item in today_candles
    ]
    gap_events = [
        item
        for item in events
        if _day(item["at"]) == session and item.get("kind") in ("unresolved-data-gap", "unresolved-close")
    ]
    coverage_positions = []
    all_gap_events = [item for item in events if item.get("kind") in ("unresolved-data-gap", "unresolved-close")]
    for trade in opened:
        config = ResearchConfig.from_dict(trade.get("configuration", engine.config.to_dict()))
        warnings = [item for item in all_gap_events if item.get("trade_id") == trade["id"]]
        missing_start = None
        last_expected_end = None
        if is_session(trade.get("session", _day(trade["entry_at"]))):
            _, trade_close = session_open_close(trade.get("session", _day(trade["entry_at"])))
            bars = {
                bar["start"]: bar
                for bar in store.candles(trade["ticker"], f"{config.interval_minutes}m", at, since=trade["entry_at"])
            }
            step = pd.Timedelta(minutes=config.interval_minutes)
            for expected in pd.date_range(stamp(trade["entry_at"]), stamp(trade_close), freq=step, inclusive="left"):
                expected_end = min(expected + step, stamp(trade_close))
                if expected_end > at:
                    break
                last_expected_end = expected_end
                bar = bars.get(iso(expected))
                if not bar or bar["volume"] <= 0 or bar["quote_unit"] != trade["quote_unit"]:
                    missing_start = iso(expected)
                    break
        processed = trade.get("last_processed_end")
        processing_pending = bool(
            warnings and last_expected_end and (not processed or stamp(processed) < last_expected_end)
        )
        # Historical rows do not preserve each processing checkpoint. Retain an
        # observed gap warning rather than silently treating later stored data
        # or a subsequently advanced checkpoint as a past resolution.
        unresolved = bool(missing_start or processing_pending or historical and warnings)
        if unresolved:
            coverage_positions.append(
                {
                    "trade_id": trade["id"],
                    "ticker": _ticker(trade["ticker"]),
                    "entry_session": trade.get("session", _day(trade["entry_at"])),
                    "carried": _day(trade["entry_at"]) < session,
                    "missing_interval_start": missing_start,
                    "last_processed_end": processed if not historical else None,
                    "warning_count": len(warnings),
                    "interval_minutes": config.interval_minutes,
                    "reason": (
                        "Required completed traded interval is absent"
                        if missing_start
                        else (
                            "Recovered observations have not been reconciled through the position ledger"
                            if not historical
                            else "An unresolved warning was observed; historical processing checkpoints are not versioned"
                        )
                    ),
                }
            )
    today_runs = [item for item in runs if _day(item["completed_at"]) == session]
    data_quality = {
        "observed_intraday_candles": len(today_candles),
        "zero_volume_candles": sum(item["volume"] <= 0 for item in today_candles),
        "first_observation_delay_minutes": {
            "sample_count": len(delays),
            "minimum": min(delays) if delays else None,
            "maximum": max(delays) if delays else None,
            "median": float(pd.Series(delays).median()) if delays else None,
            "basis": "First stored fetch availability minus candle end; archive backfills can increase this delay, so it does not establish provider latency",
        },
        "unresolved_coverage_events": len(gap_events),
        "new_gap_warnings_today": len(gap_events),
        "open_positions_with_unresolved_coverage": len(coverage_positions),
        "unresolved_open_positions": coverage_positions,
        "coverage_event_basis": "New warning events on this observation day; current unresolved open positions are counted separately, including carry-over",
        "runs": len(today_runs),
        "failed_or_degraded_runs": sum(item.get("status") not in ("ok", "done") for item in today_runs),
        "missing_watch_instruments": max(0, len(universe) - len(latest_watch)),
        "equity_stale": metrics["equity_stale"],
        "coverage_verified": False,
        "coverage_note": "No event is not proof of complete feed coverage. The journal retains positive-volume and missing-data evidence separately.",
    }

    entries = []

    def add(identifier, when, ticker, kind, title, explanation, state, source, source_id, **extra):
        if _known(when, at) and _day(when) == session:
            entries.append(
                {
                    "id": identifier,
                    "at": iso(when),
                    "ticker": _ticker(ticker),
                    "kind": kind,
                    "title": title,
                    "explanation": explanation,
                    "status": state,
                    "source": source,
                    "source_id": source_id,
                    **extra,
                }
            )

    for item in today_signals:
        eligible = None
        if is_session(item.get("session", session)):
            opening, closing = session_open_close(item.get("session", session))
            config = item.get("configuration", engine.config.to_dict())
            interval = config.get("interval_minutes", engine.config.interval_minutes)
            after = max(stamp(item["available_at"]), stamp(opening))
            expected = stamp(opening) + pd.Timedelta(
                minutes=math.ceil((after - stamp(opening)).total_seconds() / 60 / interval) * interval
            )
            if expected < stamp(closing):
                eligible = iso(expected)
        add(
            key("explanation", item["id"], "available"),
            item["available_at"],
            item["ticker"],
            "signal",
            "Signal became available",
            _state_explanation("pending"),
            "pending",
            "signals",
            item["id"],
            eligible_interval_start=eligible,
            signal_candle_closed_at=item.get("signal_at"),
        )
    for item in today_runs:
        failed = item.get("status") not in ("ok", "done")
        add(
            key("explanation", item["id"]),
            item["completed_at"],
            None,
            "collection",
            "Collection needs attention" if failed else "Research scan completed",
            (
                "Some observations failed or were incomplete. A finished job does not establish a fresh price for every share."
                if failed
                else "The research scan finished. Individual candle age and missing observations still determine which decisions are usable."
            ),
            item.get("status", "unknown"),
            "runs",
            item["id"],
        )
    prior_watch = {}
    for item in today_watches:
        state = (item.get("status"), item.get("stale"), "three" in str(item.get("reason", "")).lower())
        if prior_watch.get(item["ticker"]) == state:
            continue
        prior_watch[item["ticker"]] = state
        add(
            key("watch-explanation", item["id"]),
            item["at"],
            item["ticker"],
            "watch",
            "Opening observations updated",
            _watch_explanation(item),
            item.get("status", "unavailable"),
            "watches",
            item["id"],
        )
    for event in events:
        kind = event.get("kind", "")
        if event["id"] in event_states:
            identifier, state = event_states[event["id"]]
            signal = signal_by_id[identifier]
            # Current prose can include a later state; use it only when the
            # mutable row is known to match this exact event observation.
            reason = (
                signal.get("reason", "")
                if signal.get("updated_at") and stamp(signal["updated_at"]) == stamp(event["at"])
                else ""
            )
            add(
                key("event-explanation", event["id"]),
                event["at"],
                event.get("ticker"),
                "decision",
                {"open": "Paper entry observed", "closed": "Paper exit observed"}.get(
                    state, "Signal decision recorded"
                ),
                _state_explanation(state, reason, signal.get("direction", "")),
                state,
                "events",
                event["id"],
            )
        elif kind in ("unresolved-data-gap", "unresolved-close"):
            add(
                key("event-explanation", event["id"]),
                event["at"],
                event.get("ticker"),
                "data-quality",
                "Position remains unresolved",
                "A required completed traded interval or session-close candle is missing. The position remains open rather than inventing an exit.",
                "missing",
                "events",
                event["id"],
            )
    for trade in trades:
        add(
            key("trade-explanation", trade["id"], "open"),
            trade["entry_observed_at"],
            trade["ticker"],
            "paper-entry",
            "Paper fill recorded",
            f"The queued signal filled at its eligible future candle open. Position value {_money(trade.get('position_size'))}; entry charges {_money(entry_charges(trade))}. This is a synthetic fill, not a real order.",
            "open",
            "trades",
            trade["id"],
            execution_at=trade["entry_at"],
        )
        if _known(trade.get("exit_observed_at"), at):
            reason = {
                "stop": "a stop",
                "gap-stop": "a gap through the stop",
                "target": "the target",
                "gap-target": "a gap through the target",
                "session-close": "the session close",
            }.get(trade.get("reason"), "a completed-candle exit")
            add(
                key("trade-explanation", trade["id"], "closed"),
                trade["exit_observed_at"],
                trade["ticker"],
                "paper-exit",
                "Paper closure recorded",
                f"The paper position closed after {reason}. Net full-trade result {_money(trade.get('pnl'))}, including configured commission and purchase tax. The result is recognised on this observation day.",
                "closed",
                "trades",
                trade["id"],
                execution_at=trade.get("exit_at"),
            )
    kind_order = {
        "collection": 0,
        "watch": 1,
        "signal": 2,
        "decision": 3,
        "paper-entry": 4,
        "paper-exit": 5,
        "data-quality": 6,
    }
    entries.sort(key=lambda item: (item["at"], kind_order.get(item["kind"], 99), item["ticker"] or "", item["id"]))
    journal = {
        "session": session,
        "as_of": iso(at),
        "entries": entries[-JOURNAL_LIMIT:],
        "total_entries": len(entries),
        "truncated": len(entries) > JOURNAL_LIMIT,
        "limit": JOURNAL_LIMIT,
        "chronology": "observation time; candle execution time shown separately",
        "historical_reconstruction": historical,
        "limitations": limitations,
        "provenance": provenance,
    }
    phase = "non-trading-day"
    if is_session(session):
        session_open, session_close = session_open_close(session)
        phase = (
            "pre-open" if at < stamp(session_open) else "in-progress" if at < stamp(session_close) else "session-ended"
        )
    no_data = (
        not today_runs
        and not today_watches
        and not today_signals
        and not today_candles
        and not today_entries
        and not today_closed
    )
    status = (
        "attention-needed"
        if coverage_positions
        else (
            "no-data"
            if no_data
            else (
                "attention-needed"
                if data_quality["failed_or_degraded_runs"] or gap_events or watch["stale"]
                else "recorded"
            )
        )
    )
    headline = (
        "No current-day research observations are recorded"
        if no_data
        else f"{_count(len(today_entries),'paper entry','paper entries')}; {_count(len(today_closed),'closure')} observed; net closed result {_money(net_today)}"
    )
    summary = [
        f"{_count(watch['movers'],'previous-session mover')} {'is' if watch['movers']==1 else 'are'} recorded in today's latest observed watch; {_count(watch['stale'],'instrument')} {'lacks' if watch['stale']==1 else 'lack'} a verified fresh current-day view.",
        f"Decisions: {_count(decisions['total'],'signal')}, including {decisions['pending']} waiting, {decisions['skipped']} skipped, {decisions['missing']} missing and {decisions['missed']} missed.",
        f"{_count(len(opened),'paper position')} {'remains' if len(opened)==1 else 'remain'} open, including {len(carried)} carried from an earlier entry day.",
    ]
    if today_closed:
        summary.append(
            f"The full-trade closed result is {_money(net_today)}. The realised balance changed by {_money(change)} after today's entry charges and closure reclassification."
        )
    else:
        summary.append(
            "No closures were observed today. A zero closed result does not mean unresolved or open positions have no risk."
        )
    if coverage_positions:
        summary.append(
            f"{_count(len(coverage_positions),'open position')} {'has' if len(coverage_positions)==1 else 'have'} unresolved interval coverage, including carried positions. Later prices alone do not establish a valid exit."
        )
    if metrics["equity_stale"]:
        summary.append(
            "Open-position valuation uses stale or missing marks; it must not be treated as a fresh account value."
        )
    summary.insert(
        0,
        (
            "This is a partial-day report; later observations and trades are excluded."
            if phase in ("pre-open", "in-progress")
            else (
                "The exchange session has ended; unresolved or missing observations may still change the recorded outcome."
                if phase == "session-ended"
                else "The exchange is closed for this date; no trading session is assumed."
            )
        ),
    )
    if no_data:
        for field in (
            "net_closed_pnl_gbp",
            "gross_fill_pnl_gbp",
            "dealing_commission_gbp",
            "stamp_duty_gbp",
            "friction_gbp",
            "realised_balance_change_gbp",
        ):
            metrics[field] = None
        metrics["cost_bridge_reconciled"] = False
        metrics["costs_complete"] = False
    text_lines = [
        "FTSE paper research daily report",
        provenance["label"],
        f"Session: {session}",
        f"As of: {iso(at)}",
        f"Session phase: {phase}",
        headline,
        "",
        *summary,
        "",
        "Costs and cash-flow basis",
        f"Closed price P/L after spread and slippage: {_money(metrics['gross_fill_pnl_gbp'])}",
        f"Dealing commission: {_money(metrics['dealing_commission_gbp'])}",
        f"Purchase tax: {_money(metrics['stamp_duty_gbp'])}",
        f"Net full-trade closed P/L: {_money(metrics['net_closed_pnl_gbp'])}",
        "Spread and slippage are already included in price P/L and must not be deducted twice.",
        metrics["cashflow_basis"],
        "",
        "Limitations",
        *limitations,
    ]
    return {
        "decision_journal": journal,
        "daily_report": {
            "session": session,
            "as_of": iso(at),
            "mode": mode,
            "status": status,
            "headline": headline,
            "summary": summary,
            "metrics": metrics,
            "watch": watch,
            "decisions": decisions,
            "data_quality": data_quality,
            "limitations": limitations,
            "session_phase": phase,
            "finality": (
                "provisional"
                if phase in ("pre-open", "in-progress") or status in ("no-data", "attention-needed")
                else "observed-session-ended"
            ),
            "text": "\n".join(text_lines),
            "provenance": provenance,
            "complete_aggregate": True,
            "journal_entries_total": len(entries),
        },
    }
