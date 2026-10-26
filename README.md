# FTSE Trader

Phil's paper-trading research on FTSE 100 and FTSE 250 shares. **Nothing here places real orders. It is a test of screening models, not trading advice.**

Reliability update: [plain-English notes for Phil](docs/RELIABILITY_NOTES_FOR_PHIL.md),
including missing-price handling and how to read recovered results. [Technical details and historical impact](docs/RELIABILITY_REVIEW.md).

**Today's progress:** [FTSE Trader Today](https://philwright210458-blip.github.io/ftse-trader/), a web page rebuilt after every scan and the 17:05 results (`ftse_site.py`, workflow `pages.yml`).

## What runs automatically (Mon-Fri, on GitHub)

The timers are on cron-job.org; each one starts a GitHub workflow.

| Time (UK) | Workflow | What it does | Results |
| --- | --- | --- | --- |
| 08:15 | FTSE Full Scan (`full_scan.yml`) | Scans every share for range and reclaim setups | `ftse_scan_latest.xlsx` (one sheet: Today's Mock Trades), `scan_data/` (every setup found) |
| 07:06 | Morning picker (Claude scheduled task, not a GitHub workflow) | Reads overnight news and market mood; writes up to 10 FTSE 100 shares marked Buy or Sell, plus any to Avoid, to `scan_data/picker/<date>.csv`. From 26 Oct 2026 the scanner only labels ideas with the picker's view (agrees / opposes / avoid / no view); trades are unchanged until the research has been measured (`--picker filter` would let it skip and prioritise ideas, `--picker off` ignores it) | `scan_data/picker/` |
| 08:31, then every 15 min to 15:31 | FTSE Watch (`watch.yml`) | Scans every share again; live mock trades (FTSE 100 shares only) at trigger points: every idea that passes the rules is taken, up to 5 open at once (a closed trade frees its slot), 10 a day, one per share | `ftse_scan_latest.xlsx`, `scan_data/live_mock_trades.csv` |
| 17:05 | FTSE Outcomes (`outcomes.yml`) | Scores the live mock trades (sheet **Live Mock Trades**: totals, a comparison with every idea, and each trade; sheet **Daily**: running balance chart); also runs the momentum and structure paper traders | `ftse_outcomes.xlsx`, `ftse_momentum_live.xlsx`, `ftse_structure_live.xlsx` |
| after each scan | Publish scans (`publish_scans.yml`) | Sends scan results to the shared dashboard | (dashboard) |

Workflows run by hand from the Actions tab: **FTSE Backtest**, **FTSE Range v2 Backtest** and **FTSE Momentum Backtest**. They save their workbooks in `results/`.

## The models

| Model | Code | Status |
| --- | --- | --- |
| 15-minute range and breakout scanner (the live one) | `ftse_trade_scanner.py`, `ftse_live_mock.py`, `ftse_outcomes.py` | Running; backtest negative after costs. From 21 Oct 2026 it also skips shares seen both ways, needs a rejection candle for range trades and a fresh close 0.5 ATR beyond the edge for reclaims; from 23 Oct it also skips ideas whose target would not pay at least 1.5x the possible loss after costs (`--no-entry-rules` turns these off); from 26 Oct each target is capped at the smaller of 2% and the share's typical daily move over the previous 10 days, before the costs check (`--target-cap-pct 0` turns it off) |
| Trend pullback (idea 17) | `ftse_pullback.py` (tested by `ftse_backtest.py`) | Rejected |
| Range model version 2 (Patrick Nill's method, rebuilt) | `ftse_range_v2.py`, `ftse_range_backtest.py` | Rejected on shares and the index |
| Claude's model: monthly momentum | `ftse_momentum.py` (backtest), `ftse_momentum_live.py` (paper trading from 30 Oct 2026) | Paper trading |
| Daily structure pullback (idea 24, Market Structure report) | `ftse_structure.py`, `ftse_structure_backtest.py`, `ftse_structure_live.py` (paper trading from 22 Oct 2026, £250 a trade, results in `ftse_structure_live.xlsx`) | Paper trading; backtests in progress |

## Folders and files

- `ftse_universe.csv`: the 350 shares (the `type` column marks investment trusts).
- `scan_data/`: the live history (setups, runs, outcomes, live mock trades, momentum orders). Don't edit by hand.
  `outcomes.csv` is the old version 1 results, kept for reference and the dashboard; `outcomes.v2.csv` retains the previous results; `outcomes.v3.csv` uses the shared execution rules.
- `results/`: backtest workbooks.
- `tests/`: automatic checks (`python -m unittest discover -s tests`). They run on every pull request.
- `docs/`: `IDEAS.md` (copy of the ideas register; the Claude Doc is the master), `LEGACY_SCANNER.md` (the scanner guide and its rules), and the research app guides.
- Research app (built by Ashley; not switched on): `ftse_research/`, `ftse_research_cli.py`, `ftse_scheduler.py`, `config/`, `deploy/`, `Dockerfile`, `.env.example` and `dashboard/`. See [docs/RESEARCH_APP.md](docs/RESEARCH_APP.md). `dashboard/` and `publish_scans.py` also run the published scans site.
- `ftse_sessions.py`: London trading days and hours, used by everything.
- `requirements.txt` / `requirements.lock`: the Python packages (exact versions in the lock file).

## Running on a computer

```
pip install -r requirements.txt
python ftse_trade_scanner.py --exclude-trusts        # full scan
python ftse_trade_scanner.py --watch                 # re-check the shortlist
python ftse_outcomes.py                              # score ideas, update ftse_outcomes.xlsx
```

Yahoo's prices can be up to 15 minutes behind; check a share against your broker before relying on any price.
