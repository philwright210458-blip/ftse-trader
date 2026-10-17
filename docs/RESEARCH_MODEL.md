# Data and paper simulation rules

## Separate clocks and evidence

Every candle has its provider interval, source times, first observed availability, fetch time and quote unit. The journal retains first observations and records provider revisions separately. A later download cannot make a signal available earlier. Simulated entries are queued only after signal registration and use a future eligible candle; missing or stale entry intervals are rejected rather than replaced by a convenient later price.

The collector uses completed positive-volume bars. It does not infer trading from repeated placeholder prices. Outcomes over unobserved intervals remain unresolved. Historical replay and forward simulation are separate experiments; a batch download alone does not recreate what the system saw at each historical moment.

## Yesterday and the open

A mover's previous-day return uses adjusted closes from the prior completed exchange session and the session before it. This is distinct from the raw opening gap and movement since today's open. Corporate-action adjustments prevent a split or dividend adjustment being silently treated as an ordinary market move. Missing adjusted data withholds the classification.

Both risers and fallers are shortlisted at a configurable absolute 5% initial threshold. Three completed traded closes moving consistently, with the move since the open in the same direction, classify reversal or continuation. Otherwise the display remains undecided or unavailable. These labels describe observations; they are not a validated prediction. Existing range/breakout rules remain the signal source, with mover context recorded for comparison. A prior rise never automatically creates a short trade.

Instrument quote units are explicit. The existing universe is primarily Yahoo London share symbols, and its initial import assumes GBp unless an explicit column supplies GBP. Confirm these units when updating the universe or adding instruments. Currency ambiguity is not permission to silently convert a position. The research model uses fractional simulated quantities, which a broker may not support.

## Virtual capital and costs

The user chose £100 notional per paper position. The sample account starts at £1,000 with at most three positions and a 2% virtual daily loss guard. These latter values are proposed research settings, not live trading limits or recommendations. Initial capital and demonstration/market-paper mode are immutable within an experiment. Risk-setting changes are captured in configuration versions so they cannot silently rewrite prior decisions.

Spread, slippage and per-side commissions are explicit assumptions. The model applies half the round-trip spread on each side plus adverse slippage and commission. Long paper share purchases include configurable modelled stamp duty, initially 0.5%, in addition to spread, slippage and commissions. £100 is the notional purchase value, so entry costs require additional virtual cash. The rate is a research assumption that must be checked for the actual instrument and account before interpreting it as a real charge. Borrow costs are not modelled; the legacy workbook also displays its separate stamp-duty case. Default synthetic shorts are disabled. If enabled for research, they remain labelled as a synthetic model without established borrowing availability or charges.

Stops take precedence when stop and target both occur inside a candle. A stop gap uses the worse opening price. Target gaps do not receive an invented improvement. Exit time is the completed candle's end, with intrabar time explicitly unknown. Default forward-paper positions close at the session end only when the required bar is present. Missing close evidence keeps the position and its exposure visible.

The engine enforces position capacity, available virtual capital, same-session re-entry policy and daily loss guards. An eligible signal can still be rejected, and a no-trade day is a valid result. No profit target forces entries. No broker API, real-money account credentials or order-transmission route exists.

## Results and evaluation

Realised balances and drawdown use exit order. Marked equity estimates open-position liquidation with the stated cost assumptions; stale marks require visible qualification. Entry-cohort attribution is a separate analysis, not an account equity curve.

The workbook's main result sums pound outcomes from actual simulated position sizes and risk distances, then applies the cost bridge. Target/stop/time-limit outcomes are not synonymous with profitable/losing/flat trades. Missing, pending, skipped and completed counts reconcile to all considered ideas. Alternative holding periods evaluate the same ideas separately and must not be added together.

Evaluation keeps same-time observations and strategy/configuration versions. It reports the existing scanner, mover context and combination with selection/coverage caveats. Cohorts must have matching decision times and eligible universes before drawing ranking conclusions. Pilot thresholds in the configuration are proposed defaults; satisfying them cannot authorise real trading or establish a guaranteed edge. The 1% daily aspiration is shown as an unvalidated ambition, with account basis and costs explicit.

Initial collection requests 60 days of intraday history. Later collections request five days once all requested instruments have stored history. Scanner calculations use a configurable 60-day window so the strategy does not change merely because the archive grows. Open positions can still recover older missing evidence from the full archive.

Complete research evidence remains in the durable SQLite journal. Dashboard snapshot payloads have a bounded display budget and expose coverage limits. Archived snapshots and record exports support inspection; an empty or bounded view must not imply that omitted history was deleted.
