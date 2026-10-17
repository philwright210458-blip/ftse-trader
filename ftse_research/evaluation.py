"""Descriptive pilot evidence, with independently reconciled candidate outcomes."""
import math
import pandas as pd

from .models import ResearchConfig, stamp
from ftse_sessions import session_open_close, is_session


def evaluate(engine, as_of, max_baseline_samples=None):
    store, cfg = engine.store, engine.config
    trades = store.payloads("trades", "ORDER BY entry_at,id")
    signals = store.payloads("signals", "ORDER BY available_at,id")
    closed = [t for t in trades if t["status"] == "closed"]
    counts = {status: sum(s["status"] == status for s in signals)
              for status in ("pending", "open", "closed", "skipped", "missing", "missed")}
    counts["total"] = len(signals)
    counts["reconciled"] = sum(counts[status] for status in ("pending", "open", "closed", "skipped", "missing", "missed")) == len(signals)
    # Cash-flow drawdown is ordered by realised exit observation, never entry.
    cash = cfg.capital
    peak = cash
    maximum = 0.0
    cash_curve = []
    cashflow_sessions = {}
    for trade in sorted(closed, key=lambda item: (item["exit_at"], item["id"])):
        cash += trade["pnl"]
        peak = max(peak, cash)
        maximum = max(maximum, (peak - cash) / peak * 100 if peak else 0)
        day = str(stamp(trade["exit_at"]).tz_convert("Europe/London").date())
        cashflow_sessions[day] = cashflow_sessions.get(day, 0) + trade["pnl"]
        cash_curve.append({"time": trade["exit_at"], "balance": cash, "trade_id": trade["id"]})
    cohorts = {}
    for trade in trades:
        bucket = cohorts.setdefault(trade["session"], {"session": trade["session"], "closed": 0, "open": 0, "net_pnl": 0})
        bucket[trade["status"]] += 1
        if trade["status"] == "closed":
            bucket["net_pnl"] += trade["pnl"]
    sessions = sorted({r["session"] for r in store.payloads("runs") if is_session(r["session"])})
    usable_sessions = [row["session"] for row in store.rows("SELECT DISTINCT session FROM evaluation_samples ORDER BY session")]
    daily = []
    prior = cfg.capital
    for day in sessions:
        pnl = cashflow_sessions.get(day, 0)
        daily.append({"session": day, "net_pnl": pnl, "return_pct": pnl / prior * 100 if prior > 0 else None})
        prior += pnl
    pnl = sum(t["pnl"] for t in closed)
    wins = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [-t["pnl"] for t in closed if t["pnl"] < 0]
    avgwin = sum(wins) / len(wins) if wins else None
    avgloss = sum(losses) / len(losses) if losses else None
    breakeven = avgloss / (avgloss + avgwin) * 100 if avgwin and avgloss else None
    baselines = []
    sample_count = store.rows("SELECT COUNT(*) AS n FROM evaluation_samples")[0]["n"]
    samples = store.payloads("evaluation_samples", "ORDER BY at DESC,ticker" +
                            (" LIMIT ?" if max_baseline_samples is not None else ""),
                            (max_baseline_samples,) if max_baseline_samples is not None else ())
    for sample in samples:
        sample_config = ResearchConfig.from_dict(sample.get("configuration", cfg.to_dict()))
        _, closing = session_open_close(sample["session"])
        bars = [b for b in store.candles(sample["ticker"], f"{sample_config.interval_minutes}m", as_of)
                if stamp(b["start"]) >= stamp(sample["at"]) and stamp(b["end"]) <= stamp(closing)]
        # Reference and comparator both begin at the first future eligible bar,
        # use the same unit/cost assumptions and require the true close candle.
        eligible = [b for b in bars if b["volume"] > 0]
        complete = eligible and stamp(eligible[-1]["end"]) == stamp(closing)
        row = {"ticker": sample["ticker"], "session": sample["session"], "observed_at": sample["at"],
               "is_mover": sample["is_mover"], "previous_change_pct": sample["previous_change_pct"],
               "status": "complete" if complete else "pending-or-missing", "long_return_pct": None,
               "hypothesis_return_pct": None, "basis": "same-time eligible universe; first future bar open to session close",
               "opening_status": sample["status"], "sample_kind": sample.get("sample_kind", "first-session-observation"),
               "selected_candidate_ids": sample.get("selected_candidate_ids", [])}
        if complete:
            first, last = eligible[0], eligible[-1]
            opening, _ = session_open_close(sample["session"])
            offset = max(0, (stamp(sample["at"]) - stamp(opening)).total_seconds() / 60)
            expected_start = stamp(opening) + pd.Timedelta(minutes=math.ceil(offset / sample_config.interval_minutes) * sample_config.interval_minutes)
            first_fresh = (stamp(first["available_at"]) - stamp(first["end"])).total_seconds() / 60 <= sample_config.max_data_lag_minutes
            contiguous = all(stamp(b["start"]) == stamp(a["end"]) for a, b in zip(eligible, eligible[1:]))
            if stamp(first["start"]) != expected_start or not first_fresh or not contiguous:
                row["status"] = "pending-or-missing"
            else:
                entry = engine._friction(first["open"], "Long", config=sample_config)
                exit = engine._friction(last["close"], "Long", False, sample_config)
                quantity = sample_config.position_size / entry
                duty = entry * quantity * sample_config.stamp_duty_pct / 100
                row["long_return_pct"] = ((exit - entry) * quantity - 2 * sample_config.commission_per_side - duty) / sample_config.position_size * 100
                change = sample["previous_change_pct"]
                if sample["is_mover"] and change:
                    direction = "Short" if change > 0 else "Long"
                    entry = engine._friction(first["open"], direction, config=sample_config)
                    exit = engine._friction(last["close"], direction, False, sample_config)
                    side = -1 if direction == "Short" else 1
                    duty_rate = sample_config.stamp_duty_pct / 100 if direction == "Long" else 0
                    quantity = sample_config.position_size / entry
                    duty = entry * quantity * duty_rate
                    row["hypothesis_return_pct"] = (side * (exit - entry) * quantity - 2 * sample_config.commission_per_side - duty) / sample_config.position_size * 100
                    row["hypothesis_model"] = "descriptive synthetic reversal comparator; no borrow cost model"
        baselines.append(row)
    configurations = store.rows("SELECT config_hash,COUNT(*) AS runs FROM runs GROUP BY config_hash ORDER BY config_hash")
    sufficient = len(usable_sessions) >= cfg.minimum_pilot_sessions and len(closed) >= cfg.minimum_pilot_closed_trades and len(configurations) == 1
    unresolved = counts["missing"] + counts["missed"] + counts["open"] + counts["pending"]
    scanner = [trade for trade in closed if trade["strategy"].startswith("scanner-")]
    combined = [trade for trade in scanner if (trade.get("mover_context") or {}).get("is_mover")]
    def trade_group(items):
        return {"closed_trades": len(items), "net_pnl": sum(item["pnl"] for item in items),
                "expectancy": sum(item["pnl"] for item in items) / len(items) if items else None}
    groups = {"existing_scanner": trade_group(scanner), "scanner_with_previous_mover_context": trade_group(combined)}
    # Session clustering exposes that several same-day shares/observations are
    # correlated, rather than counting each as independent evidence.
    baseline_groups = {}
    for label, predicate, measure in (
        ("eligible_universe_long", lambda item: True, "long_return_pct"),
        ("movers_reversal_hypothesis", lambda item: item["is_mover"], "hypothesis_return_pct"),
        ("opening_reversal", lambda item: item["opening_status"] == "reversal", "hypothesis_return_pct"),
        ("opening_continuation", lambda item: item["opening_status"] == "continuation", "hypothesis_return_pct"),
        ("selected_scanner_alternatives", lambda item: bool(item["selected_candidate_ids"]), "long_return_pct"),
        ("unselected_same_time_alternatives", lambda item: item["sample_kind"] == "scanner-decision" and not item["selected_candidate_ids"], "long_return_pct")):
        rows = [item for item in baselines if predicate(item) and item.get(measure) is not None]
        clusters = {}
        for item in rows:
            clusters.setdefault(item["session"], []).append(item[measure])
        means = [sum(values) / len(values) for values in clusters.values()]
        baseline_groups[label] = {"usable_observations": len(rows), "session_clusters": len(means),
            "session_mean_return_pct": sum(means) / len(means) if means else None,
            "status": "descriptive; equal-weight observations, not an executable portfolio"}
    sensitivity = []
    for extra_bps in (5, 10, 25):
        # Deliberately simple deterioration, clearly separated from a refit of
        # stops/targets. An independent adverse bps charge is added to each side.
        extra_cost = sum(t["position_size"] * 2 * extra_bps / 10000 for t in closed)
        sensitivity.append({"additional_bps_per_side": extra_bps, "net_pnl": pnl - extra_cost,
                            "basis": "additional friction on recorded trades; not a re-simulation"})
    return {"label": "Synthetic demonstration evidence" if cfg.mode == "synthetic-demo" else "Paper pilot evidence; not proof of future returns",
        "sample_counts": counts, "sessions": len(sessions), "usable_sessions": len(usable_sessions), "closed_trades": len(closed),
        "net_pnl": pnl, "expectancy": pnl / len(closed) if closed else None,
        "win_rate_pct": len(wins) / len(closed) * 100 if closed else None,
        "average_win": avgwin, "average_loss": avgloss, "break_even_win_rate_pct": breakeven,
        "max_drawdown_pct": maximum, "drawdown_basis": "realised exit cash flows; open mark-to-market risk is additional",
        "cashflow_curve": cash_curve, "entry_cohorts": list(cohorts.values()), "session_daily_returns": daily,
        "baselines": baselines, "baseline_sample_count": sample_count, "baseline_displayed_samples": len(baselines),
        "baseline_warning": "Non-tradable descriptive comparator. No market impact or short borrowing model. Missing data excluded explicitly.",
        "aspiration": {"daily_return_pct": 1, "status": "user aspiration, not a promised or required daily outcome"},
        "criteria_status": "proposed", "criteria": {"minimum_sessions": cfg.minimum_pilot_sessions,
            "minimum_closed_trades": cfg.minimum_pilot_closed_trades, "maximum_realised_drawdown_pct": cfg.maximum_pilot_drawdown_pct},
        "sufficient_sample": sufficient, "unresolved_candidates": unresolved,
        "configuration_cohorts": configurations, "configuration_changed": len(configurations) > 1,
        "strategy_comparisons": groups, "baseline_comparisons": baseline_groups,
        "sensitivity": sensitivity, "losing_days": sum(item["net_pnl"] < 0 for item in daily),
        "no_trade_days": sum(not any(t["session"] == day for t in trades) for day in sessions),
        "operational_failures": sum(r["status"] in {"failed", "degraded"} for r in store.payloads("runs")),
        "assessment": "human-review-needed" if sufficient else "insufficient-evidence", "live_approved": False,
        "limitations": ["Paper fills do not validate executable quotes, liquidity or broker support.",
                        "Synthetic short comparators omit borrowing availability and borrow fees.",
                        "Zero-trade sessions remain in session counts; source gaps remain missing.",
                        "Historical universe membership is not reconstructed; survivorship bias remains."]}
