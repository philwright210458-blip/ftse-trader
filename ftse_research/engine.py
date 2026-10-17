"""Session-aware research orchestration and conservative next-bar paper ledger."""
from dataclasses import asdict
from datetime import timedelta
import json
import math

import pandas as pd

from ftse_sessions import is_session, previous_session, session_open_close
from .models import ResearchConfig, Signal, iso, key, serialise, stamp


def day_of(value):
    return str(stamp(value).tz_convert("Europe/London").date())


def session_bounds(day):
    opening, closing = session_open_close(day)
    return stamp(opening), stamp(closing)


class ResearchEngine:
    def __init__(self, store, config=None, persist_config=True):
        self.store = store
        self.config = config or ResearchConfig.from_dict(store.metadata("config", {}))
        existing_capital = store.metadata("initial_capital")
        if existing_capital is not None and existing_capital != self.config.capital:
            raise ValueError("Initial virtual capital is immutable; use a new database for another experiment")
        existing_mode = store.metadata("mode")
        if existing_mode is not None and existing_mode != self.config.mode:
            raise ValueError("Synthetic demonstration and market-paper journals must be kept separate")
        if persist_config:
            store.set_metadata("initial_capital", self.config.capital)
            store.set_metadata("mode", self.config.mode)
            store.set_metadata("config", self.config.to_dict())

    def set_universe(self, records):
        self.store.set_universe(records)

    def ingest_candles(self, records):
        return self.store.ingest_candles(records)

    def _intraday(self, ticker, as_of):
        return self.store.candles(ticker, f"{self.config.interval_minutes}m", as_of,
                                  since=stamp(as_of) - pd.Timedelta(days=self.config.history_days))

    def _watch(self, instrument, as_of):
        ticker = instrument["ticker"]
        day = day_of(as_of)
        result = {**instrument, "ticker": ticker, "previous_session": None,
                  "previous_change_pct": None, "opening_gap_pct": None,
                  "intraday_change_pct": None, "last_price": None,
                  "last_candle_at": None, "available_at": None,
                  "status": "unavailable", "stale": True, "is_mover": False,
                  "reason": "No completed current-session candle available"}
        if not is_session(day):
            result["reason"] = "The London exchange is closed for this date"
            return result
        previous = str(previous_session(day).date())
        before_previous = str(previous_session(previous).date())
        result["previous_session"] = previous
        daily = {day_of(row["start"]): row for row in self.store.daily_as_of(ticker, as_of)}
        yesterday = daily.get(previous)
        previous_before = daily.get(before_previous)
        result["previous_close"] = yesterday["close"] if yesterday else None
        result["previous_adjusted_close"] = yesterday.get("adjusted_close") if yesterday else None
        result["opening_price"] = None
        if yesterday and previous_before and yesterday.get("adjusted_close") and previous_before.get("adjusted_close"):
            change = (yesterday["adjusted_close"] / previous_before["adjusted_close"] - 1) * 100
            result["previous_change_pct"] = change
            result["is_mover"] = abs(change) >= self.config.mover_threshold_pct
        else:
            result["previous_data_warning"] = "Both previous-session adjusted closes are required; no reversal assumption made"
        opening, closing = session_bounds(day)
        bars = [row for row in self._intraday(ticker, as_of)
                if opening <= stamp(row["start"]) < closing and row["volume"] > 0]
        if not bars:
            if stamp(as_of) < opening:
                result["reason"] = "Previous movers prepared; waiting for the opening completed candle"
            return result
        latest = bars[-1]
        result.update(last_price=latest["close"], last_candle_at=latest["end"], available_at=latest["available_at"])
        age = max(0, (min(stamp(as_of), closing) - stamp(latest["end"])).total_seconds() / 60)
        result["data_lag_minutes"] = age
        result["stale"] = age > self.config.max_data_lag_minutes
        opening_bar = next((row for row in bars if stamp(row["start"]) == opening), None)
        if opening_bar:
            result["opening_price"] = opening_bar["open"]
            result["intraday_change_pct"] = (latest["close"] / opening_bar["open"] - 1) * 100
            if yesterday:
                result["opening_gap_pct"] = (opening_bar["open"] / yesterday["close"] - 1) * 100
        else:
            result["opening_data_warning"] = "Opening bar missing; gap and opening trend withheld"
        if result["stale"]:
            result["reason"] = "Completed price data is stale; new paper entries are blocked"
            return result
        result["status"] = "undecided"
        if not result["is_mover"]:
            result["reason"] = "Outside previous-day mover threshold; retained for scanner comparison"
        elif len(bars) < 3 or opening_bar is None:
            result["reason"] = "Waiting for three completed traded candles before classifying the opening direction"
        else:
            closes = [bar["close"] for bar in bars[-3:]]
            movement = 1 if closes[0] < closes[1] < closes[2] else -1 if closes[0] > closes[1] > closes[2] else 0
            current_move = result["intraday_change_pct"] or 0
            if not movement or movement * current_move <= 0:
                result["reason"] = "Completed candles do not yet show a consistent opening direction"
            else:
                previous_sign = 1 if result["previous_change_pct"] > 0 else -1
                result["status"] = "continuation" if movement == previous_sign else "reversal"
                result["reason"] = "Three completed closes and the move from today's open agree; descriptive evidence, not a trading guarantee"
        return result

    def watchlist(self, as_of=None):
        at = stamp(as_of)
        return sorted((self._watch(item, at) for item in self.store.universe()),
                      key=lambda row: (not row["is_mover"], -(abs(row["previous_change_pct"] or 0)), row["ticker"]))

    def scanner_signals(self, as_of):
        """Reuse the existing range/breakout rules, with strict observed-data bounds."""
        from ftse_trade_scanner import (INDEX_RULES, build_setups, find_swing_points,
                                       cluster_zones, qualify_zones, dedupe_setups, annotate_conflicts)
        at = stamp(as_of)
        day = day_of(at)
        if not is_session(day):
            return []
        opening, closing = session_bounds(day)
        result = []
        for item in self.store.universe():
            bars = [row for row in self._intraday(item["ticker"], at) if row["volume"] > 0]
            if not bars or stamp(bars[-1]["end"]) <= opening or stamp(bars[-1]["start"]) >= closing:
                continue
            last = bars[-1]
            if (at - stamp(last["end"])).total_seconds() / 60 > self.config.max_data_lag_minutes:
                continue
            frame = pd.DataFrame(bars)
            frame.index = pd.to_datetime(frame.pop("start"), utc=True).dt.tz_convert("Europe/London")
            rule = INDEX_RULES.get(item.get("index"), INDEX_RULES["FTSE 100"])
            swings = find_swing_points(frame, rule.swing_lookback)
            zones = cluster_zones(swings, rule.zone_tolerance_pct)
            zones = qualify_zones(zones, frame, rule)
            setups = annotate_conflicts(dedupe_setups(build_setups(item.get("company", item["ticker"]),
                       item["ticker"], item.get("index", "FTSE 100"), frame, zones, rule)))
            for setup in setups:
                if setup.conflict or setup.stale:
                    continue
                result.append(Signal(item["ticker"], "scanner-" + setup.setup_type.lower(), setup.direction,
                    last["end"], iso(at), setup.stop, setup.target, setup.entry,
                    item["quote_unit"], setup.quality_score, setup.note, setup.zone.low, setup.zone.high))
        return result

    def _register(self, signals, as_of, watches=()):
        contexts = {watch["ticker"]: watch for watch in watches}
        for signal in signals:
            signal = signal if isinstance(signal, Signal) else Signal(**signal)
            signal.validate()
            data = signal.to_dict()
            if stamp(data["available_at"]) > stamp(as_of):
                raise ValueError("Cannot register a signal from the future")
            session = day_of(data["signal_at"])
            # A signal submitted later cannot invent earlier availability. The
            # source-candle time stays separate for audit and deduplication.
            data["available_at"] = iso(max(stamp(data["available_at"]), stamp(as_of)))
            data.update(status="pending", reason=data["reason"], session=session,
                        entry=data["reference_price"], created_at=iso(as_of), configuration=self.config.to_dict(),
                        mover_context=contexts.get(signal.ticker))
            self.store.connection.execute("INSERT OR IGNORE INTO signals VALUES(?,?,?,?,?,?,?,?,?,?)",
                (data["id"], data["ticker"], data["strategy"], data["direction"], session,
                 data["signal_at"], data["available_at"], "pending", data["reason"], serialise(data)))

    def _signal_state(self, signal, status, reason, at):
        signal.update(status=status, reason=reason, updated_at=iso(at))
        self.store.connection.execute("UPDATE signals SET status=?,reason=?,payload=? WHERE id=?",
                                      (status, reason, serialise(signal), signal["id"]))
        self.store.event(key(signal["id"], status), at, "signal-" + status,
                         {"ticker": signal["ticker"], "signal_id": signal["id"], "reason": reason})

    def _positions(self):
        return self.store.payloads("trades", "WHERE status='open' ORDER BY entry_at,id")

    def _balance(self):
        closed = self.store.payloads("trades", "WHERE status='closed'")
        return self.config.capital + sum(trade["pnl"] for trade in closed) - sum(t["entry_commission"] + t.get("entry_stamp_duty", 0) for t in self._positions())

    def _friction(self, price, direction, entry=True, config=None):
        # Spread is a round-trip quote width: half on each execution.
        config = config or self.config
        rate = (config.spread_bps / 2 + config.slippage_bps) / 10000
        side = 1 if direction == "Long" else -1
        return price * (1 + side * rate * (1 if entry else -1))

    def _equity(self, as_of):
        equity = self._balance()
        for trade in self._positions():
            bars = self._intraday(trade["ticker"], as_of)
            mark = bars[-1]["close"] if bars else trade["entry_price"]
            trade_config = ResearchConfig.from_dict(trade["configuration"])
            exit_estimate = self._friction(mark, trade["direction"], False, trade_config)
            factor = 100 if trade["quote_unit"] == "GBp" else 1
            side = 1 if trade["direction"] == "Long" else -1
            equity += side * (exit_estimate - trade["entry_price"]) * trade["quantity"] / factor - trade_config.commission_per_side
        return equity

    def _open_trade(self, signal, bar, at):
        config = self.config
        if signal["direction"] == "Short" and not config.allow_synthetic_shorts:
            return "Synthetic short research is disabled; no assumption of borrow availability"
        positions = self._positions()
        if any(p["ticker"] == signal["ticker"] and p["strategy"] == signal["strategy"] for p in positions):
            return "A position is already open for this instrument and strategy"
        if not config.allow_same_session_reentry:
            previous = self.store.rows("SELECT id FROM trades WHERE ticker=? AND strategy=? AND session=? LIMIT 1",
                                       (signal["ticker"], signal["strategy"], signal["session"]))
            if previous:
                return "Same-session re-entry is disabled"
        if len(positions) >= config.max_positions:
            return "Maximum concurrent virtual positions reached"
        exposure = sum(p.get("entry_notional", p["position_size"] - p["entry_commission"]) for p in positions)
        stamp_rate = config.stamp_duty_pct / 100 if signal["direction"] == "Long" else 0
        entry_cash_required = config.position_size * (1 + stamp_rate) + config.commission_per_side
        if self._balance() - exposure < entry_cash_required:
            return "Insufficient unallocated virtual capital"
        session_start = self.store.metadata("equity-start-" + signal["session"], self.config.capital)
        if self._equity(bar["start"]) <= session_start * (1 - config.daily_loss_limit_pct / 100):
            return "Daily virtual loss limit reached"
        price = self._friction(bar["open"], signal["direction"])
        if not (signal["stop"] < price < signal["target"] if signal["direction"] == "Long"
                else signal["target"] < price < signal["stop"]):
            return "Opening gap has crossed the original stop or target; entry rejected"
        factor = 100 if signal["quote_unit"] == "GBp" else 1
        quantity = config.position_size / (price / factor)
        if quantity <= 0:
            return "Commission leaves no virtual position capital"
        trade = {"id": key("trade", signal["id"]), "signal_id": signal["id"],
                 "ticker": signal["ticker"], "strategy": signal["strategy"],
                 "direction": signal["direction"], "session": signal["session"],
                 "status": "open", "entry_price": price, "entry_at": bar["start"],
                 "entry_observed_at": bar["available_at"], "signal_at": signal["signal_at"],
                 "signal_available_at": signal["available_at"], "quantity": quantity,
                 "position_size": config.position_size, "stop": signal["stop"],
                 "entry_notional": price * quantity / factor,
                 "entry_stamp_duty": price * quantity / factor * stamp_rate,
                 "entry_cash_required": entry_cash_required,
                 "target": signal["target"], "quote_unit": signal["quote_unit"],
                 "entry_commission": config.commission_per_side,
                 "entry_friction": abs(price - bar["open"]) * quantity / factor,
                 "last_processed_end": None, "exit_at": None, "exit_price": None,
                 "pnl": None, "reason": "Queued signal filled at the first eligible future bar open",
                 "model": "synthetic-short-no-borrow-model" if signal["direction"] == "Short" else "synthetic-next-bar-paper",
                 "mover_context": signal.get("mover_context"),
                 "fractional_quantity": True, "configuration": config.to_dict()}
        self.store.connection.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?)",
            (trade["id"], signal["id"], trade["ticker"], trade["strategy"], trade["direction"],
             trade["session"], "open", trade["entry_at"], None, serialise(trade)))
        self._signal_state(signal, "open", trade["reason"], at)
        self.store.event(key(trade["id"], "open"), at, "paper-open", {"ticker": trade["ticker"], "trade_id": trade["id"]})
        return None

    def _exit_trade(self, trade, bar, at):
        if stamp(bar["start"]) < stamp(trade["entry_at"]):
            return
        if trade["last_processed_end"] and stamp(bar["end"]) <= stamp(trade["last_processed_end"]):
            return
        expected_start = stamp(trade["last_processed_end"] or trade["entry_at"])
        if stamp(bar["start"]) != expected_start or bar["volume"] <= 0:
            # A missing/zero-volume interval could hide an earlier stop. Later
            # favourable bars cannot resolve it until the gap is recovered.
            self.store.event(key(trade["id"], "data-gap", iso(expected_start)), at, "unresolved-data-gap",
                             {"ticker": trade["ticker"], "trade_id": trade["id"],
                              "reason": "A completed traded candle is missing; later outcomes withheld"})
            return
        long = trade["direction"] == "Long"
        raw_exit, reason = None, None
        stop, target = trade["stop"], trade["target"]
        if (bar["open"] <= stop if long else bar["open"] >= stop):
            raw_exit, reason = bar["open"], "gap-stop"
        elif (bar["open"] >= target if long else bar["open"] <= target):
            # Conservative target fill: do not award a favourable gap improvement.
            raw_exit, reason = target, "gap-target"
        elif (bar["low"] <= stop if long else bar["high"] >= stop):
            raw_exit, reason = stop, "stop"  # stop wins ambiguous same-bar stop/target
        elif (bar["high"] >= target if long else bar["low"] <= target):
            raw_exit, reason = target, "target"
        if is_session(trade["session"]):
            _, close = session_bounds(trade["session"])
            if raw_exit is None and stamp(bar["end"]) == close:
                raw_exit, reason = bar["close"], "session-close"
        trade["last_processed_end"] = bar["end"]
        if raw_exit is not None:
            trade_config = ResearchConfig.from_dict(trade["configuration"])
            price = self._friction(raw_exit, trade["direction"], False, trade_config)
            factor = 100 if trade["quote_unit"] == "GBp" else 1
            gross = (1 if long else -1) * (price - trade["entry_price"]) * trade["quantity"] / factor
            commissions = trade["entry_commission"] + trade_config.commission_per_side
            purchase_tax = trade.get("entry_stamp_duty", 0)
            trade.update(status="closed", exit_price=price, exit_at=bar["end"],
                         exit_observed_at=iso(at), exit_time_precision="completed-bar; intra-bar time unknown",
                         reason=reason, gross_pnl=gross, pnl=gross - commissions - purchase_tax,
                         costs=commissions + purchase_tax + trade["entry_friction"] + abs(price - raw_exit) * trade["quantity"] / factor)
            signal = self.store.payloads("signals", "WHERE id=?", (trade["signal_id"],))[0]
            self._signal_state(signal, "closed", reason, at)
            self.store.event(key(trade["id"], "closed"), at, "paper-close",
                             {"ticker": trade["ticker"], "trade_id": trade["id"], "pnl": trade["pnl"], "reason": reason})
        self.store.connection.execute("UPDATE trades SET status=?,exit_at=?,payload=? WHERE id=?",
                                     (trade["status"], trade["exit_at"], serialise(trade), trade["id"]))

    def _advance(self, as_of):
        pending = self.store.payloads("signals", "WHERE status='pending' ORDER BY available_at,id")
        candidates = []
        for signal in pending:
            if signal.get("configuration") != self.config.to_dict():
                self._signal_state(signal, "skipped", "Pending signal superseded by a configuration change; a new observation is required", as_of)
                continue
            if not is_session(signal["session"]):
                self._signal_state(signal, "skipped", "Signal date is not an exchange session", as_of)
                continue
            opening, close = session_bounds(signal["session"])
            expiry = min(stamp(signal["available_at"]) + pd.Timedelta(minutes=self.config.signal_expiry_minutes), close)
            bars = [bar for bar in self._intraday(signal["ticker"], as_of)
                    if stamp(bar["start"]) >= max(stamp(signal["available_at"]), opening) and stamp(bar["start"]) < close]
            if bars:
                first = bars[0]
                eligible_after = max(stamp(signal["available_at"]), opening)
                offset = (eligible_after - opening).total_seconds() / 60
                expected_start = opening + pd.Timedelta(minutes=math.ceil(offset / self.config.interval_minutes) * self.config.interval_minutes)
                lag = (stamp(first["available_at"]) - stamp(first["end"])).total_seconds() / 60
                if stamp(first["start"]) != expected_start:
                    self._signal_state(signal, "missing", "First eligible entry interval is missing; later bars cannot replace it", as_of)
                elif first["quote_unit"] != signal["quote_unit"]:
                    self._signal_state(signal, "skipped", "Signal and candle quote units disagree", as_of)
                elif first["volume"] <= 0:
                    self._signal_state(signal, "skipped", "First eligible entry candle has zero traded volume", as_of)
                elif stamp(first["start"]) > expiry or lag > self.config.max_data_lag_minutes:
                    self._signal_state(signal, "missed", "First eligible future entry bar was unavailable or observed too late", as_of)
                else:
                    candidates.append((first["start"], signal["id"], signal, first))
            elif stamp(as_of) > expiry:
                self._signal_state(signal, "missing", "No eligible completed future entry bar before expiry", as_of)
        # Replay completed bars globally by execution time, so capacity is not freed
        # by a later close before an earlier queued entry is considered.
        timeline = []
        open_positions = self._positions()
        tickers = {trade["ticker"] for trade in open_positions} | {item[2]["ticker"] for item in candidates}
        for ticker in tickers:
            knowledge_clock = None
            earliest_entry = min((trade["entry_at"] for trade in open_positions if trade["ticker"] == ticker), default=None)
            # Scanner history is bounded; an unresolved older position must
            # still be recoverable from the complete preserved archive.
            bars = self.store.candles(ticker, f"{self.config.interval_minutes}m", as_of, since=earliest_entry) if earliest_entry else self._intraday(ticker, as_of)
            for bar in bars:
                # Exit proceeds/capacity become usable only after all preceding
                # observations are available, including late gap recovery.
                knowledge_clock = max(stamp(bar["available_at"]), knowledge_clock or stamp(bar["available_at"]))
                timeline.append((iso(knowledge_clock), 1, (ticker, bar["start"]), bar))
        for start, identifier, signal, bar in candidates:
            timeline.append((start, 0, (-float(signal.get("score", 0)), signal["ticker"], identifier), (signal, bar)))
        for time, kind, _, payload in sorted(timeline, key=lambda item: (item[0], item[1], item[2])):
            if kind == 0:
                signal, bar = payload
                reason = self._open_trade(signal, bar, bar["available_at"])
                if reason:
                    self._signal_state(signal, "skipped", reason, as_of)
            else:
                for trade in self._positions():
                    if trade["ticker"] == payload["ticker"]:
                        self._exit_trade(trade, payload, time)
        # Missing session-close bars stay unresolved. Never invent a close from a
        # partial session or silently time out a position while data is missing.
        for trade in self._positions():
            _, close = session_bounds(trade["session"])
            if stamp(as_of) > close + pd.Timedelta(minutes=self.config.max_data_lag_minutes):
                self.store.event(key(trade["id"], "missing-close"), as_of, "unresolved-close",
                    {"ticker": trade["ticker"], "trade_id": trade["id"],
                     "reason": "Session close candle unavailable; position remains open until evidence arrives"})

    def run(self, as_of=None, signals=None):
        at = stamp(as_of)
        existing_clock = self.store.metadata("last_engine_at")
        if existing_clock and at < stamp(existing_clock):
            raise ValueError("Research clock cannot run backwards within a journal")
        generated = self.scanner_signals(at) if signals is None else signals
        watches = self.watchlist(at)
        if as_of is None:
            at = stamp()  # signal availability follows complete scanner work
        universe = self.store.universe()
        run_id = key("research", iso(at), serialise(universe), serialise(self.config.to_dict()))
        with self.store.transaction():
            current_clock = self.store.metadata("last_engine_at")
            if current_clock and at < stamp(current_clock):
                raise ValueError("Another run has already advanced the research clock")
            if self.store.rows("SELECT id FROM runs WHERE id=?", (run_id,)):
                return self.snapshot(at)
            day = day_of(at)
            if self.store.metadata("equity-start-" + day) is None:
                equity_clock = session_bounds(day)[0] if is_session(day) else at
                self.store.set_metadata("equity-start-" + day, self._equity(equity_clock))
            self._register(generated, at, watches)
            self._advance(at)
            health = self.store.metadata("collection_health", {})
            payload = {"id": run_id, "started_at": iso(at), "completed_at": iso(at),
                       "status": health.get("state", "ok"), "session": day,
                       "instruments": len(universe), "candidate_count": len(generated),
                       "universe_hash": key(serialise(universe)), "config_hash": key(serialise(self.config.to_dict())),
                       "strategy_version": "legacy-zone-rules/opening-context-v1", "mode": self.config.mode}
            self.store.connection.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                (run_id, iso(at), iso(at), payload["status"], payload["universe_hash"], payload["config_hash"], serialise(payload)))
            for watch in watches:
                self.store.connection.execute("INSERT OR IGNORE INTO watches VALUES(?,?,?,?,?,?)",
                    (key(run_id, watch["ticker"]), run_id, watch["ticker"], day, iso(at), serialise(watch)))
                # One first-eligible observation per instrument/day is retained
                # for same-time comparisons, preventing later outcome selection.
                if not watch["stale"] and watch["last_price"] is not None:
                    sample = {**watch, "at": iso(at), "session": day, "config_hash": payload["config_hash"],
                              "configuration": self.config.to_dict(),
                              "sample_kind": "scanner-decision" if generated else "first-session-observation",
                              "selected_candidate_ids": [s.to_dict()["id"] if isinstance(s, Signal) else
                                                         Signal(**s).to_dict()["id"] for s in generated
                                                         if (s.ticker if isinstance(s, Signal) else s["ticker"]) == watch["ticker"]]}
                    self.store.connection.execute("INSERT OR IGNORE INTO evaluation_samples VALUES(?,?,?,?,?)",
                        (key(run_id if generated else day, watch["ticker"]), day, watch["ticker"], iso(at), serialise(sample)))
            self.store.set_metadata("last_engine_at", iso(at))
            if any(not watch["stale"] for watch in watches) and health.get("state", "ok") != "failed":
                self.store.set_metadata("last_success_at", iso(at))
        return self.snapshot(at)

    def collect_and_run(self, tickers=None, as_of=None):
        from .collection import collect_yahoo
        at = stamp(as_of)
        universe = self.store.universe()
        if tickers and isinstance(tickers[0], dict):
            supplied = {record["ticker"]: record for record in tickers}
            merged = {record["ticker"]: record for record in universe}
            merged.update(supplied)
            self.set_universe(list(merged.values()))
            universe = list(supplied.values())
        elif tickers:
            wanted = set(tickers)
            universe = [record for record in universe if record["ticker"] in wanted]
            if wanted - {record["ticker"] for record in universe}:
                raise ValueError("Every collected ticker must have explicit universe metadata and quote units")
        has_history = bool(universe) and all(self.store.rows(
            "SELECT 1 FROM candles WHERE ticker=? AND interval=? LIMIT 1",
            (item["ticker"], f"{self.config.interval_minutes}m")) for item in universe)
        records, failures = collect_yahoo(universe, at, self.config.interval_minutes,
                                         intraday_period="5d" if has_history else "60d")
        counts = self.ingest_candles(records)
        # Actual availability is after the complete collection/ingestion work.
        # Caller as_of is a scheduling hint, never a historical execution clock.
        at = stamp()
        self.store.set_metadata("collection_health", {"state": ("degraded" if records else "failed") if failures else "ok",
            "last_error": "; ".join(failures) or None, "at": iso(at), "failures": failures, **counts})
        return self.run()

    def evaluate(self, as_of=None, max_baseline_samples=None):
        from .evaluation import evaluate
        return evaluate(self, stamp(as_of), max_baseline_samples=max_baseline_samples)

    def seed_demo(self, as_of=None):
        from .fixtures import seed_demo
        return seed_demo(self, as_of)

    def snapshot(self, as_of=None):
        at = stamp(as_of)
        day = day_of(at)
        opening = closing = None
        status = "closed"
        if is_session(day):
            start, end = session_bounds(day)
            opening, closing = iso(start), iso(end)
            status = "pre-open" if at < start else "open" if at < end else "closed"
        watches = self.watchlist(at)
        positions = self._positions()
        closed = self.store.payloads("trades", "WHERE status='closed' ORDER BY exit_at DESC,id LIMIT 100")
        signals = self.store.payloads("signals", "ORDER BY available_at DESC,id LIMIT 150")
        runs = self.store.payloads("runs", "ORDER BY completed_at DESC,id LIMIT 50")
        health = self.store.metadata("collection_health", {"state": "not-collected", "last_error": None})
        fresh = sum(not item["stale"] for item in watches)
        if status == "open" and fresh < len(watches) and health.get("state") != "failed":
            health = {**health, "state": "degraded", "last_error": health.get("last_error") or "Some instruments have missing or stale completed candles"}
        priority = list(dict.fromkeys([p["ticker"] for p in positions] +
                        [w["ticker"] for w in watches if w["is_mover"]] +
                        [s["ticker"] for s in signals] + [w["ticker"] for w in watches]))[:30]
        candles = {ticker: [{"time": row["start"], **{k: row[k] for k in ("open", "high", "low", "close", "volume")},
                           "available_at": row["available_at"]} for row in self._intraday(ticker, at)[-60:]] for ticker in priority}
        evaluation = self.evaluate(at, max_baseline_samples=200)
        for name, limit in (("cashflow_curve", 300), ("entry_cohorts", 90), ("session_daily_returns", 90)):
            evaluation[name] = evaluation[name][-limit:]
        counts = {table: self.store.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
                  for table in ("signals", "trades", "events", "runs", "candles", "evaluation_samples")}
        legacy_rows = self.store.rows("SELECT source,payload FROM legacy_records ORDER BY source,source_hash,row_number,id")
        legacy_counts = {row["source"]: row["n"] for row in self.store.rows(
            "SELECT source,COUNT(*) AS n FROM legacy_records GROUP BY source ORDER BY source")}
        legacy = {"label": "Historical scanner records; old simulation unverified", "counts": legacy_counts,
                  "records": [{**json.loads(row["payload"]), "source": row["source"],
                               "validation": "legacy-unverified"} for row in legacy_rows]}
        result = {"schema_version": 1, "generated_at": iso(at), "last_success_at": self.store.metadata("last_success_at"),
            "mode": self.config.mode, "market": {"session": day, "open": opening, "close": closing,
                "status": status, "timezone": "Europe/London"},
            "health": {**health, "expected_interval_minutes": self.config.interval_minutes,
                       "fresh_instruments": fresh, "total_instruments": len(watches)},
            "config": self.config.to_dict(), "watchlist": watches, "candidates": signals,
            "candles": candles, "paper": {"capital": self.config.capital, "realised_balance": self._balance(),
                "equity": self._equity(at), "positions": positions, "closed_trades": closed,
                "events": self.store.payloads("events", "ORDER BY at DESC,id LIMIT 100")},
            "runs": runs, "evaluation": evaluation, "universe": self.store.universe(), "legacy": legacy,
            "coverage": {"complete_journal_counts": counts, "watchlist": "full configured universe",
                "truncated": counts["signals"] > len(signals) or counts["trades"] > len(positions) + len(closed)
                             or counts["runs"] > len(runs) or len(watches) > len(candles)
                             or counts["evaluation_samples"] > 200,
                "included": {"watchlist": len(watches), "signals": len(signals), "closed_trades": len(closed), "legacy_records": len(legacy_rows),
                    "runs": len(runs), "chart_instruments": len(candles), "baseline_samples": len(evaluation["baselines"])},
                "dashboard_limits": {"chart_instruments": 30, "chart_candles_per_instrument": 60,
                    "recent_candidates": 150, "recent_closed_trades": 100, "recent_events": 100,
                    "recent_runs": 50, "baseline_samples": 200},
                "full_history": "Complete immutable candles, candidates, trades and events remain in the SQLite backup/export"},
            "warnings": ["Paper research only. No real orders are sent.",
                         "1% a day is an aspiration to measure, not a forecast or promise.",
                         "Prices may be delayed. Candle fills are synthetic; fractional quantities and short borrowing are not validated.",
                         "Proposed pilot criteria cannot automatically approve real trading.",
                         "Dashboard history is bounded. Full records remain in the SQLite journal and exports.",
                         "Long paper purchases include configurable illustrative stamp duty. Other tax, market impact and stock borrow costs are not modelled.",
                         "£100 position size is simulated share notional; entry commission and purchase duty require additional available cash."]}
        # Enforce a firm transfer budget even with unusually long company names,
        # notes or strategy explanations. The complete journal is never trimmed.
        budget = 1_500_000
        while len(serialise(result).encode()) > budget:
            if result["legacy"]["records"]:
                result["legacy"]["records"].pop()
            elif result["candles"]:
                result["candles"].pop(next(reversed(result["candles"])))
            elif result["candidates"]:
                result["candidates"].pop()
            elif result["evaluation"]["baselines"]:
                result["evaluation"]["baselines"].pop()
            elif result["paper"]["closed_trades"]:
                result["paper"]["closed_trades"].pop()
            else:
                raise ValueError("Universe metadata alone exceeds the dashboard transfer budget")
            result["coverage"]["size_budget_applied"] = True
            result["coverage"]["truncated"] = True
        result["coverage"]["included"].update(signals=len(result["candidates"]), chart_instruments=len(result["candles"]),
            closed_trades=len(result["paper"]["closed_trades"]), baseline_samples=len(result["evaluation"]["baselines"]),
            legacy_records=len(result["legacy"]["records"]))
        return result
