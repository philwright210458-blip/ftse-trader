# Replay, decision journal and daily report

The purpose is to make the paper engine's decisions understandable before a forward pilot. The existing dashboard style, £100 notional positions and current risk settings are retained. This change adds no broker route, hosted deployment or schedule activation.

## Try the three scenarios

With Node 22, from the repository root:

```sh
node dashboard/server.mjs --demo
```

Open `http://127.0.0.1:8787/?demo=1#replay`. Choose a scenario and select **Start scenario**. Use Previous/Next frame, Play/Pause, the time slider, Reset and playback speed. Playback pauses when navigating to another view or hiding the tab. Leave replay to restore the previous dashboard context.

The three fictional scenarios explain a riser reversing with shorts disabled, a faller rebounding into a future-filled long, and a missing candle leaving a position unresolved. These are condensed morning examples, not complete market-day recordings. Their setups are deliberately authored fixtures; they demonstrate the actual execution engine, not successful production signal discovery. Their prices, fees and outcomes are fictional.

Each frame was produced by a separate, progressive run of the actual Python engine. Only observations available by that clock were ingested. The browser selects frozen frames; it does not implement a second trading model. Earlier frames are independent copies, so rewinding cannot retain a later position or outcome. The full bundle is available to the browser, so this is an explanatory replay rather than a blind research experiment.

The replay banner persists in share detail, portfolio, journal, report and exports. Background refresh and archive selection pause during replay. No replay control sends a trade, changes the current journal or publishes a snapshot. Both publication and ingestion reject snapshots carrying replay metadata.

## Read the journal and report

The decision journal explains opening observations, waiting for completed candles, signal availability, eligible entry intervals, rule rejections, paper fills, exits and missing evidence. Observation time and modelled candle execution time are distinct. Search or filter the journal and download its visible entries as CSV.

The daily report follows the selected current, archived or replay snapshot. It distinguishes a partial-day report from a session that has ended, shows entries and closures, and reconciles price P/L with commission and purchase tax. Spread and slippage already affect the price result and are not deducted a second time. Closed trade P/L is attributed to when its exit became known; entry-cohort dates and balance changes are explained separately.

Daily aggregates use complete stored records rather than bounded dashboard trade lists. The explanatory entry list is capped at 150, with total counts and truncation visible. Missing observations, carried exposure, stale valuations, new gap warnings and open positions with unresolved coverage remain explicit. Observed delay means first stored availability minus candle end; archive backfills can increase it, so this is not a verified measure of provider latency. An absence of warnings does not prove complete feed coverage.

Download report text to review its numbers and limitations outside the dashboard. Replay exports carry a REPLAY label and use only the selected frame. The report is a readable research record, not an investment recommendation or evidence of a reliable daily return.

## Generate examples or replay recorded observations

Python 3.12 and the existing requirements are needed to regenerate engine outputs:

```sh
python dashboard/scripts/generate_examples.py
python ftse_research_cli.py replay --out runtime/fictional-replay.json
python ftse_research_cli.py replay --input runtime/observations.json --out runtime/recorded-replay.json
```

Example generation uses only in-memory SQLite journals. It writes the two labelled fictional files under `dashboard/public`; these checked-in files let Node-only preview users inspect engine-generated examples. Regenerate them after changing replay, engine or report behaviour. The hosted build embeds only fictional replay examples and rejects a recorded-data replacement.

For recorded replay, supply an object with `session`, `universe` and `candles`, plus optional `clocks` and `title`. Each universe record needs an explicit ticker and `GBp` or `GBP` quote unit. Each candle needs the original ticker, interval, start/end, OHLC, volume, quote unit, source label and timezone-aware `fetched_at`. Optional `available_at` must agree with that original observation timestamp. Daily candles need adjusted closes to establish previous-session movers. Missing adjusted closes withhold that classification.

The file must contain completed observations from one consistent, non-synthetic source and the declared exchange session, with any earlier history needed by the scanner. Future-session candles, missing timestamps, synthetic/recorded mixtures and incompatible units are rejected. The CLI never fetches prices or invents missing times. Without `clocks`, original current-session observation times determine frames; supply a shorter ordered clock list if there would be more than 100 frames. Input is capped at 64 MiB and the generated browser bundle at 12 MiB.

Recorded replay reruns current production scanner rules and the default paper configuration. It does not reconstruct historical rule versions, historical membership, executable prices or liquidity. Supplied capture provenance is not independently verified. Freshly downloading old prices does not recreate what would have been known in real time.

Use **Load a generated replay file** to open the resulting bundle in the browser. It stays local and is not uploaded. Invalid files leave the previous scenario intact. The input observation file, operational database and current snapshot cannot be overwritten through this replay command.

## Review acceptance

1. Step forward and backwards: later candles, signals and trades disappear when returning to an earlier frame.
2. Confirm the short remains skipped, the long fills only after availability, and the data-gap outcome stays unresolved.
3. Open the journal and report from a middle frame. Their downloads must contain no later observations and must retain the replay label.
4. Pause, change scenarios, navigate away and return. Controls must remain predictable and the prior current/archive snapshot must restore when leaving replay.
5. Check the cost bridge and distinguish missing data from zero profit. Review normal, narrow-screen and keyboard use.

Run the Python and Node suites listed in the README. Hosted two-user access, unattended market collection, forward-pilot evidence and native Excel checks remain the separate activation gates from the original implementation. Merging this PR does not activate the new scheduler.

## Verification for this extension

All 122 Python tests and 17 Node/API tests pass locally. The built Worker is loaded by the Node suite, including private-reader checks and bundled replay assets. Browser checks covered stepping and rewinding, source labels across views, the journal and daily report, actual local file selection, and a 390-pixel document width without horizontal overflow. Hosted deployment, real captured-market replay and forward profitability have not been established by these checks.
