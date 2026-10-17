import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createServer } from "../server.mjs";
import { validSnapshot } from "../api.mjs";

const example = JSON.parse(
  await readFile(new URL("../public/demo.json", import.meta.url), "utf8"),
).snapshot;

test("report and journal exports follow the chosen immutable snapshot revision", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "ftse-insights-"));
  const server = createServer({ directory: dir, token: "local-test-only" });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const post = (value) =>
    fetch(origin + "/api/ingest", {
      method: "POST",
      headers: {
        authorization: "Bearer local-test-only",
        "content-type": "application/json",
      },
      body: JSON.stringify(value),
    });
  try {
    assert.equal((await fetch(origin + "/api/report")).status, 404);
    const first = structuredClone(example);
    first.generated_at = new Date(Date.now() - 120000).toISOString();
    first.daily_report.text = "First report: no later-session results";
    const ack = await (await post(first)).json();
    const second = structuredClone(first);
    second.generated_at = new Date(Date.now() - 60000).toISOString();
    second.daily_report.text = "Second report";
    assert.equal((await post(second)).status, 201);
    const saved = await fetch(origin + "/api/report?revision=" + ack.revision);
    assert.match(saved.headers.get("content-disposition"), /DEMONSTRATION/);
    assert.equal(await saved.text(), first.daily_report.text);
    assert.equal(
      await (await fetch(origin + "/api/report")).text(),
      "Second report",
    );
    const json = await (
      await fetch(origin + "/api/report?format=json&revision=" + ack.revision)
    ).json();
    assert.equal(json.revision, ack.revision);
    const csv = await (
      await fetch(origin + "/api/export?table=journal&revision=" + ack.revision)
    ).text();
    assert.ok(csv.includes("explanation"));
    assert.equal(
      (await fetch(origin + "/api/report?revision=bad")).status,
      400,
    );
    assert.equal((await fetch(origin + "/api/report?format=html")).status, 400);
    const replay = structuredClone(first);
    replay.replay = { is_replay: true };
    assert.equal((await post(replay)).status, 422);
    assert.equal((await fetch(origin + "/replay.json")).status, 200);
    assert.equal((await fetch(origin + "/replay-ui.mjs")).status, 200);
    assert.equal(
      (await fetch(origin + "/../ftse_research_cli.py")).status,
      404,
    );
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(dir, { recursive: true, force: true });
  }
});

test("malformed explanations are rejected without breaking older snapshot compatibility", () => {
  assert.equal(validSnapshot(example), null);
  const bad = structuredClone(example);
  bad.daily_report.text = {};
  assert.match(validSnapshot(bad), /daily report/i);
  const broken = structuredClone(example);
  broken.decision_journal.entries[0].explanation = {};
  assert.match(validSnapshot(broken), /journal/i);
  const old = structuredClone(example);
  delete old.decision_journal;
  delete old.daily_report;
  assert.equal(validSnapshot(old), null);
});
