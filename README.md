# FTSE Research

A browser workspace for yesterday's FTSE movers, today's completed candles and automatic **paper** trades. The Python collector runs independently of the browser, preserves a durable research journal and publishes authenticated snapshots. The original scanners and spreadsheet exports remain available.

This branch is for review. **No live schedules have been changed, no Site has been deployed and no broker orders are supported.** The collector configuration starts disabled.

## Try it without market access

Python 3.12 and Node 22 are the tested baseline. Run these commands from the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python ftse_research_cli.py demo --database runtime/demo.sqlite --out runtime/demo-snapshot.json
node dashboard/server.mjs --demo
```

Open `http://127.0.0.1:8787/?demo=1` for the labelled interface example. The generated Python demo is a separate deterministic, fictional research ledger; it can be sent through the actual authenticated update API as described in [Operations](docs/OPERATIONS.md). Opening the ordinary `/` URL shows no data until an update is received. Demo data is not included in the hosted Worker.

The demo is for reviewing behaviour and presentation. Its results are not observed share performance. Synthetic and market-paper journals cannot be mixed.

## What is included

- Morning watch with previous-session risers/fallers, opening gaps, completed-candle direction and visible data freshness.
- Share detail with a candlestick chart, exact records and setup explanations.
- Virtual portfolio, explicit costs and capital limits, rejected entries and realised results.
- Durable SQLite evidence, authenticated hosted snapshot storage, archived snapshots and CSV exports.
- A simpler outcome workbook: a pound profit/loss summary, separate strategy comparison and grouped breakdowns.
- Session-aware scheduling for London holidays, daylight saving and early closes, with duplicate and missed-run protection.

The user-selected defaults are **£100 notional per simulated position**, a **5% initial configurable movers filter**, and monitoring for the **whole trading session**. The sample £1,000 virtual account, three-position limit and evaluation thresholds are proposed paper-research assumptions. They are editable in `config/research.json`; they are not live-money recommendations. Synthetic shorts are disabled by default because borrow availability and charges have not been established.

## Scheduling without writing cron jobs

`ftse_research_cli.py service` is the supplied scheduler. On a persistent Linux host it prepares the universe fifteen minutes before the exchange opens, checks at the open, then processes completed fifteen-minute bars after a ninety-second publication allowance. A final resolution runs fifteen minutes after the actual session close. These are configurable timings, not a guarantee of provider freshness.

The service calculates UK market times using the exchange calendar. It locks the journal against overlapping collectors and records missed windows instead of inventing earlier trades. Failed jobs remain recorded and do not retry indefinitely. Neither a browser refresh nor your computer staying awake is required once the separate host is configured.

```sh
python ftse_research_cli.py plan --at 2026-10-16T16:46:00+01:00
python ftse_research_cli.py tick
```

The second command reports `disabled` with the shipped configuration and makes no market requests. The existing three GitHub workflows are unchanged. The only new workflow runs offline pull-request checks; it has **no cron trigger**. Hosting activation is a separate step after review. [Full setup and recovery instructions](docs/OPERATIONS.md).

## Checks

```sh
python -m unittest discover -s tests -v
node --test dashboard/tests/*.test.mjs
node dashboard/scripts/build.mjs
python tests/report_fixture.py
```

The report fixture is deliberately synthetic and is written to `runtime/ftse_outcomes_review.xlsx`. The original tracked workbooks and CSVs are retained unchanged. The corrected legacy outcome tracker uses a separate versioned cache, `outcomes.v2.csv`, and will not reuse old flawed results as corrected observations.

## Boundaries and next steps

The implementation provides the collection, scheduling, dashboard and paper-evaluation machinery. Hosted two-user access, provider latency and a forward market pilot still need direct operational verification. A short successful demo or test suite does not prove profitability. The aspiration of 1% each day is not a forecast and never forces an entry.

Read [Implementation status](docs/IMPLEMENTATION_STATUS.md), [Operations](docs/OPERATIONS.md), [Data and simulation rules](docs/RESEARCH_MODEL.md) and the [original scanner guide](docs/LEGACY_SCANNER.md). The approved future direction is paper research first, with any decision about a separate live execution service deferred. ChatGPT Sites is considered for the research interface; no financial transaction capability is added to it.

A ready-to-inspect [synthetic workbook example](docs/examples/ftse_outcomes_synthetic.xlsx) is included in the pull request. It illustrates the layout and mixed result states without presenting old cached numbers as corrected market performance.
