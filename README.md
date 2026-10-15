# FTSE 100/250 Share Scanner

Scans every individual company in the FTSE 100 and FTSE 250 (350, listed in
`ftse_universe.csv`), finds price zones that have been independently tested at
least 3 times, and flags range and breakout setups. Everything lands in **one
workbook**, `ftse_scan_latest.xlsx`, which is updated on every run.

## Setup (once)

```
python -m pip install -r requirements.txt
```

## Two ways to run it

**Full scan** scans all 350 companies (a few minutes). Do this once, e.g. at 10:00.
```
python ftse_trade_scanner.py
```

**Watch** re-checks only the shortlist from today's latest full scan (the top 15 by
default), which takes seconds. Run it every 15 minutes while you form a view.
```
python ftse_trade_scanner.py --watch
```
Watch does nothing, and says so, if no new candle has formed since the last scan, so
running it at awkward times (or after the close) never creates duplicate entries.
It needs a full scan earlier the same day.

## The workbook

| Sheet | What it shows |
|---|---|
| **Top Trades** | This scan's ranked shortlist, with **Seen today** (e.g. `4/5` = flagged in 4 of the 5 scans since it first appeared) and **First seen**. **Type** says Trust for investment trusts and funds. **Last trade** is when the share's newest traded candle closed |
| **Today** | One row per setup, one column per scan time, cells show the quality score. A row that stays filled across the columns has persisted; one that appears and disappears is flickering |
| **All Setups** | Every raw setup from this scan, before de-duplication. Grey rows are stale (see below) |
| **History** | Everything flagged over the last 3 scanning days, newest first |

The column headings on `Today` (10:15, 10:30...) are the time each scan's latest
completed candle closed, in UK time.

Behind the scenes, each run appends to `scan_data/runs.csv` and
`scan_data/setups.csv`. The workbook is rebuilt from those. If the workbook is open in Excel when
a scan finishes, Windows blocks the update, so the scan is saved as
`ftse_scan_latest_HHMMSS.xlsx` instead. Close the workbook so the next run can update it.

## How setups are chosen

- A zone counts only with at least 3 *independent* touches: repeats must be 4+
  candles apart with a real move away in between.
- One setup per company per direction (highest quality score wins).
- **Quality Score (0-100)** blends independent touches, closeness to the zone edge,
  zone tightness, setup type and recency. The weights are judgement calls and have not
  been backtested.
- If a company has both a Long and a Short idea scoring within 15 points of each other,
  both are flagged as a conflict and left out of Top Trades. If one is clearly stronger,
  only the weaker one is excluded.
- Stops sit beyond the zone edge by at least 1.25x the share's typical candle range (the
  *median* of the last 14 candles, so one opening-auction candle can't inflate it), and never less
  than 0.1% of price. Setups risking too little to cover a typical spread are skipped. The `Risk %`
  column shows the stop distance.
- **Stale guard.** A thin share that hasn't traded for hours still shows its old last price as
  "current", so it can look like it is sitting exactly on a zone edge. Any setup whose newest traded
  candle is more than 30 minutes behind the freshest candle in the scan is left out of Top Trades and
  the history. It still appears (grey) on All Setups. Change the limit with `--max-stale`; `0` turns it off.
- **Zone width floor.** A zone counts as at least a quarter of a typical candle wide when scoring
  precision, so a zone made of one repeated price (one tick) can't earn full precision credit.
- Reward:risk is a fixed 3:1 by design, so it is not a ranking signal.
- The still-forming candle is ignored and zero-volume candles are removed.

## Scheduling on Windows (Task Scheduler)

`run_full_scan.bat` and `run_watch.bat` do the work and write any errors to `scan_log.txt`.
They must sit in the same folder as `ftse_trade_scanner.py`.

1. **Full scan**: Task Scheduler > Create Task. Trigger: Daily at 10:00. Action: Start a
   program, pointing at `run_full_scan.bat`, with "Start in" set to this folder.
2. **Watch**: a second task. Trigger: Daily at 10:15, tick "Repeat task every" 15 minutes
   "for a duration of" 2 hours. Action: `run_watch.bat`, same "Start in" folder.
3. In both, under Settings, tick "Run task as soon as possible after a scheduled start is missed".
   The PC must be on and awake at those times.

Watch every 15 minutes (or 30), not 20: the candles are 15 minutes, so 20-minute gaps
often see nothing new.

## Outcome tracker and paper trading

`ftse_outcomes.py` answers the question "would following this model have made money?". It reads
every setup the scanner has logged, works out what the market did next, and builds
`ftse_outcomes.xlsx`.

```
python ftse_outcomes.py
```

