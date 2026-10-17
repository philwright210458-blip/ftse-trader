# Research setup and operations

## Review and activation boundary

The current change belongs to a separate unmerged pull request. The existing full scan, watch and outcomes workflows, external cron-job.org timers and their outputs are not modified. No hosting resources or account invitations are created automatically.

The repository includes all source configuration for a durable research runner and a private dashboard. Activation follows review and requires access to the chosen host and Sites account. A second user's ability to visit a private Site must be verified, rather than assumed. Current prices and plan limits need checking before provisioning paid resources.

## First local integration

Install the pinned Python requirements in a virtual environment and use Node 22. Build a separate demonstration ledger:

```sh
python ftse_research_cli.py demo --database runtime/demo.sqlite --out runtime/demo-snapshot.json
```

Set a long random **local development only** value in the environment as `INGEST_TOKEN` before starting `node dashboard/server.mjs`. Use the same value in the collector environment as `FTSE_INGEST_TOKEN`, with `FTSE_SITE_URL=http://127.0.0.1:8787`. Keep token values out of commands saved in documentation, commits, screenshots and browser code. The collector's `push` command reads the chosen ledger and publishes its snapshot:

```sh
python ftse_research_cli.py push --database runtime/demo.sqlite
```

A demo ledger requires `research.mode` set to `synthetic-demo` in a separate local configuration copy; never change the production research configuration to reuse a demo database. Alternatively publish the generated snapshot using the `push_snapshot` helper from a local script. Successful publication requires an accepted or duplicate acknowledgement followed by matching durable metadata readback. Reader permissions remain separate from writer permissions.

Verify the received data through the ordinary dashboard URL, then repeat the update and inspect duplicate handling. Exercise missing credentials, a stale snapshot, a conflicting timestamp and a failed request. All these cases have offline regression checks. No real account is required for the local loopback preview.

## Durable Python host

Use one Linux runner with a persistent local data volume. Do not share a SQLite journal across network filesystems or independent hosts. The collector uses a process lock and SQLite transactions, preserving evidence and paper capital through restarts.

1. Check out the reviewed revision, install Python 3.12 and the pinned requirements in a virtual environment.
2. Create an unprivileged `ftse-research` service user. Place the application at `/opt/ftse-trader`, the private configuration at `/etc/ftse-research.json`, credentials at `/etc/ftse-research.env`, and owned durable storage at `/var/lib/ftse-research`.
3. Set configuration paths to `/var/lib/ftse-research/research.sqlite` and `/var/lib/ftse-research/snapshot.json`. Use an absolute path to the reviewed universe CSV. Review all virtual risk and cost assumptions.
4. Store `FTSE_SITE_URL`, `FTSE_INGEST_TOKEN` and the supported private Sites service credential `FTSE_SITES_SERVICE_TOKEN` in the restricted environment file. Set its permissions to owner-readable. The app never stores those values in its journal or snapshots.
5. Initialise the paper database with the configuration. Verify the session plan and database backup before enabling collection.
6. Only after acceptance, change the deployment copy's `enabled` field to `true`. Install `deploy/ftse-research.service`; append `--push` to its command when the authenticated dashboard path is verified. Start the service and inspect its first completion and readback.

The shipped unit uses a read-only application filesystem, an unprivileged user and one writable data directory. It is **not** installed or started by tests. A Dockerfile is also supplied; mount `/app/runtime` persistently, use a deployment-specific configuration and inject secrets through the host. Build/deploy support is supplied, but an actual Docker image has not been verified unless recorded in the status document.

A GitHub Actions job with an empty temporary filesystem must not start a new account ledger on every run. This is why there is no new scheduled Action in the branch. The existing timers can keep running until an explicit cutover. Once accepted, disable the old timers only after proving the new worker's operation; retain their logs and original exports.

## Timing and failure behaviour

The scheduler prepares the watch fifteen minutes before the calendar's open, performs an opening observation, checks fifteen-minute boundaries plus ninety seconds, and resolves after the actual close plus fifteen minutes. Holidays and shortened sessions come from the London calendar. It watches the entire session. The ninety-second allowance is configurable and must be compared with measured provider delay.

A job more than the configured lateness allowance behind is marked missed. The scheduler rechecks lateness immediately before claiming each job, including after a long collection. It does not create backdated paper entries. An overlapping process reports busy, and completed jobs are not repeated. Failed jobs remain terminal for their scheduled window; the next scheduled observation can collect new evidence. Diagnose the error before deliberately running a replacement observation. There is no unlimited automatic retry.

The browser reads the latest stored results once a minute. This does not fetch prices or wake the collector. Its clock can mark a previously successful snapshot stale without changing the underlying result. Failed publications are retained in collector job history and the local health snapshot. A network outage may prevent the remote dashboard receiving a failure marker, so age warnings are essential.

## Backups and restoration

Use the online SQLite backup command, which verifies integrity and reconciles tables against a consistent source read transaction:

```sh
python ftse_research_cli.py backup --out runtime/research-backup.sqlite
```

Use an unused destination; the command refuses to overwrite a backup. Copy backups off the runner using the host's secure backup facility. Keep credentials separate. Restoration means stopping the service, verifying an isolated backup copy, pointing a test configuration at that copy and comparing account balances, open positions, event counts and latest run. Only then replace the production file during a controlled restart. Never overwrite an active database or reset initial capital to recover from an error.

Keep original legacy CSVs and workbooks as the baseline. Changes to simulation rules write a separate outcome-cache version. A corrected historical outcome can remain missing if the provider no longer supplies sufficient history; missing evidence must not be converted to a zero return.

## Sites source and account-only steps

The Worker and D1 source is in `dashboard/`. Its build is local and does not deploy. At activation, register a private Site using the native Sites workflow, preserve the actual returned project identity and configure the supported storage binding and migrations. The example manifest is not a registered project.

Set server-only ingest credentials, the private dispatch trust setting and explicit authorised reader IDs. Verify one collector update while the page is closed, its readback, a fresh browser session for each intended reader and rejection for an unauthorised account. If private sharing or service access cannot be supported for the intended accounts, stop that hosting route and choose a compatible private host. Never make the dashboard public to bypass a sharing problem.

These account-only activation steps are deliberately outstanding. They cannot be honestly represented as completed by a source pull request or local test.

## Complete journal export

The shared dashboard exposes bounded snapshots and paginated archived revisions. The authoritative journal retains all records. To export an entire table without the display limits:

```sh
python ftse_research_cli.py export --table trades --out runtime/all-trades.csv
python ftse_research_cli.py export --table candles --out runtime/all-candles.csv
```

Use a new destination for each export. Numeric fields stay numeric and untrusted string values are protected against spreadsheet formula injection. Payload columns retain the full original record as JSON. A direct snapshot-file update is also supported without opening or changing a journal:

```sh
python ftse_research_cli.py push --snapshot-file runtime/demo-snapshot.json
```

## Preserve the original scanner archive

Before importing, take a journal backup. Run this against the deployment's market-paper journal, not the fictional demo:

```sh
python ftse_research_cli.py import-legacy --legacy-dir scan_data
```

The import is content-addressed and repeatable. Source CSVs and the historical workbook are never rewritten. Every imported row is labelled `legacy-unverified`, displayed separately in History and excluded from corrected paper P/L, balances, pilot counts and strategy comparisons. If a CSV changes, a new archive version is retained rather than replacing the first. Complete raw records can be exported using `export --table legacy_records`.
