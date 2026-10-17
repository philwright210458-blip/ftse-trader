import { researchProvenance } from "./presentation.mjs";
import { mountReplay, replayProvenance } from "./replay-ui.mjs";
import { recordsCSV, reportText, downloadText } from "./insights-ui.mjs";
const $ = (id) => document.getElementById(id);
let replayContext = null,
  beforeReplay = null;
let snapshot = null,
  chosen = "",
  revision = null,
  archived = false,
  archiveCursor = null,
  archiveRows = [];
const demo = new URLSearchParams(location.search).get("demo") === "1";
const text = (tag, value, cls) => {
  const e = document.createElement(tag);
  e.textContent = value ?? "Unavailable";
  if (cls) e.className = cls;
  return e;
};
const number = (v, d = 2) =>
  typeof v === "number" && Number.isFinite(v)
    ? v.toLocaleString("en-GB", {
        maximumFractionDigits: d,
        minimumFractionDigits: d,
      })
    : "Unavailable";
const pct = (v) =>
  typeof v === "number" ? `${v > 0 ? "+" : ""}${number(v)}%` : "Unavailable";
const money = (v) => (typeof v === "number" ? `£${number(v)}` : "Unavailable");
const time = (v) =>
  v && Number.isFinite(Date.parse(v))
    ? new Date(v).toLocaleString("en-GB", {
        timeZone: "Europe/London",
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Unavailable";
const empty = (el, message) =>
  el.replaceChildren(text("div", message, "empty"));
function metric(label, value, note) {
  const e = text("div", "", "metric");
  e.append(
    text("span", label, "label"),
    text("span", value, "value"),
    text("span", note, "note"),
  );
  return e;
}
function badge(value) {
  return text(
    "span",
    value || "undecided",
    `badge ${["reversal", "continuation", "unavailable"].includes(value) ? value : ""}`,
  );
}
function shareButton(row) {
  const box = document.createElement("div"),
    button = text("button", row.ticker, "ticker-link");
  button.type = "button";
  button.addEventListener("click", () => {
    chosen = row.ticker;
    location.hash = "detail";
    renderDetail();
  });
  box.append(button, text("span", row.company || row.name || "", "company"));
  return box;
}
function table(el, rows, cols, message = "No records available.") {
  if (!rows.length) {
    empty(el, message);
    return;
  }
  const wrap = text("div", "", "table-wrap"),
    t = document.createElement("table"),
    head = document.createElement("thead"),
    tr = document.createElement("tr");
  for (const c of cols) {
    const th = text("th", c.label);
    th.scope = "col";
    tr.append(th);
  }
  head.append(tr);
  t.append(head);
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const c of cols) {
      const td = document.createElement("td"),
        value = c.render ? c.render(row) : row[c.key];
      if (value instanceof Node) td.append(value);
      else
        td.textContent =
          value === null || value === undefined
            ? "Unavailable"
            : typeof value === "object"
              ? JSON.stringify(value)
              : String(value);
      tr.append(td);
    }
    body.append(tr);
  }
  t.append(body);
  wrap.append(t);
  el.replaceChildren(wrap);
}
function generic(el, rows, message) {
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  table(
    el,
    rows,
    keys.map((k) => ({ label: k.replaceAll("_", " "), key: k })),
    message,
  );
}
const descriptions = {
  replay: [
    "Session replay",
    "Explore frozen evidence at your own pace, without changing the paper ledger.",
  ],
  journal: [
    "Decision journal",
    "Understand what was known, why a setup qualified and why the system waited.",
  ],
  report: [
    "Daily report",
    "A readable account of the selected session, decisions and data quality.",
  ],
  watch: [
    "Morning watch",
    "Follow yesterday’s biggest movers from the opening bell.",
  ],
  detail: [
    "Share detail",
    "Inspect the candles, timing and reasons behind a setup.",
  ],
  portfolio: [
    "Paper portfolio",
    "Track simulated exposure and the evidence behind results.",
  ],
  history: [
    "History & exports",
    "Keep every scan, decision and outcome available for inspection.",
  ],
};
function route() {
  const view = Object.hasOwn(descriptions, location.hash.slice(1))
    ? location.hash.slice(1)
    : "watch";
  for (const k of Object.keys(descriptions)) $(k + "-view").hidden = k !== view;
  document.querySelectorAll("nav a").forEach((a) => {
    const current = a.dataset.view === view;
    a.classList.toggle("active", current);
    if (current) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  $("page-title").textContent = descriptions[view][0];
  $("page-description").textContent = descriptions[view][1];
  document.title = "FTSE Research | " + descriptions[view][0];
  if (view !== "replay") replayController.pause();
  if (view === "detail") renderDetail();
}
function freshness() {
  const el = $("data-status");
  $("return-latest").hidden = !archived || Boolean(replayContext);
  $("refresh").hidden = archived || Boolean(replayContext);
  if (!snapshot) {
    el.className = "status warning";
    el.textContent =
      "No data received. This dashboard is ready for its first research snapshot. No prices or performance are being inferred.";
    return;
  }
  const s = snapshot,
    age =
      ((replayContext ? Date.parse(replayContext.at) : Date.now()) -
        Date.parse(s.last_success_at || s.generated_at)) /
      60000,
    stale = age > Math.max((s.health.expected_interval_minutes || 15) * 2, 5),
    failed = s.health.state === "failed";
  el.className =
    "status " +
    (failed
      ? "failure"
      : stale || ["stale", "degraded", "not-collected"].includes(s.health.state)
        ? "warning"
        : "");
  el.textContent = `${replayContext ? "REPLAY · Simulated clock " + time(replayContext.at) + " London · " : archived ? "ARCHIVED SNAPSHOT · Automatic refresh paused · " : ""}${researchProvenance(snapshot, replayContext ? replayContext.scenario.source_kind === "synthetic" : demo).statusPrefix}${failed ? "Scan failed" : s.health.state === "not-collected" ? "Collection not yet completed" : s.health.state === "degraded" ? "Partial data available" : s.health.state === "empty" ? "Scan completed with no eligible movers" : stale || s.health.state === "stale" ? "Data stale" : "Latest scan received"} · Snapshot ${time(s.generated_at)} London · Last successful collection ${time(s.last_success_at)} · ${s.market.status || "Session status unavailable"}${s.health.last_error ? " · " + s.health.last_error : ""}`;
  $("session-label").textContent =
    `${s.market.session || "SESSION UNAVAILABLE"} · LONDON MARKET`;
}
function renderWatch() {
  const rows = snapshot?.watchlist || [];
  $("watch-metrics").replaceChildren(
    metric(
      "Previous-day movers",
      rows.filter((r) => r.is_mover !== false).length,
      "All qualifying risers and fallers",
    ),
    metric(
      "Reversal evidence",
      rows.filter((r) => r.is_mover !== false && r.status === "reversal")
        .length,
      "Observed, not guaranteed",
    ),
    metric(
      "Pending setups",
      (snapshot?.candidates || []).filter((r) =>
        ["pending", "open", "paper-eligible"].includes(r.status),
      ).length,
      "Paper eligibility rules apply",
    ),
    metric("First full candle", "08:15", "London time, plus feed delay"),
  );
  const query = $("watch-search").value.toLowerCase(),
    dir = $("direction-filter").value,
    status = $("status-filter").value;
  const filtered = rows.filter(
    (r) =>
      `${r.ticker} ${r.company || r.name || ""}`
        .toLowerCase()
        .includes(query) &&
      (dir === "universe" ||
        (r.is_mover !== false &&
          (dir === "all" ||
            (dir === "rise"
              ? r.previous_change_pct > 0
              : r.previous_change_pct < 0)))) &&
      (status === "all" || r.status === status),
  );
  table(
    $("watch-table"),
    filtered,
    [
      { label: "SHARE", render: shareButton },
      {
        label: "YESTERDAY",
        render: (r) =>
          text(
            "span",
            pct(r.previous_change_pct),
            r.previous_change_pct >= 0 ? "positive" : "negative",
          ),
      },
      { label: "OPENING GAP", render: (r) => pct(r.opening_gap_pct) },
      { label: "TODAY", render: (r) => pct(r.intraday_change_pct) },
      {
        label: "LAST PRICE",
        render: (r) => `${number(r.last_price)} ${r.quote_unit || "GBp"}`,
      },
      {
        label: "EVIDENCE",
        render: (r) => badge(r.stale ? "unavailable" : r.status),
      },
      { label: "LAST CANDLE · LONDON", render: (r) => time(r.last_candle_at) },
      { label: "REASON", key: "reason" },
    ],
    snapshot
      ? "No shares match this filter."
      : "No watchlist yet. The previous completed session will supply the first list.",
  );
  const cs = (snapshot?.candidates || []).filter((r) =>
    ["pending", "open", "paper-eligible"].includes(r.status),
  );
  if (!cs.length)
    empty(
      $("candidates"),
      "No qualifying setups. Waiting is a valid research outcome.",
    );
  else
    $("candidates").replaceChildren(
      ...cs.map((r) => {
        const e = text("div", "", "setup");
        e.append(
          shareButton(r),
          text("strong", `${r.direction || ""} · ${r.strategy || "setup"}`),
          text(
            "span",
            `Entry ${number(r.entry)} · Stop ${number(r.stop)} · Target ${number(r.target)}`,
          ),
          text(
            "p",
            `Available ${time(r.available_at)} · ${r.reason || "No reason supplied"}`,
          ),
        );
        return e;
      }),
    );
}
function svgEl(tag, attrs = {}) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  return e;
}
function chart(rows, share, setups, area = $("chart")) {
  if (!rows.length) {
    empty(area, "No completed candles available for this share.");
    return;
  }
  const candles = rows
    .filter((c) =>
      ["open", "high", "low", "close"].every((k) => Number.isFinite(c[k])),
    )
    .slice(-100);
  if (!candles.length) {
    empty(area, "Candle data is unavailable.");
    return;
  }
  const levels = [];
  if (Number.isFinite(share.previous_close))
    levels.push(["Previous close", share.previous_close, "#81949c"]);
  if (Number.isFinite(share.opening_price))
    levels.push(["Open", share.opening_price, "#4e7896"]);
  for (const s of setups.slice(0, 1))
    for (const k of ["entry", "stop", "target"])
      if (Number.isFinite(s[k]))
        levels.push([
          k,
          s[k],
          k === "stop" ? "#ac4848" : k === "target" ? "#087f77" : "#8d7029",
        ]);
  const values = [
    ...candles.flatMap((c) => [c.low, c.high]),
    ...levels.map((l) => l[1]),
  ];
  let low = Math.min(...values),
    high = Math.max(...values);
  const pad = (high - low) * 0.12 || 1;
  low -= pad;
  high += pad;
  const width = 1000,
    height = 360,
    left = 70,
    right = 105,
    top = 20,
    bottom = 45,
    x = (i) => left + ((i + 0.5) * (width - left - right)) / candles.length,
    y = (v) => top + ((high - v) / (high - low)) * (height - top - bottom);
  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${chosen} completed candlestick chart in ${share.quote_unit || "GBp"}. Exact values are available in the candle records table.`,
  });
  const title = svgEl("title");
  title.textContent = `${chosen} completed candles`;
  svg.append(title);
  for (let i = 0; i < 5; i++) {
    const v = low + ((high - low) * i) / 4;
    svg.append(
      svgEl("line", {
        x1: left,
        y1: y(v),
        x2: width - right,
        y2: y(v),
        stroke: "#e8eded",
      }),
    );
    const label = svgEl("text", {
      x: left - 10,
      y: y(v) + 4,
      "text-anchor": "end",
      fill: "#71818b",
      "font-size": 11,
    });
    label.textContent = number(v);
    svg.append(label);
  }
  for (const [name, v, colour] of levels) {
    svg.append(
      svgEl("line", {
        x1: left,
        y1: y(v),
        x2: width - right,
        y2: y(v),
        stroke: colour,
        "stroke-dasharray": "5 5",
        opacity: 0.8,
      }),
    );
    const label = svgEl("text", {
      x: width - right + 8,
      y: y(v) + 4,
      fill: colour,
      "font-size": 10,
    });
    label.textContent = name;
    svg.append(label);
  }
  const cw = Math.min(16, ((width - left - right) / candles.length) * 0.6);
  candles.forEach((c, i) => {
    const colour = c.close >= c.open ? "#087f77" : "#ac4848";
    svg.append(
      svgEl("line", {
        x1: x(i),
        x2: x(i),
        y1: y(c.high),
        y2: y(c.low),
        stroke: colour,
        "stroke-width": 1.4,
      }),
    );
    const rect = svgEl("rect", {
      x: x(i) - cw / 2,
      y: Math.min(y(c.open), y(c.close)),
      width: cw,
      height: Math.max(1, Math.abs(y(c.open) - y(c.close))),
      fill: colour,
    });
    const tip = svgEl("title");
    tip.textContent = `${time(c.time)} · O ${c.open} H ${c.high} L ${c.low} C ${c.close}`;
    rect.append(tip);
    svg.append(rect);
    if (
      i === 0 ||
      i === candles.length - 1 ||
      i % Math.max(1, Math.floor(candles.length / 5)) === 0
    ) {
      const t = svgEl("text", {
        x: x(i),
        y: height - 16,
        "text-anchor": "middle",
        fill: "#71818b",
        "font-size": 10,
      });
      t.textContent = new Date(c.time).toLocaleTimeString("en-GB", {
        timeZone: "Europe/London",
        hour: "2-digit",
        minute: "2-digit",
      });
      svg.append(t);
    }
  });
  area.replaceChildren(svg);
}
function renderDetail() {
  const rows = snapshot?.watchlist || [];
  if (!chosen || !rows.some((r) => r.ticker === chosen))
    chosen = rows[0]?.ticker || "";
  $("share-select").replaceChildren(
    ...(rows.length
      ? rows.map((r) => {
          const o = text(
            "option",
            `${r.ticker} · ${r.company || r.name || ""}`,
          );
          o.value = r.ticker;
          return o;
        })
      : [text("option", "No shares available")]),
  );
  $("share-select").value = chosen;
  const r = rows.find((r) => r.ticker === chosen) || {};
  $("share-title").textContent = chosen
    ? `${chosen} · ${r.company || r.name || ""}`
    : "Share detail";
  $("share-subtitle").textContent =
    r.reason || "Select a share after the first snapshot arrives.";
  const cs = (snapshot?.candidates || []).filter((c) => c.ticker === chosen),
    candles = Array.isArray(snapshot?.candles?.[chosen])
      ? snapshot.candles[chosen]
      : [];
  chart(candles, r, cs);
  $("share-facts").replaceChildren(
    metric(
      "Previous session",
      pct(r.previous_change_pct),
      r.previous_session || "Completed session",
    ),
    metric("Today", pct(r.intraday_change_pct), "Since the open"),
    metric(
      "Status",
      r.status || "Unavailable",
      r.stale ? "Data stale" : "Completed candle evidence",
    ),
    metric("Available", time(r.available_at), "London time"),
  );
  generic($("candle-table"), candles, "No candle records.");
  $("share-records").textContent = JSON.stringify(
    { watch: r, candidates: cs },
    null,
    2,
  );
}
function renderTradeTable(el, rows, closed) {
  const cols = [
    { label: "SHARE", render: shareButton },
    { label: "DIRECTION", key: "direction" },
    { label: "QUANTITY", render: (r) => number(r.quantity, 4) },
    { label: "ENTRY · LONDON", render: (r) => time(r.entry_at) },
    {
      label: "ENTRY PRICE",
      render: (r) => `${number(r.entry_price)} ${r.quote_unit || "GBp"}`,
    },
  ];
  if (closed)
    cols.push(
      { label: "EXIT · LONDON", render: (r) => time(r.exit_at) },
      {
        label: "EXIT PRICE",
        render: (r) => `${number(r.exit_price)} ${r.quote_unit || "GBp"}`,
      },
      {
        label: "NET P/L",
        render: (r) =>
          text("span", money(r.pnl), r.pnl >= 0 ? "positive" : "negative"),
      },
      { label: "COSTS", render: (r) => money(r.costs) },
      { label: "EXIT REASON", key: "reason" },
    );
  else
    cols.push(
      { label: "STOP", render: (r) => number(r.stop) },
      { label: "TARGET", render: (r) => number(r.target) },
      { label: "STATUS", render: (r) => badge(r.status) },
    );
  table(
    el,
    rows,
    cols,
    closed ? "No closed paper trades." : "No open paper positions.",
  );
  if (rows.length) {
    const details = document.createElement("details");
    details.append(
      text("summary", "Inspect every trade field"),
      text("pre", JSON.stringify(rows, null, 2)),
    );
    el.append(details);
  }
}
function renderEvaluation() {
  const e = snapshot?.evaluation || {},
    el = $("evaluation");
  el.replaceChildren();
  const hasEvidence = Object.keys(e).length > 0;
  const heading = text("div", "", "assessment");
  const label = !hasEvidence
    ? "No evaluation available"
    : e.sufficient_sample === false ||
        e.assessment === "insufficient-evidence" ||
        e.status === "insufficient_evidence"
      ? "Insufficient evidence"
      : e.assessment
        ? String(e.assessment).replaceAll("-", " ")
        : "Assessment pending";
  heading.append(
    badge(label),
    text(
      "p",
      e.label ||
        "Results describe paper research. They do not establish executable trading performance.",
      "muted",
    ),
  );
  el.append(heading);
  const metrics = text("div", "", "metrics compact");
  metrics.append(
    metric(
      "Observed sessions",
      typeof e.usable_sessions === "number"
        ? e.usable_sessions
        : typeof e.sessions === "number"
          ? e.sessions
          : "Unavailable",
      typeof e.criteria?.minimum_sessions === "number"
        ? `${e.criteria.minimum_sessions} proposed minimum`
        : "No session evidence supplied",
    ),
    metric(
      "Closed paper trades",
      typeof e.closed_trades === "number"
        ? e.closed_trades
        : typeof e.sample_counts?.closed === "number"
          ? e.sample_counts.closed
          : "Unavailable",
      typeof e.criteria?.minimum_closed_trades === "number"
        ? `${e.criteria.minimum_closed_trades} proposed minimum`
        : "No completed-trade count supplied",
    ),
    metric(
      "Net paper P/L",
      money(e.net_pnl),
      "After the model’s recorded costs",
    ),
    metric(
      "Realised drawdown",
      typeof e.max_drawdown_pct === "number"
        ? number(e.max_drawdown_pct) + "%"
        : "Unavailable",
      "Open-position risk is additional",
    ),
  );
  el.append(metrics);
  const criteria = text(
    "p",
    e.criteria_status === "proposed"
      ? "The pilot criteria are proposed and still need agreement. Meeting them cannot approve live trading."
      : "Pilot criteria and sufficient evidence must be agreed before drawing conclusions.",
    "muted",
  );
  el.append(criteria);
  if (e.sample_counts) {
    const c = e.sample_counts;
    el.append(
      text(
        "p",
        `Decision records: ${c.pending ?? 0} pending · ${c.open ?? 0} open · ${c.closed ?? 0} closed · ${c.skipped ?? 0} skipped · ${c.missing ?? 0} missing · ${c.missed ?? 0} missed. ${c.reconciled === true ? "Counts reconciled." : "Reconciliation unavailable or incomplete."}`,
        "muted",
      ),
    );
  }
  if (Array.isArray(e.limitations) && e.limitations.length) {
    const details = document.createElement("details");
    details.append(text("summary", "What these results cannot establish"));
    const list = document.createElement("ul");
    for (const item of e.limitations) list.append(text("li", item));
    details.append(list);
    el.append(details);
  }
  const raw = document.createElement("details");
  raw.append(
    text("summary", "Inspect evaluation records"),
    text("pre", JSON.stringify(e, null, 2)),
  );
  el.append(raw);
}
function renderPortfolio() {
  const p = snapshot?.paper;
  $("paper-metrics").replaceChildren(
    metric(
      "Initial paper capital",
      money(p?.capital),
      "Configuration, not real funds",
    ),
    metric(
      "Realised balance",
      money(p?.realised_balance),
      "After recorded trading costs",
    ),
    metric(
      "Estimated equity",
      money(p?.equity),
      "Includes open simulated exposure",
    ),
    metric("Open positions", p?.positions?.length || 0, "No live orders"),
  );
  renderTradeTable($("positions"), p?.positions || [], false);
  renderTradeTable($("closed-trades"), p?.closed_trades || [], true);
  renderEvaluation();
}
function renderLegacy() {
  const legacy = snapshot?.legacy,
    panel = $("legacy-panel");
  panel.hidden = !legacy;
  if (!legacy) return;
  const rows = legacy.records || [],
    selected = $("legacy-source").value || "all",
    sources = [
      ...new Set(
        rows.map((r) =>
          typeof r.source === "string" ? r.source : "Unknown source",
        ),
      ),
    ].sort();
  $("legacy-source").replaceChildren(
    ...["all", ...sources].map((source) => {
      const option = text(
        "option",
        source === "all" ? "All source files" : source,
      );
      option.value = source;
      return option;
    }),
  );
  $("legacy-source").value = sources.includes(selected) ? selected : "all";
  const source = $("legacy-source").value,
    query = $("legacy-search").value.toLowerCase(),
    filtered = rows.filter(
      (r) =>
        (source === "all" || r.source === source) &&
        JSON.stringify(r).toLowerCase().includes(query),
    );
  $("legacy-counts").textContent =
    `${rows.length} preserved historical records across ${sources.length} source files. Showing ${filtered.length}. Their performance is unverified.`;
  generic(
    $("legacy-records"),
    filtered,
    "No historical records match this filter.",
  );
}
function renderHistory() {
  renderLegacy();
  const q = $("history-search").value.toLowerCase(),
    rows = (snapshot?.runs || []).filter((r) =>
      JSON.stringify(r).toLowerCase().includes(q),
    );
  table(
    $("runs"),
    rows,
    [
      { label: "COMPLETED · LONDON", render: (r) => time(r.completed_at) },
      { label: "STATUS", render: (r) => badge(r.status) },
      { label: "SHARES CHECKED", key: "instruments" },
      { label: "NEW CANDIDATES", key: "candidate_count" },
      { label: "MODE", key: "mode" },
    ],
    "No scan history matches.",
  );
  if (rows.length) {
    const d = document.createElement("details");
    d.append(
      text("summary", "Inspect every run field"),
      text("pre", JSON.stringify(rows, null, 2)),
    );
    $("runs").append(d);
  }
  $("raw-snapshot").textContent = snapshot
    ? JSON.stringify(snapshot, null, 2)
    : "No snapshot received.";
}
async function refresh() {
  if (archived || replayContext) return;
  const button = $("refresh");
  button.disabled = true;
  try {
    const res = await fetch(demo ? "/demo.json" : "/api/snapshot", {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!res.ok)
      throw new Error(
        res.status === 403
          ? "Access denied. Sign in through the private Site with an authorised account."
          : `Dashboard request failed (${res.status}).`,
      );
    const body = await res.json();
    if (replayContext) return;
    snapshot = body.snapshot;
    revision = body.revision;
    renderAll();
  } catch (e) {
    $("data-status").className = "status failure";
    $("data-status").textContent =
      `Update failed: ${e.message}${snapshot ? " · Showing the last received snapshot from " + time(snapshot.generated_at) + "." : " No prices have been loaded."}`;
  } finally {
    button.disabled = false;
  }
}

function renderAll() {
  const provenance = researchProvenance(
    snapshot,
    replayContext ? replayContext.scenario.source_kind === "synthetic" : demo,
  );
  $("demo-banner").hidden = !provenance.synthetic;
  $("demo-banner").textContent = provenance.banner;
  freshness();
  renderWatch();
  renderDetail();
  renderPortfolio();
  renderHistory();
  renderInsights();
  renderReplayFrame();
  $("replay-banner").hidden = !replayContext;
  $("replay-banner-text").textContent = replayContext
    ? replayProvenance(replayContext.scenario) +
      " Simulated clock: " +
      time(replayContext.at) +
      " London."
    : "";
  $("archive-panel").hidden = Boolean(replayContext);
  const coverage =
    snapshot?.coverage ||
    snapshot?.health?.coverage ||
    snapshot?.evaluation?.coverage;
  const counts = coverage?.complete_journal_counts;
  $("coverage-note").textContent = coverage
    ? (counts
        ? `Snapshot coverage: ${snapshot.runs.length} of ${counts.runs} runs · ${snapshot.candidates.length} of ${counts.signals} setups · ${Object.keys(snapshot.candles).length} chart instruments. `
        : "Bounded snapshot view. ") +
      "Saved snapshots are in History. The collector CSV export contains the complete research journal."
    : "";
  document.querySelectorAll(".exports a,.button-link").forEach((a) => {
    const u = new URL(a.href);
    if (archived) u.searchParams.set("revision", revision);
    else u.searchParams.delete("revision");
    a.href = u.pathname + u.search;
  });
}
async function openArchive(id) {
  if (replayContext) return;
  try {
    const res = await fetch("/api/history?revision=" + encodeURIComponent(id), {
      cache: "no-store",
    });
    if (!res.ok) throw new Error("Saved snapshot could not be loaded.");
    const body = await res.json();
    if (replayContext) return;
    snapshot = body.snapshot;
    revision = body.revision;
    archived = true;
    renderAll();
    $("data-status").focus();
  } catch (e) {
    $("archives").prepend(text("p", e.message, "negative"));
  }
}
async function loadArchives(append = false) {
  if (replayContext) return;
  if (demo) {
    empty(
      $("archives"),
      "Demonstration mode has no persisted snapshot archive.",
    );
    return;
  }
  try {
    const res = await fetch(
      "/api/history?limit=20" +
        (append && archiveCursor
          ? "&before=" + encodeURIComponent(archiveCursor)
          : ""),
      { cache: "no-store" },
    );
    if (!res.ok) throw new Error("Snapshot archive is unavailable.");
    const body = await res.json();
    archiveRows = append ? [...archiveRows, ...body.revisions] : body.revisions;
    archiveCursor = body.next_before;
    $("more-archives").hidden = !archiveCursor;
    table(
      $("archives"),
      archiveRows,
      [
        { label: "GENERATED · LONDON", render: (r) => time(r.generated_at) },
        { label: "RECEIVED · LONDON", render: (r) => time(r.received_at) },
        {
          label: "INSPECT",
          render: (r) => {
            const b = text("button", "Open saved snapshot");
            b.type = "button";
            b.addEventListener("click", () => openArchive(r.revision));
            return b;
          },
        },
      ],
      "No saved snapshots yet.",
    );
  } catch (e) {
    empty($("archives"), e.message);
  }
}

function contextLabel() {
  return replayContext
    ? `REPLAY ${replayContext.scenario.source_kind} · ${replayContext.scenario.title} · simulated clock ${time(replayContext.at)} London`
    : archived
      ? `ARCHIVED paper research · ${time(snapshot?.generated_at)} London`
      : researchProvenance(snapshot, demo).synthetic
        ? "SYNTHETIC DEMONSTRATION · not observed market performance"
        : "Paper research · no live orders";
}
function journalEntries() {
  const cutoff = Date.parse(replayContext?.at || snapshot?.generated_at);
  return (snapshot?.decision_journal?.entries || []).filter(
    (entry) =>
      Number.isFinite(Date.parse(entry.at)) && Date.parse(entry.at) <= cutoff,
  );
}
function filteredJournal() {
  const query = $("journal-search").value.toLowerCase(),
    kind = $("journal-kind").value;
  return journalEntries().filter(
    (r) =>
      (kind === "all" || r.kind === kind) &&
      `${r.ticker || ""} ${r.title || ""} ${r.explanation || ""} ${r.status || ""}`
        .toLowerCase()
        .includes(query),
  );
}
function explanationCard(entry) {
  const card = text("article", "", "decision-card"),
    top = text("div", "", "decision-heading");
  top.append(
    text("span", time(entry.at) + " London", "decision-time"),
    badge(entry.status || entry.kind || "Recorded"),
  );
  card.append(
    top,
    text(
      "h3",
      `${entry.ticker ? entry.ticker + " · " : ""}${entry.title || "Research observation"}`,
    ),
    text("p", entry.explanation || "No explanation was supplied."),
  );
  const fields = [];
  if (entry.source) fields.push("Source: " + entry.source);
  if (entry.signal_candle_closed_at)
    fields.push(
      "Signal candle closed: " +
        time(entry.signal_candle_closed_at) +
        " London",
    );
  if (entry.eligible_interval_start)
    fields.push(
      "Earliest candidate interval starts: " +
        time(entry.eligible_interval_start) +
        " London",
    );
  if (entry.execution_at)
    fields.push("Modelled execution: " + time(entry.execution_at) + " London");
  card.append(text("p", fields.join(" · "), "muted"));
  return card;
}
function selectedReport() {
  const report = snapshot?.daily_report;
  if (!report) return null;
  const at = Date.parse(report.as_of),
    cutoff = Date.parse(replayContext?.at || snapshot?.generated_at);
  return Number.isFinite(at) && at <= cutoff ? report : null;
}
function humanFacts(container, record, labels) {
  const list = text("dl", "", "human-facts");
  for (const [key, label] of Object.entries(labels)) {
    const value = record?.[key];
    if (value === undefined || value === null) continue;
    const pair = document.createElement("div");
    pair.append(
      text("dt", label),
      text(
        "dd",
        typeof value === "boolean"
          ? value
            ? "Yes"
            : "No"
          : typeof value === "object"
            ? Object.entries(value)
                .map(
                  ([k, v]) =>
                    k.replaceAll("_", " ") +
                    ": " +
                    (typeof v === "boolean"
                      ? v
                        ? "Yes"
                        : "No"
                      : typeof v === "object"
                        ? "See complete report records"
                        : v),
                )
                .join(" · ")
            : value,
      ),
    );
    list.append(pair);
  }
  if (!list.children.length)
    empty(container, "No observations supplied for this snapshot.");
  else container.replaceChildren(list);
}
function renderInsights() {
  const journal = snapshot?.decision_journal,
    entries = journalEntries(),
    selected = $("journal-kind").value,
    kinds = [
      ...new Set(
        entries.map((e) => e.kind).filter((k) => typeof k === "string"),
      ),
    ].sort();
  $("journal-kind").replaceChildren(
    ...["all", ...kinds].map((kind) => {
      const option = text(
        "option",
        kind === "all" ? "All decision types" : kind.replaceAll("-", " "),
      );
      option.value = kind;
      return option;
    }),
  );
  $("journal-kind").value = kinds.includes(selected) ? selected : "all";
  const shown = filteredJournal();
  if (shown.length)
    $("journal-entries").replaceChildren(...shown.map(explanationCard));
  else
    empty(
      $("journal-entries"),
      journal
        ? "No decisions match these filters at this observation time."
        : "No decision journal is available for the selected snapshot.",
    );
  $("journal-context").textContent =
    contextLabel() +
    (journal
      ? " · Observations through " + time(journal.as_of) + " London."
      : "");
  $("journal-coverage").textContent = journal
    ? `${shown.length} shown of ${entries.length} available entries${typeof journal.total_entries === "number" ? " (" + journal.total_entries + " in the source journal)" : ""}. ${journal.truncated ? "This snapshot is bounded; complete records remain in the collector journal." : ""} ${journal.historical_reconstruction ? "Historical reconstruction; read the record limitations." : ""}`
    : "";
  $("journal-records").textContent = JSON.stringify(journal || {}, null, 2);
  $("journal-download").disabled = !entries.length;
  const report = selectedReport();
  $("report-download").disabled = !report;
  $("report-headline").textContent =
    report?.headline || "Daily paper-research report";
  $("report-context").textContent =
    contextLabel() +
    (report
      ? ` · ${report.status || "Status unavailable"} · as of ${time(report.as_of)} London.`
      : " · No report is available at this selected observation time.");
  if (report?.summary?.length)
    $("report-summary").replaceChildren(
      ...report.summary.map((value) => text("p", value)),
    );
  else
    empty(
      $("report-summary"),
      "A report will appear when the collector supplies session evidence.",
    );
  const m = report?.metrics;
  $("report-metrics").replaceChildren(
    metric(
      "Entries today",
      m?.entries_today ?? "Unavailable",
      "Modelled entry date",
    ),
    metric(
      "Trades closed",
      m?.closed_today ?? "Unavailable",
      "Exit observed in this session",
    ),
    metric(
      "Closed trade net P/L",
      money(m?.net_closed_pnl_gbp),
      "After the modelled costs",
    ),
    metric(
      "Estimated equity",
      money(m?.equity_estimate_gbp),
      m?.equity_stale
        ? "Warning: equity uses stale prices"
        : "Includes open paper positions",
    ),
  );
  humanFacts(
    $("report-costs"),
    {
      price: money(m?.gross_fill_pnl_gbp),
      commission: money(m?.dealing_commission_gbp),
      tax: money(m?.stamp_duty_gbp),
      net: money(m?.net_closed_pnl_gbp),
      reconciled: m?.cost_bridge_reconciled,
    },
    {
      price: "Price profit/loss after spread and slippage",
      commission: "Less dealing commission",
      tax: "Less purchase tax",
      net: "Net full-trade profit/loss",
      reconciled: "Cost bridge reconciled",
    },
  );
  humanFacts($("report-watch"), report?.watch, {
    total: "Tracked shares",
    movers: "Previous-session movers",
    risers: "Previous risers",
    fallers: "Previous fallers",
    reversal: "Reversal evidence",
    continuation: "Continuation evidence",
    undecided: "Undecided",
    unavailable: "Unavailable",
    stale: "Stale shares",
  });
  if (typeof report?.watch?.universe_basis === "string")
    $("report-watch").append(text("p", report.watch.universe_basis, "muted"));
  const quality = text("div", "", "report-quality");
  const decisions = document.createElement("div");
  humanFacts(decisions, report?.decisions, {
    total: "Setup decisions",
    pending: "Pending",
    open: "Open",
    closed: "Closed",
    skipped: "Skipped",
    missing: "Missing",
    missed: "Missed",
    reconciled: "Counts reconciled",
  });
  quality.append(decisions);
  if (report?.data_quality) {
    const note = text("div", "", "quality-note"),
      data = report.data_quality,
      delay = data.first_observation_delay_minutes || {};
    note.append(text("h3", "Collection quality", "quality-heading"));
    const facts = document.createElement("div");
    humanFacts(
      facts,
      {
        candles: data.observed_intraday_candles,
        samples: delay.sample_count,
        median:
          typeof delay.median === "number"
            ? number(delay.median) + " min"
            : "Unavailable",
        maximum:
          typeof delay.maximum === "number"
            ? number(delay.maximum) + " min"
            : "Unavailable",
        missing: data.missing_watch_instruments,
        stale: data.stale_watch_instruments ?? report.watch?.stale,
        failed: data.failed_or_degraded_runs,
        gaps: data.new_gap_warnings_today ?? data.unresolved_coverage_events,
        unresolved: data.open_positions_with_unresolved_coverage,
        verified: data.coverage_verified,
      },
      {
        candles: "Observed intraday candles",
        samples: "Observation-delay samples",
        median: "Median observed delay",
        maximum: "Longest observed delay",
        missing: "Missing watch instruments",
        stale: "Stale watch instruments",
        failed: "Failed or degraded collections",
        gaps: "New gap warnings today",
        unresolved: "Open positions with unresolved coverage",
        verified: "Complete coverage verified",
      },
    );
    note.append(
      facts,
      text(
        "p",
        typeof delay.basis === "string"
          ? delay.basis
          : "Observation timing measures the stored fetch after a candle ended. Archive backfills can increase this delay; it does not establish provider latency.",
        "muted",
      ),
    );
    if (typeof data.coverage_note === "string")
      note.append(text("p", data.coverage_note, "muted"));
    if (typeof data.coverage_event_basis === "string")
      note.append(text("p", data.coverage_event_basis, "muted"));
    quality.append(note);
  }
  $("report-quality").replaceChildren(quality);
  $("report-limitations").replaceChildren(
    ...(
      report?.limitations || [
        "No report limitations supplied because no report is available.",
      ]
    ).map((value) => text("li", value)),
  );
  $("report-records").textContent = JSON.stringify(report || {}, null, 2);
}
function renderReplayFrame() {
  $("replay-frame-content").hidden = !replayContext;
  if (!replayContext) return;
  const s = snapshot,
    watch = s?.watchlist || [];
  if (!watch.some((r) => r.ticker === chosen)) chosen = watch[0]?.ticker || "";
  $("replay-share").replaceChildren(
    ...watch.map((r) => {
      const o = text("option", r.ticker + (r.company ? " · " + r.company : ""));
      o.value = r.ticker;
      return o;
    }),
  );
  $("replay-share").value = chosen;
  const share = watch.find((r) => r.ticker === chosen) || {},
    candles = Array.isArray(s?.candles?.[chosen]) ? s.candles[chosen] : [],
    setups = (s?.candidates || []).filter((r) => r.ticker === chosen);
  chart(candles, share, setups, $("replay-chart"));
  $("replay-observation").textContent =
    `Simulated clock ${time(replayContext.at)} London. Selected share’s last candle ${time(share.last_candle_at)}; available to the collector ${time(share.available_at)}.`;
  $("replay-share-reason").textContent =
    share.reason || "No share explanation was available in this frame.";
  $("replay-facts").replaceChildren(
    metric(
      "Replay clock",
      new Date(replayContext.at).toLocaleTimeString("en-GB", {
        timeZone: "Europe/London",
        hour: "2-digit",
        minute: "2-digit",
      }),
      `Frame ${replayContext.index + 1} of ${replayContext.total}`,
    ),
    metric("Known setups", s.candidates.length, "Only this frozen snapshot"),
    metric(
      "Open paper positions",
      s.paper.positions.length,
      "Simulation, no broker orders",
    ),
    metric(
      "Selected evidence",
      share.status || "Unavailable",
      chosen || "No share available",
    ),
  );
  if (setups.length)
    $("replay-setups").replaceChildren(
      ...setups.map((r) => {
        const card = text("div", "", "setup");
        card.append(
          text(
            "strong",
            `${r.ticker} · ${r.direction || ""} · ${r.status || "Recorded"}`,
          ),
          text("p", r.reason || "No setup explanation supplied."),
          text(
            "p",
            `Known at ${time(r.available_at)} London · entry ${number(r.entry)} · stop ${number(r.stop)} · target ${number(r.target)}`,
            "muted",
          ),
        );
        return card;
      }),
    );
  else
    empty(
      $("replay-setups"),
      "No setup for this share had been recorded at this moment.",
    );
  const recent = journalEntries().slice(-4);
  if (recent.length)
    $("replay-decisions").replaceChildren(...recent.map(explanationCard));
  else
    empty(
      $("replay-decisions"),
      "No decision explanations were available at this moment.",
    );
}
const replayController = mountReplay({
  formatTime: time,
  applyFrame(frame, scenario, position) {
    if (!replayContext) beforeReplay = { snapshot, revision, archived, chosen };
    replayContext = { at: frame.at, scenario, ...position };
    snapshot = frame.snapshot;
    revision = null;
    archived = false;
    renderAll();
  },
  exitReplay() {
    const restore = beforeReplay;
    replayContext = null;
    beforeReplay = null;
    if (restore) {
      snapshot = restore.snapshot;
      revision = restore.revision;
      archived = restore.archived;
      chosen = restore.chosen;
    }
    renderAll();
    if (!archived) refresh();
  },
});
$("journal-search").addEventListener("input", renderInsights);
$("journal-kind").addEventListener("change", renderInsights);
$("journal-download").addEventListener("click", () => {
  const rows = filteredJournal().map((r) => ({
    research_context: contextLabel(),
    ...r,
  }));
  downloadText(
    recordsCSV(rows),
    (replayContext ? "REPLAY-" : "") + "decision-journal.csv",
    "text/csv; charset=utf-8",
  );
});
$("report-download").addEventListener("click", () =>
  downloadText(
    reportText(selectedReport(), contextLabel()),
    (replayContext ? "REPLAY-" : "") + "daily-paper-report.txt",
  ),
);
$("replay-share").addEventListener("change", (e) => {
  chosen = e.target.value;
  renderAll();
});

$("legacy-source").addEventListener("change", renderLegacy);
$("legacy-search").addEventListener("input", renderLegacy);
$("load-archives").addEventListener("click", () => loadArchives());
$("more-archives").addEventListener("click", () => loadArchives(true));
$("return-latest").addEventListener("click", () => {
  archived = false;
  refresh();
});

$("demo-banner").hidden = !demo;
document.querySelectorAll(".exports a,.button-link").forEach((a) =>
  a.addEventListener("click", (e) => {
    if (demo || replayContext) {
      e.preventDefault();
      const tableName = new URL(a.href).searchParams.get("table"),
        rows =
          tableName === "legacy"
            ? snapshot?.legacy?.records || []
            : snapshot?.[tableName] || snapshot?.paper?.[tableName] || [],
        keys = [...new Set(rows.flatMap(Object.keys))],
        cell = (v) => {
          const raw = String(
              typeof v === "object" ? JSON.stringify(v) : (v ?? ""),
            ),
            safe =
              typeof v === "string" && /^[\s]*[=+@-]/.test(raw)
                ? "'" + raw
                : raw;
          return '"' + safe.replaceAll('"', '""') + '"';
        },
        csv =
          "\ufeff" +
          [
            keys.map(cell).join(","),
            ...rows.map((r) => keys.map((k) => cell(r[k])).join(",")),
          ].join("\r\n"),
        url = URL.createObjectURL(new Blob([csv], { type: "text/csv" })),
        link = document.createElement("a");
      link.href = url;
      link.download =
        (replayContext ? "REPLAY-" : "DEMO-") + tableName + ".csv";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }),
);
for (const id of ["watch-search", "direction-filter", "status-filter"])
  $(id).addEventListener("input", renderWatch);
$("history-search").addEventListener("input", renderHistory);
$("share-select").addEventListener("change", (e) => {
  chosen = e.target.value;
  renderDetail();
});
$("refresh").addEventListener("click", refresh);
window.addEventListener("hashchange", route);
route();
renderWatch();
renderDetail();
renderPortfolio();
renderHistory();
renderInsights();
refresh();
setInterval(refresh, 60000);