Run it once a day after the close. `run_outcomes.bat` does that and logs to `outcomes_log.txt`;
schedule it for weekdays at 17:00. Yahoo only keeps about 60 days of 15-minute candles, so results
are cached in `scan_data/outcomes.csv` as they resolve. The first run downloads history for every
share with a setup and takes a few minutes; later runs only fetch shares whose hold period is still open.

**How a trade is simulated.** You act on a scan and fill at the open of the next 15-minute candle.
The stop and target are the scan's levels. If both fall inside one candle, the stop is assumed first.
A gap through the stop fills at the open, so a loss can exceed 1R. A setup you couldn't have filled
within 30 minutes (a thin share that didn't trade) or whose price had already passed the stop or
target is skipped. Setups from scans after 15:30 are ignored.

**The mock trader.** Each day it looks at the first scan at or after 10:30, and takes the top 1 or
top 2 ideas by score (one per share). A third version also needs the idea to have been flagged in at
least two scans by then, with no peeking at later scans. Hold periods of 1, 2 and 5 trading days are
tested side by side.

**The comparison that matters.** "Pool average" is every idea at its first sighting of the day, which
is what picking at random would earn. If the top picks don't beat it, the ranking adds nothing.

| Sheet | What it shows |
|---|---|
| **Summary** | Each scenario and hold period: trades, how often the target and the stop were hit (with a 95% range), average R before and after costs, drawdown, and the hit rate needed to break even |
| **Breakdowns** | The pool sliced by score band, trust vs company, range vs breakout, direction, index and touches, each labelled by how many trades back it |
| **Mock Trades** | Every paper trade: fill, stop, target, result, exit, cost and running total |
| **Daily** | Cumulative R by day for each scenario, a £ column, and the equity chart |
| **All Outcomes** | Every first-sighting idea and what happened at each hold period |

**Costs** are applied when the report is built, so changing them never re-downloads anything:
`--cost-pct 0.10` is the assumed round-trip spread and slippage as a percentage of price (an assumption,
not a measurement; set it to what your broker really charges). `--stamp-duty-pct 0.5` fills one extra
column showing results if you buy actual UK shares, since stamp duty of 0.5% applies to purchases (long
entries) and not to spread bets or CFDs. With stops of 0.3-0.5% of price, that duty alone costs 1R or
more per long trade. Set `--stamp-duty-pct 0` to drop it.

**Reading it honestly.** A few hundred independent trades are needed before a hit rate means much; the
sheets say how many you have. The pool collects ideas far faster than 1-2 trades a day, so the
Breakdowns become informative first, but ideas on the same day share the same market conditions, so
they are less independent than the count suggests. With many slices, some will look good by chance. If
you change the simulation rules, delete `scan_data/outcomes.csv` so it is recomputed.

## Options

```
--watch               Re-check today's shortlist only
--watch-top 15        How many shortlisted tickers to watch
--top 10              Rows shown in Top Trades (0 = no cap)
--max-stale 30        Minutes behind the freshest candle before a setup counts as stale (0 = off)
--exclude-trusts      Leave out investment trusts and funds entirely

Outcome tracker (ftse_outcomes.py): --horizons 1,2,5  --horizon 1  --pick-time 10:30  --last-entry 15:30
                                    --cost-pct 0.10  --stamp-duty-pct 0.5  --risk-gbp 100
--limit 20            Full scan: only the first N companies (testing)
--index "FTSE 100"    Full scan: one index only
--interval 15m        Candle size (the history keeps one column per candle)
--out file.xlsx       Different workbook
--data-dir folder     Different history folder
--delay 0.3           Seconds between tickers (raise it if Yahoo rate-limits you)
```

## Keeping the company list current

`ftse_universe.csv` is a snapshot from the June 2026 FTSE review. Membership changes
quarterly. Check it against the Wikipedia FTSE 100 and FTSE 250 pages and edit it, or ask
for a regenerated copy. Columns: `company`, `epic`, `yahoo_ticker`, `index`, `type`. `type` is `trust` for investment
trusts and funds (93 of the 350, 88 of them in the FTSE 250) and `company` for everything else. I
classified these by hand; a few are borderline (Law Debenture, Molten Ventures), so edit the column
if you disagree. Without a `type` column everything is treated as a company.

## Limitations

- **Yahoo's free data is probably delayed**, so use the scan to find candidates, and check the live price on your broker before acting.
- Parameters (3 touches, tolerances, score weights) are untested starting points.
- No session filter yet (opening auction, the 14:30 US open).
- Investment trusts are thin and trade on discounts to NAV. Use `--exclude-trusts` to skip them. To
  apply it to the scheduled tasks, add it to the `python` line in both `.bat` files.
- Prices are as Yahoo quotes them, mostly pence.
- This is a screening tool, not trading advice.
