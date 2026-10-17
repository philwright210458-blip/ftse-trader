import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { D1Store } from "../api.mjs";
// Exercise production SQL against real SQLite, rather than a query-string mock.
const python = `import sqlite3,json,sys
d=json.load(sys.stdin)
c=sqlite3.connect(d['file']);c.row_factory=sqlite3.Row
if 'schema' in d:
 c.executescript(d['schema']);c.commit();print('{}')
elif d.get('all'):
 rows=c.execute(d['sql'],d['args']).fetchall();print(json.dumps({'results':[dict(r) for r in rows]}))
elif d.get('first'):
 r=c.execute(d['sql'],d['args']).fetchone();print(json.dumps(dict(r) if r else None))
else:
 c.execute('BEGIN');out=[]
 try:
  for s in d['statements']:
   q=c.execute(s['sql'],s['args']);out.append({'meta':{'changes':q.rowcount}})
  c.commit();print(json.dumps(out))
 except:
  c.rollback();raise
`;
test("D1 SQL keeps immutable revisions, rejects stale/conflicting racing writers and advances atomically", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "ftse-d1-"));
  const file = path.join(dir, "db.sqlite");
  const run = (input) => {
    const result = spawnSync("python3", ["-c", python], {
      input: JSON.stringify({ file, ...input }),
      encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
  };
  try {
    run({
      schema: await readFile(
        new URL("../migrations/0001_snapshots.sql", import.meta.url),
        "utf8",
      ),
    });
    const db = {
      prepare(sql) {
        const q = {
          sql,
          args: [],
          bind(...args) {
            return {
              ...q,
              args,
              first: async () => run({ sql, args, first: true }),
              all: async () => run({ sql, args, all: true }),
            };
          },
          async first() {
            return run({ sql, args: [], first: true });
          },
          async all() {
            return run({ sql, args: [], all: true });
          },
        };
        return q;
      },
      async batch(statements) {
        return run({ statements });
      },
    };
    const store = new D1Store(db),
      row = (revision, generated_at) => ({
        revision,
        generated_at,
        received_at: generated_at,
        payload: JSON.stringify({ revision }),
      });
    assert.equal(
      await store.put(row("a", "2026-10-16T08:00:00.000Z")),
      "accepted",
    );
    assert.equal(
      await store.put(row("a", "2026-10-16T08:00:00.000Z")),
      "duplicate",
    );
    assert.equal(
      await store.put(row("b", "2026-10-16T08:00:00.000Z")),
      "conflict",
    );
    assert.equal(
      await store.put(row("old", "2026-10-15T08:00:00.000Z")),
      "stale",
    );
    const results = await Promise.all([
      store.put(row("latest", "2026-10-16T10:00:00.000Z")),
      store.put(row("middle", "2026-10-16T09:00:00.000Z")),
    ]);
    assert.deepEqual(results, ["accepted", "stale"]);
    assert.equal((await store.latest()).revision, "latest");
    assert.equal((await store.byRevision("a")).revision, "a");
    assert.equal((await store.history(null, 1))[0].revision, "latest");
    assert.equal(
      (await store.history("2026-10-16T10:00:00.000Z", 5))[0].revision,
      "a",
    );
    assert.deepEqual(
      run({
        first: true,
        sql: "SELECT COUNT(*) AS count FROM snapshot_revisions",
        args: [],
      }),
      { count: 2 },
    );
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
