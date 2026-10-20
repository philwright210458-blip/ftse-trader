# FTSE Trader ideas register

*Copy of the Claude Doc "FTSE Trader ideas register", which is the master copy (Phil edits statuses there). Last synced: 19 October 2026.*

Every idea for improving the model goes here, so nothing gets lost. Ideas are tested side by side in the outcomes workbook before any change to the live scanner, and only once there are enough results (from early November at the soonest).

## Ideas register

| # | Idea | Source | How we'd test it | Status |
| --- | --- | --- | --- | --- |
| 1 | Trade with the trend: buys only above the 50-day average price, sells only below | Video 1 (investing basics, 17 Oct); Video 2 (moving averages, 18 Oct) | Tested 19 Oct, 45-day backtest: price vs 50-day average gave -42p per £100 after costs (no filter: -43p); adding a rising 50-day slope gave -49p. No improvement. | Rejected |
| 2 | Skip shares whose 20-period average (15-minute candles) is flat | Video 2 | Tested 19 Oct, 45-day backtest: requiring a rising 20-period average on 15-minute candles (with the 50-day trend) gave -54p per £100 after costs. Worse than no filter. | Rejected |
| 3 | Don't enter when the price is overstretched, far above or below the 20-period line | Video 2 | Not tested on its own; a 1% limit is built into the trend-pullback strategy (idea 17). | Test planned |
| 4 | Skip the first pullback after a sharp run-up | Video 2 | Flag ideas that follow a steep rise and compare results | Rejected |
| 5 | Avoid buys just below the 200-day average (treat it as a ceiling); use it as a profit target | Video 2 | Flag ideas with the 200-day line between entry and target | Test planned |
| 6 | Only count breakouts on above-normal trading volume | Video 1; Video 2 | Compare breakouts on high vs normal volume | New |
| 7 | Focus on "mover" days: shares that gapped or moved sharply since the open | Video 2; son's research model (5% movers filter) | Compare results on mover vs ordinary days | Test planned |
| 8 | Wider stops (e.g. 2% instead of about 0.6%) so stamp duty is a smaller share of each move | Model review, 17 Oct | Tested 19 Oct, 45-day backtest: stops of at least 1% or 2% gave -42p to -77p per £100 after costs over holds of 1, 2 and 5 days. No improvement. | Rejected |
| 9 | Trade FTSE index ETFs instead of single shares, as ETFs pay no stamp duty | Video 1 | Would need a separate scan of ETFs | Later |
| 10 | Run a real-share version inside a stocks and shares ISA so gains are tax-free | Video 1 | Check ISA dealing fees and API access before deciding | Later |
| 11 | Use spread betting or CFDs to avoid stamp duty and take the "sell" ideas | Model review, 17 Oct | Paper-test with the sells included, without stamp duty | Later |
| 12 | Broker for automation: eToro or IG front-runners; Trading 212 or Interactive Brokers for real shares; XTB ruled out (no API since March 2025) | Broker research, 17 Oct | Check API access on a demo account once the model passes its test | Later |
| 13 | Minimum quality score before an idea counts | Project notes, 14 Oct | Breakdowns sheet already groups results by score | New |
| 14 | Tighter proximity to the zone edge before entry | Project notes, 14 Oct | Group ideas by distance from the zone edge | New |
| 15 | Time-of-day filter (e.g. avoid the first hour or the last hour) | Project notes, 14 Oct | Group results by scan time | New |
| 16 | Remove three dead tickers (BSIF.L, IPF.L, JTC.L) from the share list | Project notes, 14 Oct | Housekeeping; no test needed | New |
| 17 | Trend-pullback strategy, a separate engine rather than a filter: rising 50-day trend, dip to a rising 20-period average on 15-minute candles, enter as it turns back up, stop below the dip, target 2x the stop distance | Video 2; Claude review, 19 Oct | Tested 19 Oct, 45-day backtest: 4,589 signals averaged -5p per £100 before costs, -41p after; first 3 a day, buys only: +15p before costs, -45p after. No reliable advantage. | Rejected |
| 18 | Move from 15-minute trades to daily-chart trades held for weeks, so the 60p cost per £100 is small next to the size of the moves | Claude review, 19 Oct | Backtest on several years of daily prices (Yahoo keeps decades), far more evidence than 45 days | New |

## How to use this register

- **Adding ideas:** send Claude any new research (videos, articles, notes) and ask for it to be added, or add a row in the Claude Doc. Ideas added here in GitHub are pulled into the Doc at the next sync.
- **Status:** New = not yet looked at; Test planned = ready to test once there is enough data; Testing = running side by side in the workbook; Adopted = added to the live scanner; Rejected = tested and didn't help; Later = account or automation decisions for after the model passes its test.
- **One change at a time:** test ideas side by side first, and adopt only those that clearly improve profit per £100 after all costs, across enough trades and days.
- **Backtests (19 Oct):** ideas 1, 2, 8 and 17 were tested on 45 days of history with `ftse_backtest.py`; results are in `ftse_backtest.xlsx`, `ftse_backtest_hold2d.xlsx` and `ftse_backtest_hold5d.xlsx`.

See also the Claude Doc "FTSE Trader: how the current model works" (model description, comparison with the original video, and the version 2 and momentum results).
