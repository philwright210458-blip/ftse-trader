import { json, digest, secureEqual, MAX_BODY, toCSV } from "./api.mjs";
const repo = "philwright210458-blip/ftse-trader";
const stamp = (value) =>
  typeof value === "string" &&
  Number.isFinite(Date.parse(value)) &&
  /(?:Z|[+-]\d\d:\d\d)$/.test(value);
export function validateArchive(s) {
  if (
    s?.schema_version !== 1 ||
    s.kind !== "scanner-archive" ||
    s.synthetic !== false
  )
    return "Expected an actual scanner archive.";
  if (
    !stamp(s.published_at) ||
    Date.parse(s.published_at) > Date.now() + 300000
  )
    return "Invalid publication time.";
  const source = s.source;
  if (
    source?.repository !== repo ||
    source.branch !== "main" ||
    !/^[a-f0-9]{40}$/.test(source.commit_sha) ||
    !Number.isSafeInteger(source.commit_sequence) ||
    source.commit_sequence < 1 ||
    !stamp(source.committed_at)
  )
    return "Invalid immutable source.";
  if (
    !Array.isArray(source.files) ||
    source.files.length < 2 ||
    source.files.length > 4 ||
    new Set(source.files.map((f) => f.name)).size !== source.files.length
  )
    return "Invalid file manifest.";
  const limits = { runs: 100, setups: 3000, outcomes: 4000 };
  for (const [key, limit] of Object.entries(limits)) {
    if (!Array.isArray(s[key]) || s[key].length > limit)
      return "Invalid record count.";
    for (const row of s[key]) {
      if (
        !row ||
        typeof row !== "object" ||
        Array.isArray(row) ||
        Object.keys(row).length > 85 ||
        Object.values(row).some(
          (v) => typeof v !== "string" || v.length > 16000,
        )
      )
        return "Invalid record.";
      if (
        ["mode", "source", "source_kind"].some((k) =>
          /demo|synthetic/i.test(row[k] || ""),
        )
      )
        return "Fictional records are not accepted.";
      const expected =
        key === "runs"
          ? ["runs.csv"]
          : key === "setups"
            ? ["setups.csv"]
            : ["outcomes.csv", "outcomes.v2.csv", "outcomes.v3.csv"];
      const required =
        key === "runs"
          ? [
              "day",
              "as_of",
              "mode",
              "run_time",
              "data_time",
              "scanned",
              "failed",
              "flagged",
            ]
          : key === "setups"
            ? [
                "day",
                "as_of",
                "mode",
                "run_time",
                "data_time",
                "company",
                "ticker",
                "setup",
                "direction",
                "score",
                "entry",
                "stop",
                "target",
              ]
            : ["day", "ticker", "direction", "horizon", "status", "outcome"];
      if (required.some((k) => typeof row[k] !== "string"))
        return "Missing record fields.";
      if (!source.files.some((f) => f.name === row._source_file))
        return "Record missing from file manifest.";
      if (!expected.includes(row._source_file)) return "Invalid source file.";
      if (key !== "outcomes" && !/^[a-f0-9]{32}$/.test(row._run_id))
        return "Missing run identity.";
      if (
        key === "runs" &&
        (!stamp(row._collected_at) ||
          !stamp(row.data_time) ||
          ["scanned", "failed", "flagged"].some((k) => !/^\d+$/.test(row[k])))
      )
        return "Invalid scan clock or counts.";
    }
  }
  for (const file of source.files) {
    if (
      !["runs.csv", "setups.csv", "outcomes.csv", "outcomes.v2.csv", "outcomes.v3.csv"].includes(
        file.name,
      ) ||
      !/^[a-f0-9]{64}$/.test(file.sha256) ||
      !Number.isSafeInteger(file.total_rows) ||
      file.total_rows < 0 ||
      !Number.isSafeInteger(file.included_rows) ||
      file.included_rows < 0 ||
      file.included_rows > file.total_rows ||
      file.truncated !== file.included_rows < file.total_rows
    )
      return "Invalid file coverage.";
    const rows =
      file.name === "runs.csv"
        ? s.runs
        : file.name === "setups.csv"
          ? s.setups
          : s.outcomes;
    if (
      rows.filter((r) => r._source_file === file.name).length !==
      file.included_rows
    )
      return "File coverage does not match records.";
  }
  if (
    !source.files.some((f) => f.name === "runs.csv") ||
    !source.files.some((f) => f.name === "setups.csv")
  )
    return "Required scanner files missing.";
  if (
    s.runs.some(
      (r, i) =>
        i > 0 &&
        Date.parse(r._collected_at) < Date.parse(s.runs[i - 1]._collected_at),
    )
  )
    return "Runs must be ordered.";
  const latest = s.runs.at(-1);
  if (
    source.last_scan_at !== (latest?._collected_at ?? null) ||
    source.last_data_time !== (latest?.data_time ?? null)
  )
    return "Latest scan clocks do not match records.";
  if (
    !s.upstream ||
    ![
      "success",
      "failure",
      "cancelled",
      "timed_out",
      "action_required",
      "neutral",
      "skipped",
      "stale",
      "startup_failure",
      "not-observed",
    ].includes(s.upstream.conclusion) ||
    typeof s.upstream.workflow !== "string" ||
    s.upstream.workflow.length > 200 ||
    (s.upstream.run_id !== null && !/^\d+$/.test(s.upstream.run_id))
  )
    return "Invalid upstream status.";
  return null;
}
export class ScanD1Store {
  constructor(db) {
    this.db = db;
  }
  latest() {
    return this.db
      .prepare(
        "SELECT r.* FROM scan_revisions r JOIN scan_current c ON c.revision=r.revision WHERE c.singleton=1",
      )
      .first();
  }
  async put(row) {
    // Both statements run in one transaction. A racing older publication cannot replace a newer source.
    await this.db.batch([
      this.db
        .prepare(
          `INSERT OR IGNORE INTO scan_revisions(revision,source_sequence,commit_sha,published_at,received_at,payload)
        SELECT ?,?,?,?,?,? WHERE NOT EXISTS (
          SELECT 1 FROM scan_revisions r JOIN scan_current c ON r.revision=c.revision
          WHERE r.source_sequence > ? OR (r.source_sequence = ? AND (r.commit_sha != ? OR r.published_at >= ?)))`,
        )
        .bind(
          row.revision,
          row.source_sequence,
          row.commit_sha,
          row.published_at,
          row.received_at,
          row.payload,
          row.source_sequence,
          row.source_sequence,
          row.commit_sha,
          row.published_at,
        ),
      this.db
        .prepare(
          `INSERT INTO scan_current(singleton,revision) SELECT 1,? WHERE EXISTS (SELECT 1 FROM scan_revisions WHERE revision=?)
        ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision WHERE
        (SELECT source_sequence FROM scan_revisions WHERE revision=excluded.revision) > (SELECT source_sequence FROM scan_revisions WHERE revision=scan_current.revision)
        OR ((SELECT source_sequence FROM scan_revisions WHERE revision=excluded.revision) = (SELECT source_sequence FROM scan_revisions WHERE revision=scan_current.revision)
        AND (SELECT commit_sha FROM scan_revisions WHERE revision=excluded.revision) = (SELECT commit_sha FROM scan_revisions WHERE revision=scan_current.revision)
        AND (SELECT published_at FROM scan_revisions WHERE revision=excluded.revision) > (SELECT published_at FROM scan_revisions WHERE revision=scan_current.revision))`,
        )
        .bind(row.revision, row.revision),
    ]);
    const current = await this.latest();
    if (current?.revision === row.revision) return "accepted";
    return current?.source_sequence === row.source_sequence &&
      current?.commit_sha !== row.commit_sha
      ? "conflict"
      : "stale";
  }
}
export async function scansAPI(request, env, store) {
  const url = new URL(request.url),
    path = url.pathname;
  const writer = await secureEqual(
    request.headers.get("authorization") || "",
    env.SCANS_WRITE_TOKEN?.length >= 32
      ? "Bearer " + env.SCANS_WRITE_TOKEN
      : "",
  );
  if (path === "/api/scans/ingest" || path === "/api/scans/status") {
    if (!writer)
      return json(
        { error: "A dedicated publisher credential is required." },
        403,
      );
    if (path.endsWith("/status")) {
      if (request.method !== "GET") return json({ error: "Use GET." }, 405);
      const row = await store.latest(),
        s = row ? JSON.parse(row.payload) : null;
      return json({
        revision: row?.revision ?? null,
        commit_sha: s?.source.commit_sha ?? null,
        counts: s
          ? {
              runs: s.runs.length,
              setups: s.setups.length,
              outcomes: s.outcomes.length,
            }
          : null,
      });
    }
    if (request.method !== "POST") return json({ error: "Use POST." }, 405);
    if (!request.headers.get("content-type")?.startsWith("application/json"))
      return json({ error: "Use JSON." }, 415);
    if (Number(request.headers.get("content-length")) > MAX_BODY)
      return json({ error: "Archive too large." }, 413);
    // Read the stream with a hard limit, including chunked requests without Content-Length.
    const reader = request.body?.getReader();
    if (!reader) return json({ error: "Missing archive." }, 400);
    const chunks = [];
    let length = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > MAX_BODY) {
        await reader.cancel();
        return json({ error: "Archive too large." }, 413);
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.length;
    }
    let s;
    try {
      s = JSON.parse(new TextDecoder().decode(bytes));
    } catch {
      return json({ error: "Invalid JSON." }, 400);
    }
    const error = validateArchive(s);
    if (error) return json({ error }, 400);
    const payload = JSON.stringify(s),
      revision = await digest(payload),
      current = await store.latest();
    if (current?.revision === revision)
      return json({ status: "duplicate", revision });
    const status = await store.put({
      revision,
      source_sequence: s.source.commit_sequence,
      commit_sha: s.source.commit_sha,
      published_at: new Date(s.published_at).toISOString(),
      received_at: new Date().toISOString(),
      payload,
    });
    return json(
      { status, revision },
      ["accepted", "duplicate"].includes(status) ? 200 : 409,
    );
  }
  // Native private Site dispatch authorises the shared audience and supplies visitor identity.
  // Local preview never accepts these headers unless explicitly enabled by its trusted adapter.
  if (
    env.SCANS_TRUST_SITES_DISPATCH !== "true" ||
    !request.headers.get("oai-authenticated-user-id")
  )
    return json({ error: "Sign in with an authorised ChatGPT account." }, 403);
  if (request.method !== "GET") return json({ error: "Use GET." }, 405);
  const row = await store.latest();
  if (path === "/api/scans/snapshot")
    return json({
      revision: row?.revision ?? null,
      snapshot: row ? JSON.parse(row.payload) : null,
    });
  if (path === "/api/scans/export") {
    const table = url.searchParams.get("table") || "setups";
    if (!["runs", "setups", "outcomes"].includes(table))
      return json({ error: "Unknown table." }, 400);
    if (!row) return json({ error: "No scans published yet." }, 404);
    return new Response(toCSV(JSON.parse(row.payload)[table]), {
      headers: {
        "content-type": "text/csv; charset=utf-8",
        "content-disposition": `attachment; filename="ftse-collected-${table}.csv"`,
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
      },
    });
  }
  return json({ error: "Not found." }, 404);
}
