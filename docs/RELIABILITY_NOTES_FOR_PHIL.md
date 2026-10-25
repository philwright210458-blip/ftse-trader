# Reliability fixes: notes for Phil

These changes make the paper-trading records more dependable. They do not change which chart patterns the strategies look for, promise better returns or place real orders.

## 1. A missing page cannot tell us who won

Imagine reading a match report with a page missing. You cannot safely say what happened on that page.

The daytime tracker could jump over missing prices and report a win. The evening checker was more careful. Worse, the daytime result could make room for another trade.

Both now use the same checking rules. If an important price is missing, you will see **Price check incomplete**. The trade keeps its place until enough evidence arrives. Early market closes also use the proper exchange closing time.

## 2. A replay cannot peek at the next page

The model-check backtest could discover that a trade would fail to start later, then immediately give its place to another trade from the same scan. The running scanner did not know that outcome yet.

The replay now reserves places when decisions are made. It frees them only when the relevant price candle has finished. A rejected entry still counts towards the daily decision limit, just as it does in the running scanner.

This makes the comparison fairer. It does not guarantee that the result will improve. Old model-check workbooks describe the old calculation and have not been replaced by this PR.

## 3. Each share gets its own bookmark

The structure trader used one bookmark for the whole share list. If one share downloaded and another failed, the bookmark could move past the missing share's signal.

It now keeps a bookmark for each share and stops at missing sessions. On recovery it works through the missed days in order, allowing a new trade after the previous one has closed.

Recovered signals say **Recovered after missing data** in the Trades sheet. The Summary counts them and says they are included in its totals. These are reconstructed signals, not proof that the computer recorded them on the original day. Existing recorded decisions take priority if a recovered signal would overlap one.

The first updated run keeps copies of the old structure records and bookmark before migrating them. Repeated runs do not duplicate trades.

## 4. Yesterday's price is not today's opening price

The momentum trader could substitute yesterday's closing price when today's opening price was missing. That could record a purchase or sale at a price we never actually observed.

It now waits for the real opening price from the intended day. It does not switch to a later day's opening price.

If any required price is missing, the whole group of purchases and sales waits. This keeps money set aside for the intended purchases and prevents spending money from an unconfirmed sale. The status is **waiting for opening prices**. Later monthly decisions also wait until this group is resolved.

If the price never becomes available, the group stays unresolved. It needs investigation; the program will not guess a price.

## What happens to earlier results?

- Recorded scanner decisions stay unchanged.
- The original outcome files remain. New calculations go into `outcomes.v3.csv`, alongside the older files.
- The outcomes workbook states that it uses the new checks. Totals may differ as results are rechecked; unresolved trades do not count as completed results.
- Missing historical prices cannot be recovered from a list of signals alone. We have not claimed a corrected historical profit figure.
- The first evening outcome check after merging may take longer because it builds the new result file.

At the reviewed starting commit there were 14 recorded intraday decisions, no structure trades and no momentum orders. The regression tests use made-up prices to prove the fixes; they are not new market results.

## GitHub space and running time

The dashboard publishing workflow remains switched off. This PR does not restart it or run the long market backtests.

The read-only usage check found about 7.4 MiB of downloadable artifacts, 82.7 MiB of cache and a GitHub-reported repository size of 71.5 MiB. It also found recent backtests taking roughly 90 and 112 minutes. Storage and running time are different allowances. These figures alone do not tell us which account limit was reached.

The next useful check is the account's GitHub billing and usage screen. Keep the short automatic code checks enabled: they help catch mistakes before changes are merged. Do not delete trade history to save space without a separate backup and retention plan.
