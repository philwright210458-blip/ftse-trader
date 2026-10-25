# Paper-trading reliability update

Baseline: `50011438503318f19d928872feae6df703717343`, reviewed on 25 October 2026.

Plain-English explanation: [Notes for Phil](RELIABILITY_NOTES_FOR_PHIL.md).

## Implementation

- `ftse_execution.py` owns completed-candle preparation and as-of-time execution. It has no scanner, reporting or network dependency. Existing scanner/outcome imports remain available for callers.
- The intraday display and model-check replay use this engine. Replay checks previous decisions at each scan time and reserves new decisions before learning their eventual results.
- Intraday gaps stay unresolved. A daily range can bridge a gap only after that session closes. Missing entry candles do not prove a rejected entry.
- `outcomes.v3.csv` isolates changed execution semantics from v1/v2 caches. The publisher selects v3 when present, otherwise v2, without mixing the two. The paused site's validator and source label recognise v3; no site deployment or workflow activation is part of this change.
- Structure state schema 2 uses `checked_by_ticker`. Migration ignores the untrustworthy global progress date, rechecks available history and preserves recorded decisions. `trades.pre-reliability.csv` and `state.pre-reliability.json` are created once. Trade/state replacements are atomic individually; writing trades first and deduplicating signals makes interrupted progress writes replay-safe.
- Structure recovery processes signals in date order with historical occupancy checks. Original recorded decisions take priority over overlapping recovered signals. Missing execution sessions leave positions unresolved.
- Momentum executes a rebalance only when every required opening price for its original session is present. Waiting status survives file reloads and blocks later monthly decisions. This deliberately favours incomplete but honest records over partial allocations or invented fills.

## Regression evidence

The four review reproductions failed against the baseline, then passed after the fixes:

| Scenario | Before | After |
| --- | --- | --- |
| Entry candle missing, later candle reaches target | Daytime tracker reports a win and frees a slot | Incomplete price check; slot retained |
| Seven candidates, first five later gap beyond their targets | Replay considers all seven in the same scan | Only the first five decisions reserve places |
| Share B missing while share A advances | B's recovered signal is permanently skipped | B resumes from its own progress record |
| Original opening price absent | Previous close returned as execution price | Unresolved; no substituted fill |

Additional checks cover observable rejection timing, a late missing entry, early market closes, daily-data look-ahead, scanner/replay selection parity, missing-session recovery, multiple chronological signals, migration backups and reruns, momentum cash preservation and file reloads, missing sale prices and later-decision blocking, and v3 archive provenance.

## Validation

- Python 3.12: all 260 tests passed (`python -m unittest discover -s tests`).
- Node 22: all 23 dashboard tests passed, including API authentication, v3 source validation and both Worker build tests.
- The offline research `demo` and `evaluate` CLI pipeline completed using temporary files.
- `git diff --check` passed. No recorded CSVs, workbooks, workflow schedules or secrets were changed in this PR.
- No live market execution or provider-data correctness is established by these offline checks.

## Historical impact and migration

Read-only baseline inventory: 14 live mock decisions dated 20–23 October 2026; 2,074 cached v2 outcomes dated 15–23 October; zero structure trades; zero momentum orders. The committed structure state had advanced to 23 October.

The repository does not contain the raw market candle paths needed to determine which of the 14 intraday decisions were affected. Their historical P/L impact is unknown. Existing spreadsheet backtest results remain outputs of the older implementation. No large market backtest or provider download is needed to validate the regression fixes, and none was run for this PR.

Keep original caches and recorded decisions. The normal outcome job builds v3 without importing v2 results as newly validated evidence. This can make its first run slower and can leave older results unresolved where provider history is unavailable. The workbook identifies the new checks and excludes unresolved results from completed totals.

Structure migration backs up original files before writing; on recovery, new rows have their actual recording timestamp and a recovered label. If necessary, inspect these files before any separate manual rollback. Do not restore an old bookmark over newly recorded trades.

## GitHub usage check

Read on 25 October 2026 through repository-scoped API calls:

| Item | Observed |
| --- | --- |
| Repository size reported by GitHub | 73,267 KiB, about 71.5 MiB |
| Current tracked tree at baseline | About 9.1 MiB |
| Downloadable artifacts | 16, total 7,754,380 bytes, about 7.4 MiB |
| Actions caches | 1, total 86,752,064 bytes, about 82.7 MiB |
| Dashboard publisher | `disabled_manually` |
| Recent model-check workflow | About 90 minutes elapsed |
| Recent stop/target workflow | About 112 minutes elapsed |

Elapsed workflow times are not verified billed minutes. Account-wide usage, included allowances and the exhausted quota were not available from these repository checks. No artifacts, caches, history or schedules were deleted or changed. Keep long backtests deliberate and inspect the account usage screen before choosing a retention or compute change. Rewriting Git history is outside this fix.
