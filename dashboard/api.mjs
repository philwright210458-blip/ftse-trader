export const MAX_BODY = 2 * 1024 * 1024;
export const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
const canonical = (value) =>
  Array.isArray(value)
    ? value.map(canonical)
    : value && typeof value === "object"
      ? Object.fromEntries(
          Object.keys(value)
            .sort()
            .map((k) => [k, canonical(value[k])]),
        )
      : value;
export async function digest(value) {
  return Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
    ),
  )
    .map((v) => v.toString(16).padStart(2, "0"))
    .join("");
}
export async function secureEqual(a, b) {
  if (!a || !b) return false;
  const [x, y] = await Promise.all([digest(a), digest(b)]);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}
export function validSnapshot(s) {
  if (s?.replay !== undefined)
    return "Replay frames cannot be ingested as current research snapshots.";
  if (
    !s ||
    s.schema_version !== 1 ||
    typeof s.generated_at !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T.*(?:Z|\+00:00)$/.test(s.generated_at || "") ||
    !Number.isFinite(Date.parse(s.generated_at))
  )
    return "Expected schema_version 1 and generated_at in UTC.";
  if (
    s.last_success_at !== null &&
    s.last_success_at !== undefined &&
    (typeof s.last_success_at !== "string" ||
      !Number.isFinite(Date.parse(s.last_success_at)))
  )
    return "Invalid last successful collection timestamp.";
  if (Date.parse(s.generated_at) > Date.now() + 5 * 60000)
    return "Snapshot timestamp is in the future.";
  if (
    !s.market ||
    !s.health ||
    !["ok", "stale", "failed", "empty", "degraded", "not-collected"].includes(
      s.health.state,
    )
  )
    return "Missing market or valid health state.";
  for (const k of ["watchlist", "candidates", "runs"])
    if (
      !Array.isArray(s[k]) ||
      s[k].some((r) => !r || typeof r !== "object" || Array.isArray(r))
    )
      return `Expected ${k} record array.`;
  if (
    !s.candles ||
    typeof s.candles !== "object" ||
    Array.isArray(s.candles) ||
    !s.paper ||
    !s.evaluation
  )
    return "Missing candles, paper or evaluation.";
  for (const k of ["positions", "closed_trades", "events"])
    if (
      !Array.isArray(s.paper[k]) ||
      s.paper[k].some((r) => !r || typeof r !== "object" || Array.isArray(r))
    )
      return `Expected paper.${k} record array.`;
  for (const k of ["capital", "realised_balance", "equity"])
    if (typeof s.paper[k] !== "number" || !Number.isFinite(s.paper[k]))
      return `Expected finite paper.${k}.`;
  if (
    s.legacy !== undefined &&
    (!s.legacy ||
      !Array.isArray(s.legacy.records) ||
      s.legacy.records.some(
        (r) => !r || typeof r !== "object" || Array.isArray(r),
      ))
  )
    return "Invalid legacy record array.";
  if (s.decision_journal !== undefined) {
    const journal = s.decision_journal;
    if (
      !journal ||
      !Array.isArray(journal.entries) ||
      journal.entries.some(
        (entry) =>
          !entry ||
          typeof entry !== "object" ||
          Array.isArray(entry) ||
          ["id", "at", "ticker", "kind", "title", "explanation", "status"].some(
            (key) =>
              entry[key] !== undefined &&
              entry[key] !== null &&
              typeof entry[key] !== "string",
          ),
      )
    ) {
      return "Invalid decision journal.";
    }
  }
  if (s.daily_report !== undefined) {
    const report = s.daily_report;
    if (
      !report ||
      typeof report !== "object" ||
      Array.isArray(report) ||
      ["session", "as_of", "mode", "status", "headline", "text"].some(
        (key) =>
          report[key] !== undefined &&
          report[key] !== null &&
          typeof report[key] !== "string",
      ) ||
      ["summary", "limitations"].some(
        (key) =>
          report[key] !== undefined &&
          (!Array.isArray(report[key]) ||
            report[key].some((value) => typeof value !== "string")),
      )
    ) {
      return "Invalid daily report.";
    }
  }
  const scalar = (record, keys, type) =>
    keys.every(
      (k) =>
        record[k] === undefined ||
        record[k] === null ||
        typeof record[k] === type,
    );
  if (
    !scalar(
      s.market,
      ["session", "status", "open", "close", "timezone"],
      "string",
    ) ||
    !scalar(s.health, ["last_error"], "string")
  )
    return "Invalid market or health display fields.";
  const strings = [
    "ticker",
    "company",
    "name",
    "quote_unit",
    "status",
    "reason",
    "direction",
    "strategy",
    "previous_session",
    "last_candle_at",
    "available_at",
    "signal_at",
    "entry_at",
    "exit_at",
  ];
  const numbers = [
    "previous_change_pct",
    "opening_gap_pct",
    "intraday_change_pct",
    "last_price",
    "previous_close",
    "opening_price",
    "entry",
    "stop",
    "target",
    "score",
    "quantity",
    "entry_price",
    "exit_price",
    "pnl",
    "gross_pnl",
    "costs",
  ];
  for (const rows of [
    s.watchlist,
    s.candidates,
    s.paper.positions,
    s.paper.closed_trades,
  ])
    for (const r of rows) {
      if (
        typeof r.ticker !== "string" ||
        !r.ticker.trim() ||
        !scalar(r, strings, "string") ||
        !scalar(r, numbers, "number") ||
        numbers.some((k) => typeof r[k] === "number" && !Number.isFinite(r[k]))
      )
        return "Invalid share or trade display fields.";
    }
  if (
    !scalar(
      s.evaluation,
      ["label", "status", "assessment", "criteria_status"],
      "string",
    ) ||
    (s.evaluation.limitations !== undefined &&
      (!Array.isArray(s.evaluation.limitations) ||
        s.evaluation.limitations.some((v) => typeof v !== "string")))
  )
    return "Invalid evaluation display fields.";
  if (
    s.evaluation.sample_counts &&
    Object.values(s.evaluation.sample_counts).some(
      (v) => typeof v !== "number" && typeof v !== "boolean",
    )
  )
    return "Invalid evaluation counts.";
  if (
    s.health.expected_interval_minutes !== undefined &&
    (typeof s.health.expected_interval_minutes !== "number" ||
      !Number.isFinite(s.health.expected_interval_minutes) ||
      s.health.expected_interval_minutes <= 0)
  )
    return "Invalid collection interval.";
  if (
    s.coverage?.complete_journal_counts &&
    Object.values(s.coverage.complete_journal_counts).some(
      (v) => typeof v !== "number" || !Number.isFinite(v),
    )
  )
    return "Invalid coverage counts.";
  for (const [ticker, rows] of Object.entries(s.candles)) {
    if (!Array.isArray(rows)) return `Invalid candles for ${ticker}.`;
    for (const r of rows)
      if (
        !r ||
        typeof r.time !== "string" ||
        !Number.isFinite(Date.parse(r.time)) ||
        !["open", "high", "low", "close"].every(
          (k) => typeof r[k] === "number" && Number.isFinite(r[k]),
        ) ||
        r.high < Math.max(r.open, r.close, r.low) ||
        r.low > Math.min(r.open, r.close, r.high)
      )
        return `Invalid candle for ${ticker}.`;
  }
  return null;
}
export function canRead(request, env) {
  if (env.LOCAL_PREVIEW === true) return true;
  // These headers are trustworthy only behind Sites' private authenticated dispatch.
  if (env.DEPLOYMENT_MODE !== "sites" || env.TRUST_SITES_DISPATCH !== "true")
    return false;
  const user = request.headers.get("oai-authenticated-user-id");
  const allow = (env.READER_USER_IDS || "")
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
  return Boolean(user && allow.includes(user));
}
export function csvCell(value) {
  const raw =
    typeof value === "object" && value !== null
      ? JSON.stringify(value)
      : String(value ?? "");
  const safe =
    typeof value === "string" && /^[\s]*[=+@-]/.test(raw) ? "'" + raw : raw;
  return '"' + safe.replaceAll('"', '""') + '"';
}
export function toCSV(rows) {
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  return (
    "\ufeff" +
    [
      keys.map(csvCell).join(","),
      ...rows.map((r) => keys.map((k) => csvCell(r[k])).join(",")),
    ].join("\r\n")
  );
}
export async function handleAPI(request, env, store) {
  const url = new URL(request.url);
  if (url.pathname === "/api/ingest-status") {
    if (request.method !== "GET") return json({ error: "Use GET." }, 405);
    if (
      !(await secureEqual(
        request.headers.get("authorization"),
        env.INGEST_TOKEN ? "Bearer " + env.INGEST_TOKEN : null,
      ))
    )
      return json({ error: "Update authentication required." }, 401);
    const row = await store.latest();
    return json({
      revision: row?.revision || null,
      generated_at: row?.generated_at || null,
    });
  }
  if (url.pathname === "/api/ingest") {
    if (request.method !== "POST") return json({ error: "Use POST." }, 405);
    if (
      !(await secureEqual(
        request.headers.get("authorization"),
        env.INGEST_TOKEN ? "Bearer " + env.INGEST_TOKEN : null,
      ))
    )
      return json({ error: "Update authentication required." }, 401);
    if (!request.headers.get("content-type")?.startsWith("application/json"))
      return json({ error: "Expected application/json." }, 415);
    if (Number(request.headers.get("content-length")) > MAX_BODY)
      return json({ error: "Snapshot too large." }, 413);
    const reader = request.body?.getReader();
    let bytes = 0;
    const chunks = [];
    if (reader) {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        bytes += value.length;
        if (bytes > MAX_BODY) {
          await reader.cancel();
          return json({ error: "Snapshot too large." }, 413);
        }
        chunks.push(value);
      }
    }
    let snapshot;
    try {
      const all = new Uint8Array(bytes);
      let at = 0;
      for (const c of chunks) {
        all.set(c, at);
        at += c.length;
      }
      snapshot = JSON.parse(new TextDecoder().decode(all));
    } catch {
      return json({ error: "Invalid JSON." }, 400);
    }
    const error = validSnapshot(snapshot);
    if (error) return json({ error }, 422);
    snapshot.generated_at = new Date(snapshot.generated_at).toISOString();
    const payload = JSON.stringify(canonical(snapshot));
    const revision = await digest(payload);
    const result = await store.put({
      revision,
      generated_at: snapshot.generated_at,
      received_at: new Date().toISOString(),
      payload,
    });
    return json(
      { status: result, revision, generated_at: snapshot.generated_at },
      result === "stale" || result === "conflict"
        ? 409
        : result === "accepted"
          ? 201
          : 200,
    );
  }
  if (!canRead(request, env))
    return json(
      {
        error:
          "This private dashboard requires an authorised signed-in reader.",
      },
      403,
    );
  if (request.method !== "GET") return json({ error: "Use GET." }, 405);
  if (url.pathname === "/api/snapshot") {
    const row = await store.latest();
    return json(
      row
        ? { revision: row.revision, snapshot: JSON.parse(row.payload) }
        : { revision: null, snapshot: null },
    );
  }
  if (url.pathname === "/api/history") {
    const revision = url.searchParams.get("revision");
    if (revision) {
      if (!/^[a-f0-9]{64}$/.test(revision))
        return json({ error: "Invalid revision." }, 400);
      const row = await store.byRevision(revision);
      return row
        ? json({ revision: row.revision, snapshot: JSON.parse(row.payload) })
        : json({ error: "Revision not found." }, 404);
    }
    const requested = Number(url.searchParams.get("limit") || 20),
      limit = Math.min(
        100,
        Math.max(1, Number.isFinite(requested) ? Math.floor(requested) : 20),
      ),
      before = url.searchParams.get("before");
    if (before && !Number.isFinite(Date.parse(before)))
      return json({ error: "Invalid history cursor." }, 400);
    const rows = await store.history(
      before ? new Date(before).toISOString() : null,
      limit + 1,
    );
    const revisions = rows.slice(0, limit);
    return json({
      revisions,
      next_before: rows.length > limit ? revisions.at(-1).generated_at : null,
    });
  }
  if (url.pathname === "/api/export") {
    const requestedRevision = url.searchParams.get("revision");
    if (requestedRevision && !/^[a-f0-9]{64}$/.test(requestedRevision))
      return json({ error: "Invalid revision." }, 400);
    const row = requestedRevision
      ? await store.byRevision(requestedRevision)
      : await store.latest();
    const s = row ? JSON.parse(row.payload) : null;
    if (!s) return json({ error: "No snapshot has been received." }, 404);
    const table = url.searchParams.get("table") || "watchlist";
    const tables = {
      watchlist: s.watchlist,
      candidates: s.candidates,
      runs: s.runs,
      positions: s.paper.positions,
      closed_trades: s.paper.closed_trades,
      events: s.paper.events,
      legacy: s.legacy?.records || [],
      journal: s.decision_journal?.entries || [],
    };
    if (!Object.hasOwn(tables, table))
      return json({ error: "Unknown export table." }, 400);
    return new Response(toCSV(tables[table]), {
      headers: {
        "content-type": "text/csv; charset=utf-8",
        "content-disposition": `attachment; filename="ftse-${s.mode === "synthetic-demo" ? "DEMONSTRATION-" : ""}${table}.csv"`,
        "cache-control": "no-store",
      },
    });
  }
  if (url.pathname === "/api/report") {
    const requestedRevision = url.searchParams.get("revision");
    if (requestedRevision && !/^[a-f0-9]{64}$/.test(requestedRevision))
      return json({ error: "Invalid revision." }, 400);
    const row = requestedRevision
      ? await store.byRevision(requestedRevision)
      : await store.latest();
    const snapshot = row ? JSON.parse(row.payload) : null;
    const report = snapshot?.daily_report;
    if (!report)
      return json(
        { error: "No daily report is available for this snapshot." },
        404,
      );
    const format = url.searchParams.get("format") || "text";
    if (format === "json") return json({ revision: row.revision, report });
    if (format !== "text")
      return json({ error: "Use text or json report format." }, 400);
    if (typeof report.text !== "string")
      return json({ error: "This report has no text version." }, 404);
    const date = /^\d{4}-\d{2}-\d{2}$/.test(report.session)
      ? report.session
      : "session";
    return new Response(report.text, {
      headers: {
        "content-type": "text/plain; charset=utf-8",
        "content-disposition": `attachment; filename="ftse-${snapshot.mode === "synthetic-demo" ? "DEMONSTRATION-" : ""}daily-report-${date}.txt"`,
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
      },
    });
  }
  return json({ error: "Not found." }, 404);
}
export class D1Store {
  constructor(db) {
    this.db = db;
  }
  latest() {
    return this.db
      .prepare(
        "SELECT r.* FROM snapshot_revisions r JOIN snapshot_current c ON c.revision=r.revision WHERE c.singleton=1",
      )
      .first();
  }
  byRevision(revision) {
    return this.db
      .prepare("SELECT * FROM snapshot_revisions WHERE revision=?")
      .bind(revision)
      .first();
  }
  async history(before, limit) {
    const query = before
      ? this.db
          .prepare(
            "SELECT revision,generated_at,received_at FROM snapshot_revisions WHERE generated_at < ? ORDER BY generated_at DESC LIMIT ?",
          )
          .bind(before, limit)
      : this.db
          .prepare(
            "SELECT revision,generated_at,received_at FROM snapshot_revisions ORDER BY generated_at DESC LIMIT ?",
          )
          .bind(limit);
    return (await query.all()).results;
  }
  async put(row) {
    const old = await this.latest();
    if (old && old.generated_at > row.generated_at) return "stale";
    if (old && old.generated_at === row.generated_at)
      return old.revision === row.revision ? "duplicate" : "conflict";
    const results = await this.db.batch([
      this.db
        .prepare(
          "INSERT OR IGNORE INTO snapshot_revisions(revision,generated_at,received_at,payload) SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM snapshot_current WHERE generated_at >= ?)",
        )
        .bind(
          row.revision,
          row.generated_at,
          row.received_at,
          row.payload,
          row.generated_at,
        ),
      this.db
        .prepare(
          "INSERT INTO snapshot_current(singleton,revision,generated_at) SELECT 1,revision,generated_at FROM snapshot_revisions WHERE revision=? ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,generated_at=excluded.generated_at WHERE excluded.generated_at>snapshot_current.generated_at",
        )
        .bind(row.revision),
    ]);
    if (results[0].meta.changes) return "accepted";
    const current = await this.latest();
    return current.revision === row.revision
      ? "duplicate"
      : current.generated_at === row.generated_at
        ? "conflict"
        : "stale";
  }
}
