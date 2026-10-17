# Private dashboard: collected scanner records

The private Site now has a separate **Collected scans** home page. It reads actual CSV records committed by the existing full-scan, watch and outcomes jobs. The fictional dashboard, journal and replay stay at `/demo?demo=1`. Scanner outcomes never become corrected paper-ledger trades or profits.

## How updates work

1. Your existing external timers trigger the same GitHub workflows at the same times.
2. Those workflows collect data and commit their CSVs as before.
3. Once this integration is merged onto `main`, a separate `workflow_run` publisher wakes after any of those three jobs finishes. It copies the committed archive into private Site storage. It does not collect prices, change timers or execute trades.
4. The browser checks stored publications every minute while open. It shows scan collection time, source candle time and publication time separately. Refreshing the browser cannot create fresh market data.

The publisher deliberately checks out the latest `main`, with full history. The upstream job's starting SHA predates its saved CSV commit and must not be used as the archive source. A failed upstream job can still publish the previous archive, labelled with the failure. A successful job with no usable market data need not create a new scan; the last actual scan time remains visible.

Publication is capped at 100 runs, 3,000 setups and 2,000 outcomes per source file, then reduced to a 1.8 MB transfer budget if needed. The UI and exports disclose included versus total rows. The GitHub CSV archive remains untouched and complete. D1 stores immutable publication revisions and the current pointer. Source commit sequence and timestamp guards prevent older or conflicting publications replacing a newer source. Main history should not be force-pushed; a rewritten history requires an explicit publisher reconciliation.

## Configured credentials and hosting

GitHub Actions repository secrets:

- `FTSE_SCANS_WRITE_TOKEN`: dedicated application publisher token, also set as the Site's secret `SCANS_WRITE_TOKEN`.
- `FTSE_SCANS_SITE_ACCESS`: the supported service credential for this private Site, sent only in `OAI-Sites-Authorization`.

Repository variable: `FTSE_SCANS_SITE_URL`, the HTTPS origin of the private Site. No GitHub credential is stored in the Site. The existing Site's private audience, including both invited dad email addresses, is retained. `SCANS_TRUST_SITES_DISPATCH=true` is appropriate only behind native Sites dispatch, which validates approved visitors and supplies signed-in identity headers. Never expose this Worker through an untrusted proxy that accepts spoofed identity headers.

The deployed Worker uses a separate D1 scan archive schema in `dashboard/scan-migrations`. It has no collector, broker or timer. Build it with `node dashboard/scripts/build-scans-site.mjs /absolute/output-folder`. This creates `dist/server/index.js` and root `drizzle/`; preserve the existing Site's registered identity in `.openai/hosting.json`, remove `static`, and set `d1` to `DB`. Package and deploy through the supported Sites workflow. Do not deploy `scan-site-build` as a public standalone Worker.

Initial publication uses the current immutable `origin/main` archive, with a persisted digest and source-commit receipt. Automatic forwarding only starts when this PR is merged onto `main`. Repository secrets alone do not activate a workflow.

## Review and test before merging

This PR is stacked on PR #2 because its fictional demo uses the replay and journal assets. Review this integration diff against that branch; merge PR #2 first, then this PR. Neither PR is merged automatically.

1. Open the private Site using an invited ChatGPT account. The home page should say **Collected scans**, show actual company names and clearly disclose the latest scan's date. Check that an uninvited account cannot access it. Both invitations are saved; acceptance from dad's own account still needs his check.
2. Select a full scan and a watch scan. Compare the counts and an example setup's entry/stop/target with the corresponding `scan_data/runs.csv` and `setups.csv` rows. Multiple setups may refer to one share. Try the search and direction filter. Download each included table.
3. Follow **Explore the fictional demo**. It must visibly say fictional/demo and show its separate replay and journal. It must not show real scanner rows as paper profits. Return to `/` for actual records.
4. Check old scan times stay old when the page is refreshed. A newly copied archive is not a new quote. Review the file coverage limits and legacy-outcome warning.
5. Run `python -m unittest discover -s tests -v` and `node --test dashboard/tests/*.test.mjs` on Node 22 / Python 3.12. The tests cover actual immutable CSV conversion, private reads, dedicated publisher authentication, demo rejection, payload caps, SQL ordering and conflicting publishers. Network access to a broker is never involved.

Before merge, `python publish_scans.py --ref origin/main --out /tmp/ftse-scans-review.json` is a safe local conversion only. Do not use the production writer to test invalid data or invented source commits. Use local test stores for negative cases.

After merge, the first existing scan completion should start **Publish collected scans to private dashboard**. It can also be started manually to copy existing records without starting a scan. Verify the publisher reports `readback_verified: true`, then check the Site's source commit and latest scan clock. If it fails, the scan jobs continue independently; the Site retains its previous records and age warning. Do not adjust any external timer as part of this test.

## Limits

These CSVs do not contain the full candle series, historical decision evidence or the corrected paper ledger. Do not infer those from setup rows. Price units are not declared in the archive, so levels are displayed exactly as recorded. Legacy outcomes remain unverified. This integration makes the existing collection visible; it does not establish a profitable strategy or claim live execution readiness.
