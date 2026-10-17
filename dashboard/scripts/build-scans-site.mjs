import { readFile, writeFile, mkdir, cp, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const destination = path.resolve(
  process.argv[2] || path.join(root, "scan-site-build"),
);
const assets = {};
for (const file of await readdir(path.join(root, "public"))) {
  if (!/\.(html|js|mjs|json|css)$/.test(file)) continue;
  const ext = path.extname(file),
    type = {
      ".html": "text/html",
      ".js": "application/javascript",
      ".mjs": "application/javascript",
      ".json": "application/json",
      ".css": "text/css",
    }[ext];
  assets["/" + file] = {
    body: await readFile(path.join(root, "public", file), "utf8"),
    type: type + "; charset=utf-8",
  };
}
function stripImport(source, module) {
  const expression = new RegExp(
    "^import\\s+\\{[^}]+\\}\\s+from\\s+[\"']\\./" +
      module +
      "\\.mjs[\"'];?\\s*$",
    "m",
  );
  if (!expression.test(source))
    throw new Error("Expected module import: " + module);
  return source.replace(expression, "");
}
const api = await readFile(path.join(root, "api.mjs"), "utf8");
const scans = stripImport(
  await readFile(path.join(root, "scans-api.mjs"), "utf8"),
  "api",
);
assets["/index.html"].body = assets["/index.html"].body.replace(
  "<body>",
  '<body><p style="padding:12px 24px;margin:0;background:#fff4d6"><a href="/">← Return to actual collected scans</a> · This workspace uses fictional demo data.</p>',
);
const worker = stripImport(
  await readFile(path.join(root, "scans-worker.mjs"), "utf8"),
  "scans-api",
).replace("__SCAN_ASSETS__", () => JSON.stringify(assets));
await mkdir(path.join(destination, "dist/server"), { recursive: true });
await writeFile(
  path.join(destination, "dist/server/index.js"),
  api + "\n" + scans + "\n" + worker,
);
await cp(
  path.join(root, "scan-migrations"),
  path.join(destination, "drizzle"),
  { recursive: true },
);
console.log(
  "Built collected-scans Worker and separate schema. No deployment performed.",
);
