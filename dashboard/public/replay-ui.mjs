export function selectReplayFrame(scenario, index) {
  const frames = Array.isArray(scenario?.frames) ? scenario.frames : [];
  if (!frames.length) return null;
  const bounded = Math.min(
    frames.length - 1,
    Math.max(0, Math.trunc(Number(index) || 0)),
  );
  return {
    frame: frames[bounded],
    index: bounded,
    total: frames.length,
    atEnd: bounded === frames.length - 1,
  };
}
export function validateReplayBundle(bundle) {
  if (
    bundle?.schema_version !== 1 ||
    bundle?.kind !== "paper-replay" ||
    !Array.isArray(bundle.scenarios) ||
    !bundle.scenarios.length
  )
    return "Choose a schema-version-1 paper-replay bundle with scenarios.";
  const ids = new Set();
  for (const scenario of bundle.scenarios) {
    if (
      !scenario ||
      typeof scenario.id !== "string" ||
      !scenario.id ||
      ids.has(scenario.id) ||
      typeof scenario.title !== "string" ||
      !["synthetic", "recorded"].includes(scenario.source_kind) ||
      !Array.isArray(scenario.frames) ||
      !scenario.frames.length
    )
      return "Every scenario needs a unique name, source kind and completed frames.";
    if (
      (scenario.description !== undefined &&
        typeof scenario.description !== "string") ||
      (scenario.limitations !== undefined &&
        (!Array.isArray(scenario.limitations) ||
          scenario.limitations.some((v) => typeof v !== "string")))
    )
      return "Scenario explanations must be plain text.";
    ids.add(scenario.id);
    let previous = -Infinity;
    for (const frame of scenario.frames) {
      const at = typeof frame?.at === "string" ? Date.parse(frame.at) : NaN,
        s = frame?.snapshot;
      if (
        !Number.isFinite(at) ||
        at <= previous ||
        s?.schema_version !== 1 ||
        !Array.isArray(s.watchlist) ||
        !Array.isArray(s.candidates) ||
        !s.paper ||
        !Array.isArray(s.paper.positions) ||
        !Array.isArray(s.paper.closed_trades) ||
        !Array.isArray(s.paper.events) ||
        !Array.isArray(s.runs) ||
        !s.candles ||
        !s.market ||
        !s.health
      )
        return "Replay frames must contain ordered timestamps and complete paper snapshots.";
      previous = at;
      const rows = [
        ...s.watchlist,
        ...s.candidates,
        ...s.paper.positions,
        ...s.paper.closed_trades,
        ...s.paper.events,
        ...s.runs,
      ];
      if (rows.some((r) => !r || typeof r !== "object" || Array.isArray(r)))
        return "Snapshot rows must be records.";
      const displayFields = [
        "ticker",
        "company",
        "name",
        "reason",
        "status",
        "direction",
        "strategy",
        "quote_unit",
        "last_candle_at",
        "available_at",
        "previous_session",
        "entry_at",
        "exit_at",
      ];
      for (const r of [
        ...s.watchlist,
        ...s.candidates,
        ...s.paper.positions,
        ...s.paper.closed_trades,
      ])
        if (
          typeof r.ticker !== "string" ||
          displayFields.some(
            (k) =>
              r[k] !== undefined && r[k] !== null && typeof r[k] !== "string",
          )
        )
          return "Share and trade display fields must use valid text.";
      if (
        typeof s.generated_at !== "string" ||
        !Number.isFinite(Date.parse(s.generated_at)) ||
        (s.last_success_at !== null &&
          s.last_success_at !== undefined &&
          typeof s.last_success_at !== "string")
      )
        return "Snapshot generation and collection clocks must be valid text timestamps.";
      if (
        ["state", "last_error"].some(
          (k) =>
            s.health[k] !== undefined &&
            s.health[k] !== null &&
            typeof s.health[k] !== "string",
        ) ||
        ["session", "status"].some(
          (k) =>
            s.market[k] !== undefined &&
            s.market[k] !== null &&
            typeof s.market[k] !== "string",
        )
      )
        return "Market and collection status must be plain text.";
      const evaluation = s.evaluation || {};
      if (
        ["label", "status", "assessment", "criteria_status"].some(
          (k) =>
            evaluation[k] !== undefined && typeof evaluation[k] !== "string",
        ) ||
        (evaluation.sample_counts &&
          Object.values(evaluation.sample_counts).some(
            (v) => typeof v !== "number" && typeof v !== "boolean",
          )) ||
        (evaluation.limitations !== undefined &&
          (!Array.isArray(evaluation.limitations) ||
            evaluation.limitations.some((v) => typeof v !== "string")))
      )
        return "Evaluation fields use an unsupported display format.";
      if (
        s.coverage?.complete_journal_counts &&
        Object.values(s.coverage.complete_journal_counts).some(
          (v) => typeof v !== "number" || !Number.isFinite(v),
        )
      )
        return "Snapshot coverage counts must be numeric.";
      if (
        s.decision_journal &&
        (!Array.isArray(s.decision_journal.entries) ||
          s.decision_journal.entries.some(
            (r) =>
              !r ||
              typeof r !== "object" ||
              [
                "at",
                "ticker",
                "kind",
                "title",
                "explanation",
                "status",
                "source",
                "execution_at",
                "eligible_interval_start",
                "signal_candle_closed_at",
              ].some(
                (k) =>
                  r[k] !== undefined &&
                  r[k] !== null &&
                  typeof r[k] !== "string",
              ),
          ))
      )
        return "Decision explanations must be valid timestamped records.";
      if (
        s.health.expected_interval_minutes !== undefined &&
        (typeof s.health.expected_interval_minutes !== "number" ||
          !Number.isFinite(s.health.expected_interval_minutes))
      )
        return "The expected collection interval must be numeric.";
      if (
        s.daily_report?.metrics &&
        [
          "entries_today",
          "closed_today",
          "net_closed_pnl_gbp",
          "equity_estimate_gbp",
        ].some(
          (k) =>
            s.daily_report.metrics[k] !== undefined &&
            s.daily_report.metrics[k] !== null &&
            typeof s.daily_report.metrics[k] !== "number",
        )
      )
        return "Report summary metrics must be numeric or unavailable.";
      if (
        s.daily_report &&
        (["headline", "status", "as_of"].some(
          (k) =>
            s.daily_report[k] !== undefined &&
            typeof s.daily_report[k] !== "string",
        ) ||
          !Array.isArray(s.daily_report.summary) ||
          s.daily_report.summary.some((v) => typeof v !== "string") ||
          !Array.isArray(s.daily_report.limitations) ||
          s.daily_report.limitations.some((v) => typeof v !== "string"))
      )
        return "Daily reports must contain plain-text summaries and limitations.";
      if (
        Object.values(s.candles).some(
          (rows) =>
            !Array.isArray(rows) ||
            rows.some(
              (r) =>
                !r ||
                typeof r.time !== "string" ||
                !Number.isFinite(Date.parse(r.time)) ||
                !["open", "high", "low", "close"].every(
                  (k) => typeof r[k] === "number" && Number.isFinite(r[k]),
                ),
            ),
        )
      )
        return "Candle rows must contain valid timestamps and numeric prices.";
      for (const value of [
        s.generated_at,
        s.last_success_at,
        s.decision_journal?.as_of,
        s.daily_report?.as_of,
      ])
        if (
          value &&
          (!Number.isFinite(Date.parse(value)) || Date.parse(value) > at)
        )
          return "A frame contains a snapshot or report from after its replay clock.";
      const observations = [
        ...(s.decision_journal?.entries || []).map((r) => r.at),
        ...s.candidates.map((r) => r.available_at),
        ...Object.values(s.candles).flatMap((rows) =>
          Array.isArray(rows)
            ? rows.map((r) => r.available_at || r.time)
            : [null],
        ),
        ...s.paper.closed_trades.map((r) => r.exit_observed_at || r.exit_at),
      ];
      if (
        observations.some(
          (value) =>
            value &&
            (!Number.isFinite(Date.parse(value)) || Date.parse(value) > at),
        )
      )
        return "A frame contains observations from after its replay clock.";
    }
  }
  return null;
}
export function replayProvenance(scenario) {
  return scenario?.source_kind === "recorded"
    ? "RECORDED REPLAY · Frozen observations, not a live feed. Playback never places orders or changes the paper ledger."
    : "SYNTHETIC REPLAY · Fictional market scenarios and authored setups, processed by the paper engine. This is not observed market performance.";
}
export function mountReplay({ applyFrame, exitReplay, formatTime }) {
  const $ = (id) => document.getElementById(id);
  let bundle = null,
    scenario = null,
    index = 0,
    active = false,
    timer = null,
    localImport = false;
  const pause = () => {
    if (timer) clearInterval(timer);
    timer = null;
    $("replay-play").textContent = "Play";
    $("replay-play").setAttribute("aria-pressed", "false");
  };
  function controls() {
    const selection = selectReplayFrame(scenario, index),
      ready = Boolean(selection);
    for (const id of [
      "replay-play",
      "replay-next",
      "replay-prev",
      "replay-reset",
      "replay-scrub",
      "replay-start",
    ])
      $(id).disabled = !ready;
    $("replay-prev").disabled = !ready || index === 0;
    $("replay-next").disabled = !ready || selection.atEnd;
    $("replay-scrub").max = String(Math.max(0, (selection?.total || 1) - 1));
    $("replay-scrub").value = String(index);
    $("replay-scrub").setAttribute(
      "aria-valuetext",
      ready
        ? `${formatTime(selection.frame.at)} London, frame ${index + 1} of ${selection.total}`
        : "No frames",
    );
    $("replay-clock").textContent = ready
      ? `${formatTime(selection.frame.at)} London · frame ${index + 1} of ${selection.total}`
      : "No replay frames available.";
    $("replay-position").textContent = active
      ? "Viewing a frozen replay frame. The latest data feed is paused."
      : "Choose a scenario, then start its replay. Latest data remains unchanged.";
    $("replay-description").textContent =
      scenario?.description || "Loading the available paper-research examples.";
    const limitations = Array.isArray(scenario?.limitations)
      ? scenario.limitations
      : [];
    $("replay-limitations").replaceChildren(
      ...limitations.map((value) => {
        const li = document.createElement("li");
        li.textContent = value;
        return li;
      }),
    );
    $("replay-provenance").textContent = scenario
      ? replayProvenance(scenario)
      : "Replays use frozen evidence. They never send trades.";
  }
  function setFrame(next) {
    const s = selectReplayFrame(scenario, next);
    if (!s) return;
    index = s.index;
    active = true;
    applyFrame(s.frame, scenario, { index, total: s.total });
    if (s.atEnd) pause();
    controls();
  }
  function start() {
    pause();
    setFrame(0);
  }
  function play() {
    if (timer) {
      pause();
      return;
    }
    const s = selectReplayFrame(scenario, index);
    if (!s) return;
    if (!active || s.atEnd) setFrame(0);
    if (s.total === 1) {
      pause();
      return;
    }
    const interval = 1000 / Number($("replay-speed").value || 1);
    timer = setInterval(() => setFrame(index + 1), interval);
    $("replay-play").textContent = "Pause";
    $("replay-play").setAttribute("aria-pressed", "true");
  }
  $("replay-start").addEventListener("click", start);
  $("replay-play").addEventListener("click", play);
  $("replay-next").addEventListener("click", () => {
    pause();
    setFrame(index + 1);
  });
  $("replay-prev").addEventListener("click", () => {
    pause();
    setFrame(index - 1);
  });
  $("replay-reset").addEventListener("click", start);
  $("replay-scrub").addEventListener("input", (e) => {
    pause();
    setFrame(e.target.value);
  });
  $("replay-speed").addEventListener("change", () => {
    pause();
    controls();
  });
  $("replay-scenario").addEventListener("change", () => {
    pause();
    scenario =
      bundle?.scenarios.find((s) => s.id === $("replay-scenario").value) ||
      null;
    index = 0;
    if (active) setFrame(0);
    else controls();
  });
  $("exit-replay").addEventListener("click", () => {
    pause();
    active = false;
    exitReplay();
    controls();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) pause();
  });
  function accept(next) {
    const error = validateReplayBundle(next);
    if (error) throw new Error(error);
    pause();
    bundle = next;
    $("replay-scenario").replaceChildren(
      ...bundle.scenarios.map((s) => {
        const o = document.createElement("option");
        o.value = s.id;
        o.textContent = s.title;
        return o;
      }),
    );
    scenario = bundle.scenarios[0];
    index = 0;
    if (active) setFrame(0);
    else controls();
  }
  async function load() {
    try {
      const response = await fetch("/replay.json", {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!response.ok)
        throw new Error(
          "Replay examples are unavailable. The current dashboard is still usable.",
        );
      const next = await response.json();
      if (!localImport) accept(next);
    } catch (error) {
      controls();
      $("replay-position").textContent = error.message;
    }
  }
  $("replay-file").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      if (file.size > 12 * 1024 * 1024)
        throw new Error("This file exceeds the 12 MiB replay limit.");
      const next = JSON.parse(await file.text());
      const error = validateReplayBundle(next);
      if (error) throw new Error(error);
      localImport = true;
      accept(next);
      $("replay-import-status").textContent =
        "Replay file loaded locally. No file was uploaded and no paper ledger changed.";
    } catch (error) {
      $("replay-import-status").textContent =
        "File not loaded: " +
        error.message +
        " Your previous scenario is unchanged.";
    } finally {
      event.target.value = "";
    }
  });
  controls();
  load();
  return {
    pause,
    get active() {
      return active;
    },
  };
}
