import { readFile, writeFile, mkdir, cp } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), ".."),
  assets = {};
for (const [file, type] of [
  ["index.html", "text/html"],
  ["app.js", "application/javascript"],
  ["presentation.mjs", "application/javascript"],
  ["style.css", "text/css"],
])
  assets["/" + file] = {
    body: await readFile(path.join(root, "public", file), "utf8"),
    type: type + "; charset=utf-8",
  };
const api = await readFile(path.join(root, "api.mjs"), "utf8");
const workerSource = await readFile(path.join(root, "worker.mjs"), "utf8");
const apiImport = /^import\s+\{[^}]+\}\s+from\s+["']\.\/api\.mjs["'];?\s*$/m;
if (!apiImport.test(workerSource)) {
  throw new Error("Expected the Worker API import before embedding source.");
}
const worker = workerSource
  .replace(apiImport, "")
  .replace("__EMBEDDED_ASSETS__", JSON.stringify(assets));
await mkdir(path.join(root, "dist", "server"), { recursive: true });
await writeFile(
  path.join(root, "dist", "server", "index.js"),
  api + "\n" + worker,
);
await cp(path.join(root, "migrations"), path.join(root, "dist", "migrations"), {
  recursive: true,
});
try {
  const manifest = await readFile(
    path.join(root, ".openai", "hosting.json"),
    "utf8",
  );
  if (!JSON.parse(manifest).project_id)
    throw new Error("Registered project_id required.");
  await mkdir(path.join(root, "dist", ".openai"), { recursive: true });
  await writeFile(path.join(root, "dist", ".openai", "hosting.json"), manifest);
} catch (e) {
  if (e.code === "ENOENT")
    console.log(
      "Source build only: no registered Sites manifest exists. Nothing has been deployed.",
    );
  else throw e;
}
console.log(
  "Built dashboard/dist/server/index.js with embedded assets and schema migrations.",
);
