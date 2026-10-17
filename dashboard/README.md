# FTSE Research dashboard

Private, shared browser interface for the scanner and automatic **paper** trades. No broker integration, real orders, payments or live financial transactions. This change is source for review. No Site, database, schedule or audience is created or changed by running the build.

## Local preview and checks

Requires Node 20 or later, with no dependency installation:

```sh
cd dashboard
node server.mjs
# http://127.0.0.1:8787
node --test tests/*.test.mjs
node scripts/build.mjs
```

The initial view is explicitly empty. To inspect fictional example data, use `node server.mjs --demo` and open `http://127.0.0.1:8787/?demo=1`. The demonstration endpoint is absent unless explicitly enabled, and the interface demo is never embedded in the hosted Worker. Explicitly labelled fictional replay examples are embedded behind the same private-reader boundary as the dashboard. The banner remains visible in every demo view. API snapshots and archived snapshots marked `mode: synthetic-demo` also show that banner without any URL flag; their CSV downloads carry a DEMONSTRATION filename. This fixture is a UI example, not a model result.

For an end-to-end local ingest, set `INGEST_TOKEN` in the terminal environment before starting the server. POST a valid scanner snapshot to `/api/ingest` with `Authorization: Bearer <token>` and `Content-Type: application/json`. No default token exists. The local server binds only to loopback, rejects non-loopback hosts and cross-origin browser writes, and writes ignored `.local-data/` files. It is a single-process development server, not an internet-facing hosting service. Paper records live in the collector; the dashboard retains immutable complete snapshot revisions.

## Hosted private deployment, deferred until reviewed

`worker.mjs` is a dependency-free Cloudflare Worker ESM implementation intended for the ChatGPT Sites authenticated private dispatch boundary. `scripts/build.mjs` embeds HTML/CSS/JS and the API into `dist/server/index.js`; SQL migrations are copied into `dist/migrations/`. The build runs without credentials and never deploys. Do not deploy the Worker directly to a public workers.dev route with trusted identity headers enabled.

At deployment time, use the native Sites registration and source workflow. Confirm the supported D1 manifest declaration and migration location using the then-current Sites tooling; `hosting.example.json` documents the intended binding and is deliberately not an active registered manifest. Persist the actual returned private Site ID in `.openai/hosting.json`, provision D1 as `DB`, and apply `migrations/0001_snapshots.sql` before serving requests. Keep secrets outside source and the browser bundle. The existing scanner remains external to the Site.

Required runtime configuration:

| Name | Purpose |
| --- | --- |
| `DB` | Provisioned D1 database binding with the supplied schema |
| `DEPLOYMENT_MODE=sites` | Explicit private Sites hosting mode |
| `TRUST_SITES_DISPATCH=true` | Trust identity headers only behind authenticated Sites dispatch |
| `READER_USER_IDS` | Comma-separated explicit authorised Site-scoped ChatGPT user IDs |
| `INGEST_TOKEN` | Long random server-only token used by the external scanner |

All production dashboard routes, snapshots and CSV exports require the Sites-provided stable authenticated user ID to be in the allowlist. Service credentials cannot read dashboard data. The collector requires both the private Site service-access credential in `OAI-Sites-Authorization` (validated and consumed by Sites dispatch) and the separate application ingest token in `Authorization`. The application token alone must never bypass private Site access. Do not lower the Site audience to make ingestion work. Confirm two authorised user accounts and one unauthorised account at deployment. A private Site may impose owner-only access; the second-user access capability must be proven before choosing this host.

The API hashes token comparisons before constant-length comparison, streams and limits payloads to 2 MiB, validates the snapshot contract and candle ranges, rejects future/stale snapshots, returns duplicates idempotently, and rejects conflicting snapshots with identical generation times. D1 batches the immutable revision insertion and current-pointer update atomically. Snapshots and exports are never cached. Reader names and prices render as text; no external scripts or chart CDN are used. API failures preserve the last displayed snapshot with a visible warning. A one-minute browser refresh only reads stored results; it never triggers collection or fabricates freshness. The authenticated snapshot archive is paginated; opening an earlier revision pauses refresh and displays an archive warning until the reader returns to latest. CSV exports use that selected revision. Coverage counts explain bounded current views; complete immutable collector journal exports remain available through the CLI.

## Contract

`GET /api/history?limit=20&before=<UTC-time>` returns paginated immutable revision metadata and `next_before`; `GET /api/history?revision=<digest>` retrieves an earlier complete snapshot for an authorised reader. `GET /api/snapshot` returns `{revision, snapshot}`; both values are null before the first successful update. `POST /api/ingest` accepts a complete schema-version-1 snapshot and returns 201 accepted, 200 duplicate, 409 stale/conflict, 401 missing/bad token, 413 too large or 422 invalid schema. `GET /api/ingest-status` requires the ingest bearer and returns only `{revision, generated_at}` so an unattended writer can verify acknowledgement without a reader grant. `GET /api/export?table=…` returns formula-safe CSV for watchlist, candidates, runs, positions, closed_trades events or legacy.

Required top-level snapshot fields: `schema_version`, UTC `generated_at`, `last_success_at`, `market`, `health`, `watchlist`, `candidates`, `candles`, `paper`, `runs`, `evaluation`. Health states: `ok`, `stale`, `failed`, `empty`, `degraded`, `not-collected`. UTC generation timestamps may end in `Z` or `+00:00` and are normalised to milliseconds and `Z` for acknowledgement and ordering. Candles are keyed by ticker with `time`, `open`, `high`, `low`, `close`, optional `volume`. Paper includes finite `capital`, `realised_balance`, `equity` and arrays `positions`, `closed_trades`, `events`. Additional fields are preserved in complete-record inspection and exports. The collector owns truthful source timestamps, price-unit conversion, strategy and simulation logic. The dashboard does not compute trade signals.

Optional `legacy.records` preserves imported historical CSV fields in an explicitly unverified History section and separate formula-safe CSV export. Source filtering and full record inspection never add those old outcomes to corrected paper evaluation.

## Review and operational limits

The graph uses completed candles with explicit units, prior-close/open markers when supplied and entry/stop/target lines for the selected setup. Exact candle and complete snapshot records remain inspectable. No data is discarded by the visual shortlist. Operational histories can grow; the upstream collector must bound each snapshot within 2 MiB while retaining complete durable evidence outside the display payload. D1 revision retention, backup/export cadence, long-term storage capacity and deletion policy must be agreed before an extended hosted run. No hosted integration, two-account access, data-provider latency or unattended schedule is claimed verified by the local tests.

## Replay and explanatory records

The replay uses frozen outputs of the actual Python engine, with fictional authored setups clearly distinguished from recorded observations and production signal discovery. Playback is isolated from current snapshots and never writes an operational journal. Decision journal CSV and daily report text follow the selected current, archived or replay snapshot. Replay snapshots cannot pass the ingest boundary. See [the replay and report guide](../docs/REPLAY_AND_REPORTS.md) for controls, recorded file generation and review acceptance.

`GET /api/export?table=journal` exports bounded explanatory entries. `GET /api/report` downloads the selected stored snapshot's canonical plain-text report; `?format=json` returns its structure and revision. Both support the existing `revision` parameter and private reader checks. Reports are absent, rather than fabricated, for old snapshots without the new fields.
