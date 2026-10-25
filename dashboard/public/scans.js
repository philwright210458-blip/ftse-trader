const $ = (id) => document.getElementById(id);
const date = (value) =>
  value
    ? new Intl.DateTimeFormat("en-GB", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "Europe/London",
      }).format(new Date(value)) + " UK"
    : "Not recorded";
const text = (tag, value) => {
  const element = document.createElement(tag);
  element.textContent = value ?? "—";
  return element;
};
let snapshot,
  revision,
  busy = false;
function row(values) {
  const tr = document.createElement("tr");
  for (const value of values) tr.append(text("td", value || "—"));
  return tr;
}
function renderSetups() {
  const run = snapshot.runs.find((r) => r._run_id === $("run").value),
    q = $("search").value.trim().toLowerCase(),
    direction = $("direction").value;
  const matches = snapshot.setups.filter(
    (s) =>
      s._run_id === run?._run_id &&
      (!q || `${s.company} ${s.ticker}`.toLowerCase().includes(q)) &&
      (!direction || s.direction.toUpperCase() === direction),
  );
  $("setups").replaceChildren(
    ...matches.slice(0, 500).map((s) => {
      const tr = row([
        s.company,
        s.setup,
        s.direction,
        s.score,
        s.entry,
        s.stop,
        s.target,
      ]);
      tr.firstChild.append(text("small", s.ticker));
      return tr;
    }),
  );
  $("matches").textContent =
    `${matches.length} matching setup records${matches.length > 500 ? " · showing first 500; download includes all retained rows" : ""}. Several setups can refer to the same share.`;
  $("run-detail").textContent = run
    ? `${run.mode === "full" ? "Full scan" : "Watch scan"} · ${run.scanned} shares scanned · ${run.failed} failed · ${run.flagged} flagged setup records · Candle time: ${date(run.data_time)}`
    : "No scan runs recorded.";
}
function render(s) {
  snapshot = s;
  const latest = s.runs.at(-1),
    age = latest
      ? Math.max(0, Date.now() - Date.parse(latest._collected_at))
      : null;
  $("health").textContent = latest
    ? `${age > 30 * 60000 ? "Historical records, not a current market view. " : ""}Latest collected scan: ${date(latest._collected_at)}. Source candles: ${date(latest.data_time)}. Published to this dashboard: ${date(s.published_at)}. These clocks describe different events.`
    : "No scan runs are recorded in the published archive.";
  $("summary").replaceChildren(
    ...[
      ["Latest scan", latest?.as_of || "—", latest?.day || "No runs"],
      ["Shares scanned", latest?.scanned || "0", "In the latest scan"],
      ["Setups flagged", latest?.flagged || "0", "Records, not unique shares"],
      ["Scan runs", s.runs.length, "Included in this archive"],
    ].map(([label, value, note]) => {
      const card = text("div", "");
      card.className = "card";
      card.append(text("small", label), text("b", value), text("small", note));
      return card;
    }),
  );
  const selected = $("run").value;
  $("run").replaceChildren(
    ...[...s.runs].reverse().map((r) => {
      const option = text(
        "option",
        `${r.day} ${r.as_of} · ${r.mode} · collected ${date(r._collected_at)}`,
      );
      option.value = r._run_id;
      return option;
    }),
  );
  if (s.runs.some((r) => r._run_id === selected)) $("run").value = selected;
  const source = s.source;
  $("provenance").replaceChildren();
  for (const [label, value] of [
    ["Repository", source.repository],
    ["Source commit", source.commit_sha],
    ["Committed", date(source.committed_at)],
    ["Last copied to Site", date(s.published_at)],
    ["Latest scan collection", date(source.last_scan_at)],
    ["Latest candle used", date(source.last_data_time)],
  ]) {
    $("provenance").append(text("dt", label));
    const dd = text("dd", value);
    if (label === "Source commit") {
      const a = text("a", value.slice(0, 12) + " · open source");
      a.href = `https://github.com/${source.repository}/commit/${source.commit_sha}`;
      a.target = "_blank";
      a.rel = "noopener";
      dd.replaceChildren(a);
    }
    $("provenance").append(dd);
  }
  $("upstream").textContent =
    `Last publication trigger: ${s.upstream.workflow} · ${s.upstream.conclusion === "not-observed" ? "manual archive copy; job status not checked" : s.upstream.conclusion}.`;
  if (!["success", "not-observed"].includes(s.upstream.conclusion))
    $("upstream").textContent +=
      " The source job did not complete successfully. These records may predate that attempt.";
  $("coverage").replaceChildren(
    ...source.files.map((f) =>
      row([
        f.name,
        `${f.included_rows}${f.truncated ? " (limited)" : ""}`,
        f.total_rows,
        f.name === "outcomes.csv"
          ? "Legacy outcomes, unverified"
          : ["outcomes.v2.csv", "outcomes.v3.csv"].includes(f.name)
            ? `Scanner outcomes ${f.name.split(".")[1]}; separate model`
            : "Actual scanner observations",
      ]),
    ),
  );
  $("outcomes").replaceChildren(
    ...s.outcomes
      .slice(-100)
      .reverse()
      .map((o) =>
        row([
          o.day,
          o.ticker,
          o.direction,
          o.horizon,
          o.status,
          o.outcome,
          o._source_file,
        ]),
      ),
  );
  renderSetups();
  $("content").hidden = false;
}
async function refresh() {
  if (busy) return;
  busy = true;
  $("refresh").disabled = true;
  try {
    const response = await fetch("/api/scans/snapshot", {
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok)
      throw new Error(
        response.status === 403
          ? "Sign in with one of the authorised ChatGPT accounts to see collected scans."
          : "The scan service is unavailable. Previously shown records may be out of date.",
      );
    const body = await response.json();
    if (!body.snapshot) {
      $("message").textContent =
        "No collected scans have been published yet. The fictional demo is available separately.";
      return;
    }
    {
      render(body.snapshot);
      revision = body.revision;
    }
    $("message").textContent =
      `Connected to stored scanner records · Checked ${date(new Date().toISOString())}.`;
  } catch (error) {
    $("message").textContent =
      error.name === "TimeoutError"
        ? "The request timed out. Previously shown records may be out of date."
        : error.message;
  } finally {
    busy = false;
    $("refresh").disabled = false;
  }
}
$("refresh").addEventListener("click", refresh);
for (const id of ["run", "search", "direction"])
  $(id).addEventListener(
    id === "search" ? "input" : "change",
    () => snapshot && renderSetups(),
  );
setInterval(() => {
  if (!document.hidden) refresh();
}, 60000);
refresh();
