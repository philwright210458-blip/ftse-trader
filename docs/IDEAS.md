# FTSE Trader ideas register

*Copy of the Claude Doc "FTSE Trader ideas register", which is the master copy (Phil edits statuses there). Last synced: 18 October 2026.*

Every idea for improving the model goes here, so nothing gets lost. Ideas are tested side by side in the outcomes workbook before any change to the live scanner, and only once there are enough results (from early November at the soonest).

## Ideas register

| # | Idea | Source | How we'd test it | Status |
| --- | --- | --- | --- | --- |
| 1 | Trade with the trend: buys only above the 50-day average price, sells only below | Video 1 (investing basics, 17 Oct); Video 2 (moving averages, 18 Oct) | Compare profit per £100 for ideas that pass vs fail the filter | Test planned |
| 2 | Skip shares whose 20-period average (15-minute candles) is flat | Video 2 | Same comparison, using the slope of the 20-period line | Test planned |
| 3 | Don't enter when the price is overstretched, far above or below the 20-period line | Video 2 | Group ideas by distance from the 20-period line and compare results | Test planned |
| 4 | Skip the first pullback after a sharp run-up | Video 2 | Flag ideas that follow a steep rise and compare results | New |
| 5 | Avoid buys just below the 200-day average (treat it as a ceiling); use it as a profit target | Video 2 | Flag ideas with the 200-day line between entry and target | Test planned |
| 6 | Only count breakouts on above-normal trading volume | Video 1; Video 2 | Compare breakouts on high vs normal volume | New |
| 7 | Focus on "mover" days: shares that gapped or moved sharply since the open | Video 2; son's research model (5% movers filter) | Compare results on mover vs ordinary days | Test planned |
| 8 | Wider stops (e.g. 2% instead of about 0.6%) so stamp duty is a smaller share of each move | Model review, 17 Oct | Re-simulate the same ideas with wider stops and targets, side by side | Test planned |
| 9 | Trade FTSE index ETFs instead of single shares, as ETFs pay no stamp duty | Video 1 | Would need a separate scan of ETFs | Later |
| 10 | Run a real-share version inside a stocks and shares ISA so gains are tax-free | Video 1 | Check ISA dealing fees and API access before deciding | Later |
| 11 | Use spread betting or CFDs to avoid stamp duty and take the "sell" ideas | Model review, 17 Oct | Paper-test with the sells included, without stamp duty | Later |
| 12 | Broker for automation: eToro or IG front-runners; Trading 212 or Interactive Brokers for real shares; XTB ruled out (no API since March 2025) | Broker research, 17 Oct | Check API access on a demo account once the model passes its test | Later |
| 13 | Minimum quality score before an idea counts | Project notes, 14 Oct | Breakdowns sheet already groups results by score | New |
| 14 | Tighter proximity to the zone edge before entry | Project notes, 14 Oct | Group ideas by distance from the zone edge | New |
| 15 | Time-of-day filter (e.g. avoid the first hour or the last hour) | Project notes, 14 Oct | Group results by scan time | New |
| 16 | Remove three dead tickers (BSIF.L, IPF.L, JTC.L) from the share list | Project notes, 14 Oct | Housekeeping; no test needed | New |

## How to use this register

- **Adding ideas:** send Claude any new research (videos, articles, notes) and ask for it to be added, or add a row in the Claude Doc. Ideas added here in GitHub are pulled into the Doc at the next sync.
- **Status:** New = not yet looked at; Test planned = ready to test once there is enough data; Testing = running side by side in the workbook; Adopted = added to the live scanner; Rejected = tested and didn't help; Later = account or automation decisions for after the model passes its test.
- **One change at a time:** test ideas side by side first, and adopt only those that clearly improve profit per £100 after all costs, across enough trades and days.
- **First testing round:** early November, once about two to three weeks of results have built up. Likely candidates: ideas 1, 2, 3, 5, 7 and 8.

See also: [Model review and next steps (17 Oct 2026)](MODEL_REVIEW_2026-10-17.md).
