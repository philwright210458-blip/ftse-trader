export function recordsCSV(rows) {
  const keys = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const cell = (value) => {
    const raw =
      typeof value === "object" && value !== null
        ? JSON.stringify(value)
        : String(value ?? "");
    const safe =
      typeof value === "string" && /^[\s]*[=+@-]/.test(raw) ? "'" + raw : raw;
    return '"' + safe.replaceAll('"', '""') + '"';
  };
  return (
    "\ufeff" +
    [
      keys.map(cell).join(","),
      ...rows.map((row) => keys.map((key) => cell(row[key])).join(",")),
    ].join("\r\n")
  );
}
export function reportText(report, context = "Paper research") {
  if (!report)
    return `${context}\nNo daily report is available for the selected snapshot.\n`;
  if (typeof report.text === "string") return context + "\n\n" + report.text;
  const lines = [
    context,
    report.headline || "Daily paper-research report",
    `Session: ${report.session || "Unavailable"}`,
    `As of: ${report.as_of || "Unavailable"}`,
    `Status: ${report.status || "Unavailable"}`,
    "",
    ...(report.summary || []),
  ];
  for (const [title, values] of [
    ["Metrics", report.metrics],
    ["Watch", report.watch],
    ["Decisions", report.decisions],
    ["Data quality", report.data_quality],
  ]) {
    if (values) {
      lines.push("", title);
      for (const [key, value] of Object.entries(values))
        lines.push(
          `${key.replaceAll("_", " ")}: ${typeof value === "object" ? JSON.stringify(value) : value}`,
        );
    }
  }
  lines.push("", "Limitations", ...(report.limitations || []));
  return lines.join("\n") + "\n";
}
export function downloadText(
  content,
  filename,
  type = "text/plain; charset=utf-8",
) {
  const url = URL.createObjectURL(new Blob([content], { type })),
    a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
