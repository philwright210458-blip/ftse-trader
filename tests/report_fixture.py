"""Synthetic cash-flow fixture for report QA, never an observed market result.

Run from the repository root to produce runtime/ftse_outcomes_review.xlsx.
"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import ftse_outcomes as out


def fixture():
    rows, results = [], []
    cases = [
        # key, day, direction, score, risk %, outcome, R, status
        ("profit", "2026-09-14", "Long", 90, 2.0, "target", 3.0, "done"),
        ("target-loss", "2026-09-14", "Long", 65, 5.0, "target", 0.02, "done"),
        ("loss", "2026-09-15", "Short", 65, 5.0, "stop", -0.8, "done"),
        ("flat", "2026-09-15", "Long", 65, 3.0, "timeout", 0.2, "done"),
        ("skip", "2026-09-16", "Long", 75, 2.0, "gap", 0.0, "skipped"),
        ("missing", "2026-09-16", "Long", 75, 2.0, "missing_coverage", 0.0, "nodata"),
        ("pending", "2026-09-17", "Long", 75, 2.0, "", 0.0, "pending"),
        ("pool-extra", "2026-09-18", "Long", 65, 1.0, "target", 1.0, "done"),
    ]
    for i, (key, day, side, score, risk, event, r, status) in enumerate(cases):
        rows.append(
            dict(
                key=key,
                day=day,
                as_of="10:30",
                company=f"Synthetic example {i+1}",
                ticker=f"DEMO{i}.L",
                index="FTSE 100",
                setup="Range" if i % 2 else "Breakout",
                direction=side,
                score=score,
                touches=4 + i,
                entry=100,
                stop=100 - risk if side == "Long" else 100 + risk,
                target=100 + 3 * risk if side == "Long" else 100 - 3 * risk,
                data_time=f"{day}T10:15:00+01:00",
                run_time=f"{day} 10:31:00",
                mode="full",
                conflict=False,
                rank=1 if i % 2 == 0 else 2,
            )
        )
        if status != "pending":
            results.append(
                dict(
                    key=key,
                    day=day,
                    ticker=f"DEMO{i}.L",
                    direction=side,
                    horizon=1,
                    status=status,
                    outcome=event,
                    fill=100.0,
                    exit_price=100 + r * risk * (1 if side == "Long" else -1),
                    exit_time=f"{day}T15:00:00+01:00" if status == "done" else "",
                    r_gross=r,
                    fill_risk_pct=risk,
                    ambiguous=False,
                )
            )
    pool = pd.DataFrame(rows)
    main = pool.iloc[:7].copy()
    picks = {out.LIVE: main}
    args = argparse.Namespace(
        horizons="1,2,5", horizon=1, cost_pct=0.1, stamp_duty_pct=0.5, pick_time="10:30", position_gbp=100.0
    )
    return args, pool, picks, pd.DataFrame(results)


def build(destination="runtime/ftse_outcomes_review.xlsx"):
    args, pool, picks, results = fixture()
    wb = out.build_report(args, pool, pool, picks, results, {}, "SYNTHETIC FIXTURE: no observed market performance")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destination)
    return destination


if __name__ == "__main__":
    print(build())
