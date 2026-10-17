import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

test("built Worker loads, embeds assets and retains private reader checks", async () => {
  const script = fileURLToPath(new URL("../scripts/build.mjs", import.meta.url));
  execFileSync(process.execPath, [script], { stdio: "pipe" });
  const output = new URL("../dist/server/index.js", import.meta.url);
  execFileSync(process.execPath, ["--check", fileURLToPath(output)]);
  const source = await readFile(output, "utf8");
  assert.doesNotMatch(source, /__EMBEDDED_ASSETS__|DEMO-FALL/);
  const { default: worker } = await import(output.href);
  const denied = await worker.fetch(new Request("https://research.example/"), {});
  assert.equal(denied.status, 403);
  const preview = await worker.fetch(new Request("https://research.example/"), {
    LOCAL_PREVIEW: true,
  });
  assert.equal(preview.status, 200);
  assert.match(await preview.text(), /FTSE Research/);
  const missing = await worker.fetch(new Request("https://research.example/no-file"), {
    LOCAL_PREVIEW: true,
  });
  assert.equal(missing.status, 404);
});
