import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
  selectReplayFrame,
  validateReplayBundle,
  replayProvenance,
} from "../public/replay-ui.mjs";
import { recordsCSV, reportText } from "../public/insights-ui.mjs";

const bundle = JSON.parse(
  await readFile(new URL("../public/replay.json", import.meta.url), "utf8"),
);

test("engine-generated replay selects immutable frames and retains source disclosure", () => {
  assert.equal(validateReplayBundle(bundle), null);
  for (const scenario of bundle.scenarios) {
    const first = JSON.stringify(scenario.frames[0]);
    assert.equal(selectReplayFrame(scenario, -10).index, 0);
    assert.equal(selectReplayFrame(scenario, 100000).atEnd, true);
    assert.equal(selectReplayFrame(scenario, 1).index, 1);
    assert.equal(JSON.stringify(scenario.frames[0]), first);
    assert.match(replayProvenance(scenario), /replay/i);
  }
  assert.equal(selectReplayFrame({ frames: [] }, 0), null);
});

test("invalid bundles and future evidence are rejected before replacing a replay", () => {
  const wrong = structuredClone(bundle);
  wrong.kind = "live-trades";
  assert.ok(validateReplayBundle(wrong));
  const order = structuredClone(bundle);
  order.scenarios[0].frames[1].at = order.scenarios[0].frames[0].at;
  assert.ok(validateReplayBundle(order));
  const future = structuredClone(bundle);
  future.scenarios[0].frames[0].snapshot.daily_report.as_of =
    "2099-01-01T00:00:00Z";
  assert.ok(validateReplayBundle(future));
  const futureCandle = structuredClone(bundle);
  const selected = futureCandle.scenarios.find((s) =>
    s.frames.some((f) =>
      Object.values(f.snapshot.candles).some((rows) => rows.length),
    ),
  );
  const frame = selected.frames.find((f) =>
    Object.values(f.snapshot.candles).some((rows) => rows.length),
  );
  Object.values(frame.snapshot.candles).find(
    (rows) => rows.length,
  )[0].available_at = "2099-01-01T00:00:00Z";
  assert.ok(validateReplayBundle(futureCandle));
  assert.equal(validateReplayBundle(bundle), null);
});

test("selected report downloads retain canonical cost and provenance wording", () => {
  const report = bundle.scenarios[1].frames.at(-1).snapshot.daily_report;
  const output = reportText(report, "REPLAY · selected frame only");
  assert.ok(output.startsWith("REPLAY"));
  assert.ok(output.includes(report.text));
  const csv = recordsCSV([
    { ticker: '=HYPERLINK("bad")', pnl: -2.5, reason: "line one\nline two" },
  ]);
  assert.ok(csv.includes("'=HYPERLINK"));
  assert.ok(csv.includes('"-2.5"'));
  assert.ok(!csv.includes("'-2.5"));
  assert.ok(csv.includes("line one\nline two"));
});
