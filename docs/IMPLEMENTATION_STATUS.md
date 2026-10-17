# Implementation and activation status

This pull request implements the research application and its verification tooling. It does not claim that a profitable forward pilot has already happened, or that the hosted service has been activated.

## Phase coverage

| Phase | Delivered in the branch | Acceptance still requiring deployment or elapsed market observations |
| --- | --- | --- |
| 0 Feasibility | Authenticated Python-to-HTTP snapshot write and independent persisted-metadata readback; bounded snapshot contract; real single-symbol Yahoo access check; private hosting source | Native Sites provisioning, unattended private service credentials and separate intended reader accounts must be tested after review. Hosting costs remain unapproved. |
| 1 Correctness | Seven reviewed defects fixed; session calendar and corrected separate cache; compact pound-led workbook, strategy sections and reconciled breakdowns | Original historical results are preserved as unverified. Corrected historical recomputation requires sufficient provider coverage. Native Excel app interaction has not been tested. |
| 2 Data and scheduling | Durable SQLite journal, revisions, repeatable legacy import, verified backup, complete CSV export, session scheduler, container/service configuration, lock and missed-window protection | Choose and configure the persistent host, backup destination, secret store and failure notification route. The service starts disabled. |
| 3 Opening watch | Adjusted previous-session movers, configurable 5% filter, opening gap and completed-candle direction; original scanner rules retained | Measure open-session provider delay, validate the current universe and quote units, and agree whether the available granularity meets the purpose. |
| 4 Dashboard | Four responsive browser views, candlestick chart, private Worker/D1 API, archives, history and safe exports; normal and mobile-width browser checks | Confirm hosted Sites compatibility and access for both users. The supplied source build has not been deployed. |
| 5 Automatic paper trades | Future-bar simulation, £100 notional positions, configurable duty/fees, persistent limits and positions, explicit rejection and missing-data handling | Review proposed virtual capital, risk/cost settings and simulated instrument assumptions before starting the forward pilot. |
| 6 Evaluation | Reconciled results, exit-order drawdown, cohort views, comparable descriptive benchmarks, clustered session context and adverse-cost sensitivity | Run the forward market pilot, agree evaluation criteria and assess sufficient observations. Tests and fictional data do not establish a trading advantage. |

Live execution remains a separate future decision. There is no broker integration or real-order route.

## Confirmed user decisions

- £100 means the **notional value per simulated position**, not the whole account and not a maximum permitted loss. Modelled costs require additional virtual cash.
- Prepare before the market opens and watch the **whole trading session**.
- Deliver the work in a separate branch and an **unmerged pull request** for the owner's review.
- Leave the existing live schedules unchanged. Do not deploy or cut over the new collector as part of opening the pull request.

The £1,000 sample account, three-position limit, 2% virtual daily loss guard and pilot thresholds are explicit proposed research defaults. They are not established live-money limits. An initial 5% movers threshold is configurable. The 1% daily ambition is not a promised result or an instruction to force trades.

## Verification evidence

- All 87 Python regression tests pass. The suite covers original defects, exchange sessions, data ingestion, unit conversion, immutable revisions, future entry timing, conservative exit chronology, costs, allocation and persistence, legacy isolation, backup, scheduler locking and missed windows.
- All 11 Node regression tests pass. The suite exercises the actual HTTP API, authentication, duplicate/stale/conflicting updates, persisted readback, archived exports, legacy isolation, synthetic provenance and production D1 SQL against SQLite.
- A fictional Python ledger was published through the real local API and its stored revision was independently read back. The dashboard was inspected using that API response.
- A single read-only Vodafone collection returned 193 validated daily/intraday candles and no collection errors. This is source-access evidence, not proof of opening-session latency, future availability or all-symbol coverage.
- Importing the original CSV archive inserted 645 raw rows; repeating the same import inserted zero rows and identified 645 duplicates. Those records are excluded from corrected paper balances and evaluation.
- A separate SQLite backup passed integrity and table reconciliation checks.
- The Docker image built successfully; its offline disabled tick exited without collecting or activating a schedule. The dashboard source build and generated Worker syntax were checked. Browser checks covered morning watch, share chart, paper portfolio, archive controls, synthetic labels and a 390-pixel layout without document overflow.
- The tracked workbook example in `docs/examples/ftse_outcomes_synthetic.xlsx` is explicitly fictional. Summary, Strategy Comparison and Breakdowns were rendered and inspected; arithmetic and detailed tabs have regression checks. Native Excel rendering remains unverified.

## Files deliberately preserved

The original `scan_data` CSVs, `ftse_outcomes.xlsx`, `ftse_scan_latest.xlsx` and all three existing full scan/watch/outcome workflow files are unchanged. No external cron timer, Site audience, hosted secret, paid service or broker account has been changed.

## Operating limits

The source universe's quote units must be checked when instruments are added or refreshed. The paper model uses fractional synthetic fills and does not validate broker liquidity, borrow availability or all tax treatments. Synthetic short trading is disabled by default. The scheduler is intended for a single persistent Linux runner; the local Node server is a loopback development preview. Source for Docker and systemd is provided, but no production container or service has been launched.
