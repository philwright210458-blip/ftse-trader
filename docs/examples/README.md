# Synthetic workbook example

`ftse_outcomes_synthetic.xlsx` is a fictional fixture for inspecting the new workbook layout. It is not an observed market result and does not replace `ftse_outcomes.xlsx` or any cached outcome.

Regenerate the same example from the repository root:

```sh
python tests/report_fixture.py
cp runtime/ftse_outcomes_review.xlsx docs/examples/ftse_outcomes_synthetic.xlsx
```

The fixture includes profitable, losing, flat, skipped, missing and pending cases. Summary shows the main strategy's realised pound result and costs, Strategy Comparison separates alternative holding periods, and Breakdowns reconciles each partition with its own baseline. The remaining tabs preserve the trade ledger and detail.
