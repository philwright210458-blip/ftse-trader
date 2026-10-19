import test from "node:test";
import assert from "node:assert/strict";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { execFileSync, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { scansAPI, validateArchive, ScanD1Store } from "../scans-api.mjs";
const snapshot = JSON.parse(
  execFileSync(
    "python3",
    [
      "-c",
      'import json,publish_scans;print(json.dumps(publish_scans.build_snapshot("HEAD")))',
    ],
    // The real archive grows daily; allow output beyond Node's 1 MB default buffer.
    { cwd: new URL("../../", import.meta.url), encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
  ),
);
const env = {
  SCANS_WRITE_TOKEN: "a".repeat(64),
  SCANS_TRUST_SITES_DISPATCH: "true",
};
const req = (path, options = {}) =>
  new Request("https://private.example/api/scans/" + path, options);
const memory = () => ({
  row: null,
  async latest() {
    return this.row;
  },
  async put(row) {
    this.row = row;
    return "accepted";
  },
});
test("actual committed scanner archive validates and rejects synthetic/mislabelled archives", () => {
  assert.equal(validateArchive(snapshot), null);
  for (const mutate of [
    (s) => (s.synthetic = true),
    (s) => (s.runs[0].mode = "synthetic-demo"),
    (s) => (s.source.repository = "other/repo"),
    (s) => s.source.files[0].included_rows++,
    (s) => (s.source.last_scan_at = s.published_at),
    (s) => s.runs.reverse(),
    (s) => (s.setups[0]._source_file = "outcomes.csv"),
  ]) {
    const s = structuredClone(snapshot);
    mutate(s);
    assert.ok(validateArchive(s));
  }
});
test("private reads require trusted native identity; publisher token cannot read rows", async () => {
  const store = memory();
  assert.equal((await scansAPI(req("snapshot"), env, store)).status, 403);
  assert.equal(
    (
      await scansAPI(
        req("snapshot", {
          headers: { authorization: "Bearer " + env.SCANS_WRITE_TOKEN },
        }),
        env,
        store,
      )
    ).status,
    403,
  );
  assert.equal(
    (
      await scansAPI(
        req("snapshot", {
          headers: { "oai-authenticated-user-id": "visitor" },
        }),
        {},
        store,
      )
    ).status,
    403,
  );
  assert.equal(
    (
      await scansAPI(
        req("snapshot", {
          headers: { "oai-authenticated-user-id": "visitor" },
        }),
        env,
        store,
      )
    ).status,
    200,
  );
});
test("publisher authentication, payload validation, size cap and persisted receipt", async () => {
  const store = memory(),
    headers = {
      authorization: "Bearer " + env.SCANS_WRITE_TOKEN,
      "content-type": "application/json",
    };
  const post = (body) => req("ingest", { method: "POST", headers, body });
  assert.equal(
    (await scansAPI(req("ingest", { method: "POST", body: "{}" }), env, store))
      .status,
    403,
  );
  assert.equal((await scansAPI(post("{"), env, store)).status, 400);
  assert.equal(
    (await scansAPI(post(" ".repeat(2 * 1024 * 1024 + 1)), env, store)).status,
    413,
  );
  let response = await scansAPI(post(JSON.stringify(snapshot)), env, store);
  assert.equal(response.status, 200);
  const ack = await response.json();
  assert.equal(ack.status, "accepted");
  response = await scansAPI(req("status", { headers }), env, store);
  const status = await response.json();
  assert.equal(status.revision, ack.revision);
  assert.equal(status.commit_sha, snapshot.source.commit_sha);
  assert.equal(status.counts.setups, snapshot.setups.length);
  assert.equal(
    (await (await scansAPI(post(JSON.stringify(snapshot)), env, store)).json())
      .status,
    "duplicate",
  );
  response = await scansAPI(
    req("export?table=setups", {
      headers: { "oai-authenticated-user-id": "dad" },
    }),
    env,
    store,
  );
  assert.equal(response.status, 200);
  assert.match(await response.text(), /ticker/);
});
const python = `import sqlite3,json,sys
s=json.load(sys.stdin);c=sqlite3.connect(s['file']);c.row_factory=sqlite3.Row
if 'schema' in s:c.executescript(s['schema']);c.commit();print('{}')
elif 'sql' in s:
 r=c.execute(s['sql'],s.get('args',[])).fetchone();print(json.dumps(dict(r) if r else None))
else:
 c.execute('BEGIN')
 try:
  for q in s['statements']:c.execute(q['sql'],q['args'])
  c.commit();print('[]')
 except:c.rollback();raise
`;
test("production scan SQL keeps source ordering, rejects conflicting equal sequences and racing stale writers", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "ftse-scan-d1-"));
  try {
    const run = (input) => {
      const r = spawnSync("python3", ["-c", python], {
        input: JSON.stringify({ file: path.join(dir, "db"), ...input }),
        encoding: "utf8",
      });
      assert.equal(r.status, 0, r.stderr);
      return JSON.parse(r.stdout);
    };
    run({
      schema: await readFile(
        new URL("../scan-migrations/0000_scan_archive.sql", import.meta.url),
        "utf8",
      ),
    });
    const db = {
      prepare(sql) {
        return {
          sql,
          args: [],
          bind(...args) {
            return { sql, args, first: async () => run({ sql, args }) };
          },
          first: async () => run({ sql }),
        };
      },
      batch: async (statements) => run({ statements }),
    };
    const a = new ScanD1Store(db),
      b = new ScanD1Store(db);
    const row = (revision, seq, commit, clock) => ({
      revision,
      source_sequence: seq,
      commit_sha: commit,
      published_at: clock,
      received_at: clock,
      payload: "{}",
    });
    assert.equal(
      await a.put(row("a", 10, "x", "2026-10-17T10:00:00.000Z")),
      "accepted",
    );
    assert.equal(
      await b.put(row("b", 11, "y", "2026-10-17T10:01:00.000Z")),
      "accepted",
    );
    assert.equal(
      await a.put(row("old", 10, "x", "2026-10-17T10:02:00.000Z")),
      "stale",
    );
    assert.equal(
      await a.put(row("collision", 11, "z", "2026-10-17T10:03:00.000Z")),
      "conflict",
    );
    assert.equal(
      await a.put(row("same-time", 11, "y", "2026-10-17T10:01:00.000Z")),
      "stale",
    );
    assert.equal(
      await a.put(row("c", 11, "y", "2026-10-17T10:04:00.000Z")),
      "accepted",
    );
    assert.equal((await a.latest()).revision, "c");
    assert.equal(
      await a.put(row("a", 10, "x", "2026-10-17T10:00:00.000Z")),
      "stale",
    );
    assert.equal((await b.latest()).revision, "c");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("collected-scans build runs independently, keeps demo separate and blocks paper APIs", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "ftse-scans-worker-"));
  try {
    execFileSync(
      process.execPath,
      ["dashboard/scripts/build-scans-site.mjs", dir],
      { cwd: new URL("../../", import.meta.url) },
    );
    const source = await readFile(
      path.join(dir, "dist/server/index.js"),
      "utf8",
    );
    const { writeFile } = await import("node:fs/promises");
    await writeFile(path.join(dir, "worker.mjs"), source);
    const worker = (
      await import(new URL("file://" + path.join(dir, "worker.mjs")))
    ).default;
    const root = await worker.fetch(
      new Request("https://private.example/"),
      {},
    );
    assert.equal(root.status, 200);
    assert.match(await root.text(), /Collected scans/);
    assert.equal(
      (
        await worker.fetch(
          new Request("https://private.example/api/snapshot"),
          {},
        )
      ).status,
      404,
    );
    assert.equal(
      (
        await worker.fetch(
          new Request("https://private.example/api/scans/snapshot"),
          {},
        )
      ).status,
      403,
    );
    assert.equal(
      (await worker.fetch(new Request("https://private.example/demo"), {}))
        .status,
      302,
    );
    const demo = await worker.fetch(
      new Request("https://private.example/demo?demo=1"),
      {},
    );
    assert.match(await demo.text(), /Return to actual collected scans/);
    assert.match(
      await readFile(path.join(dir, "drizzle/0000_scan_archive.sql"), "utf8"),
      /CREATE TABLE scan_revisions/,
    );
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
