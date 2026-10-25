# Synthetic workbook example

`ftse_outcomes_synthetic.xlsx` is a fictional fixture for inspecting the new workbook layout. It is not an observed market result and does not replace `ftse_outcomes.xlsx` or any cached outcome.

Regenerate the same example from the repository root:

```sh
python tests/report_fixture.py
cp runtime/ftse_outcomes_review.xlsx docs/examples/ftse_outcomes_synthetic.xlsx
```

The fixture includes profitable, losing, flat, skipped, missing and pending cases. Since 23 Oct 2026 the workbook has two sheets: **Live Mock Trades** (totals, one comparison line against every idea the scanner flagged, then each trade) and **Daily** (running balance and chart). The old Summary, Strategy Comparison, Breakdowns, Mock Trades (top 2 at 10:30) and All Outcomes sheets were removed; every idea's result is still saved in `scan_data/outcomes.v3.csv`.
