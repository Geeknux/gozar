import { type ReactNode, useMemo, useState } from "react";

import { ApiError } from "../api";
import { config } from "../config";
import { engineLabel } from "../engine";
import { type MessageKey, useI18n } from "../i18n";
import Chart from "./Chart";
import Drilldown from "./Drilldown";
import {
  type Cell,
  CHARTS,
  type ChartType,
  type Column,
  cellOf,
  comparisonColumn,
  dimensionColumns,
  dimensionText,
  measureColumns,
  type Result,
  type Row,
  type Scalar,
  type Spec,
  trend,
} from "./model";
import { isolate } from "../bidi";

type Props = {
  result: Result;
  spec: Spec; // the spec that produced ``result`` (for drill-down and export)
  jobId?: string; // set when a background job produced it: export its stored result
  chart: ChartType;
  onChart?: (chart: ChartType) => void;
  /** dashboard widgets: no chart switcher or meta line, small export buttons */
  compact?: boolean;
  /** compact only: shown at the start of the tools row (the widget's title) */
  head?: ReactNode;
  /** names exports (file and sheet): the saved report's or widget's name */
  title?: string;
};

type Drill = { cell: Cell; measure: string | null; comparison: boolean; title: string };

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export default function Results({ result, spec, jobId, chart, onChart, compact = false, head, title: exportTitle = "" }: Props) {
  const { t, fmt } = useI18n();
  const dims = dimensionColumns(result);
  const measures = measureColumns(result);
  const [measureKey, setMeasureKey] = useState<string>("");
  const measure = measures.find((m) => m.key === measureKey) ?? measures[0];
  const [drill, setDrill] = useState<Drill | null>(null);
  const [exportError, setExportError] = useState("");

  const describe = (cell: Cell, measureKey: string | null) => {
    const parts = dims.filter((d) => d.key in cell).map((d) => dimensionText(d, cell[d.key], fmt, t));
    const m = measures.find((x) => x.key === measureKey);
    if (m) parts.push(m.label);
    return parts.join(" · ") || result.columns.map((c) => c.label).join(" · ");
  };
  const open = (cell: Cell, measureKey: string | null, comparison = false) => {
    let title = describe(cell, measureKey);
    if (comparison && result.comparison) {
      const range = t("report.periodRange", {
        start: fmt.dateIn(result.comparison.start, result.calendar, { dateStyle: "medium" }),
        end: fmt.dateIn(result.comparison.end, result.calendar, { dateStyle: "medium" }),
      });
      title = `${title} · ${t(`compare.${result.comparison.kind}`)} (${range})`;
    }
    setDrill({ cell, measure: measureKey, comparison, title });
  };

  async function download(format: "csv" | "xlsx") {
    setExportError("");
    try {
      const query = new URLSearchParams({ format, ...(exportTitle ? { title: exportTitle } : {}) });
      const resp = jobId
        ? await fetch(`${config.apiBase}/reports/jobs/${jobId}/export?${query}`, { credentials: "same-origin" })
        : await fetch(`${config.apiBase}/reports/export?${query}`, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
            body: JSON.stringify(spec),
          });
      if (!resp.ok) throw new ApiError(resp.status, await resp.json().catch(() => ({})));
      const disposition = resp.headers.get("Content-Disposition") ?? "";
      const encoded = /filename\*=utf-8''([^;]+)/i.exec(disposition)?.[1];
      const name = encoded ? decodeURIComponent(encoded) : (/filename="([^"]+)"/.exec(disposition)?.[1] ?? `report.${format}`);
      const url = URL.createObjectURL(await resp.blob());
      const a = Object.assign(document.createElement("a"), { href: url, download: name });
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : String(err));
    }
  }

  const needsMeasurePicker = ["bar", "line", "area", "pie", "pivot"].includes(chart) && measures.length > 1;

  let body;
  if (!result.rows.length) body = <p className="empty">{t("report.empty")}</p>;
  else if (chart === "table") body = <Table result={result} onDrill={open} />;
  else if (chart === "kpi") {
    body = dims.length ? <p className="empty">{t("report.kpiNeedsNoDimensions")}</p> : <Kpi result={result} onDrill={open} />;
  } else if (chart === "pivot") {
    body = dims.length === 2 ? <Pivot result={result} measure={measure} onDrill={open} /> : <p className="empty">{t("report.pivotNeedsTwo")}</p>;
  } else if (chart === "pie" && dims.length !== 1) body = <p className="empty">{t("report.pieNeedsDimension")}</p>;
  else if (!dims.length) body = <p className="empty">{t("report.chartNeedsDimension")}</p>;
  else body = <Chart result={result} kind={chart} measure={measure} onDrill={open} />;

  const exports = (
    <>
      {!compact && <span className="muted">{t("report.export")}:</span>}
      <button type="button" className={compact ? "button small" : undefined} onClick={() => download("csv")}>
        {t("report.exportCsv")}
      </button>
      <button type="button" className={compact ? "button small" : undefined} onClick={() => download("xlsx")}>
        {t("report.exportXlsx")}
      </button>
    </>
  );
  const drillDialog = drill && (
    <Drilldown
      spec={spec}
      cell={drill.cell}
      measure={drill.measure}
      comparison={drill.comparison}
      title={drill.title}
      onClose={() => setDrill(null)}
    />
  );

  if (compact) {
    return (
      <div className="results compact">
        <div className="widget-tools">
          {head}
          {needsMeasurePicker && (
            <select value={measure.key} aria-label={t("report.pivotMeasure")} onChange={(e) => setMeasureKey(e.target.value)}>
              {measures.map((m) => (
                <option key={m.key} value={m.key}>
                  {isolate(m.label)}
                </option>
              ))}
            </select>
          )}
          {exports}
        </div>
        {result.truncated && <p className="notice">{t("report.truncated", { count: result.row_count })}</p>}
        {exportError && <p className="error">{exportError}</p>}
        <div className="widget-body">{body}</div>
        {drillDialog}
      </div>
    );
  }

  return (
    <section className="results card">
      <div className="results-head">
        <div className="segmented chart-switch" role="group">
          {CHARTS.map((c) => (
            <button key={c} type="button" className={c === chart ? "active" : ""} aria-pressed={c === chart} onClick={() => onChart?.(c)}>
              {t(`chart.${c}` as MessageKey)}
            </button>
          ))}
        </div>
        <div className="results-actions">
          {needsMeasurePicker && (
            <select value={measure.key} aria-label={t("report.pivotMeasure")} onChange={(e) => setMeasureKey(e.target.value)}>
              {measures.map((m) => (
                <option key={m.key} value={m.key}>
                  {isolate(m.label)}
                </option>
              ))}
            </select>
          )}
          {exports}
        </div>
      </div>
      <p className="muted results-meta">
        {t("report.rows", { count: result.row_count })}
        {result.period && (
          <>
            {" · "}
            {t("report.periodRange", {
              start: fmt.dateIn(result.period.start, result.calendar, { dateStyle: "medium" }),
              end: fmt.dateIn(result.period.end, result.calendar, { dateStyle: "medium" }),
            })}
          </>
        )}
        {" · "}
        {result.cache?.hit && result.cache.stored_at
          ? t("report.cached", { time: fmt.date(result.cache.stored_at) })
          : t("report.duration", { ms: result.duration_ms })}
        {result.execution && result.execution.engine !== "orm" && (
          <>
            {" · "}
            <span title={result.execution.pre_aggregation ?? undefined}>
              {t("report.answeredBy", { engine: engineLabel(t, result.execution.engine, result.execution.pre_aggregation) })}
            </span>
            {result.execution.stale && result.execution.lag_seconds != null && (
              <> {t("report.staleBy", { minutes: fmt.number(Math.max(1, Math.round(result.execution.lag_seconds / 60))) })}</>
            )}
          </>
        )}
        {result.comparison && (
          <>
            {" · "}
            {t("report.comparedWith", {
              what: t(`compare.${result.comparison.kind}`),
              range: t("report.periodRange", {
                start: fmt.dateIn(result.comparison.start, result.calendar, { dateStyle: "medium" }),
                end: fmt.dateIn(result.comparison.end, result.calendar, { dateStyle: "medium" }),
              }),
            })}
          </>
        )}
        {jobId && <> · {t("job.fromJob")}</>}
        {" · "}
        {t("report.clickToDrill")}
      </p>
      {result.truncated && <p className="notice">{t("report.truncated", { count: result.row_count })}</p>}
      {exportError && <p className="error">{exportError}</p>}
      {body}
      {drillDialog}
    </section>
  );
}

type DrillFn = (cell: Cell, measure: string | null, comparison?: boolean) => void;

function compare(a: Scalar | null, b: Scalar | null): number {
  if (a === b) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b));
}

function Table({ result, onDrill }: { result: Result; onDrill: DrillFn }) {
  const { t, fmt } = useI18n();
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const rows = useMemo(() => {
    if (!sort) return result.rows;
    return [...result.rows].sort((a, b) => sort.dir * compare(a[sort.key] ?? null, b[sort.key] ?? null));
  }, [result.rows, sort]);
  const toggle = (key: string) =>
    setSort((s) => (s?.key !== key ? { key, dir: 1 } : s.dir === 1 ? { key, dir: -1 } : null));

  return (
    <div className="table-wrap">
      <table className="grid report-table">
        <thead>
          <tr>
            {result.columns.map((c) => (
              <th key={c.key} className={c.role === "dimension" ? "" : "num"}>
                <button type="button" className="link" onClick={() => toggle(c.key)}>
                  <bdi>{c.label}</bdi> {sort?.key === c.key ? (sort.dir === 1 ? "▲" : "▼") : ""}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {result.columns.map((c) => {
                if (c.role === "comparison" && c.comparison !== "compare") {
                  const dir = trend(row[c.key]);
                  return (
                    <td key={c.key} className={`num change ${dir ?? ""}`}>
                      <bdi>{signed(fmt.measure(row[c.key], c.format), dir)}</bdi>
                    </td>
                  );
                }
                return (
                  <td key={c.key} className={c.role === "dimension" ? "" : "num"}>
                    <button
                      type="button"
                      className="cell-link"
                      onClick={() =>
                        onDrill(cellOf(result, row), c.role === "dimension" ? null : (c.measure ?? c.key), c.role === "comparison")
                      }
                    >
                      <bdi>{c.role === "dimension" ? dimensionText(c, row[c.key] ?? null, fmt, t) : fmt.measure(row[c.key], c.format)}</bdi>
                    </button>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** "+12%" / "−3" with an arrow; ``dir`` from ``trend()``. */
function signed(text: string, dir: ReturnType<typeof trend>): string {
  if (dir === "up") return `▲ +${text}`;
  if (dir === "down") return `▼ ${text.replace(/^[-−]/, "−")}`;
  if (dir === "flat") return `■ ${text}`;
  return text;
}

function Kpi({ result, onDrill }: { result: Result; onDrill: DrillFn }) {
  const { t, fmt } = useI18n();
  const row: Row = result.rows[0] ?? {};
  return (
    <div className="kpis">
      {measureColumns(result).map((m) => {
        const previous = comparisonColumn(result, m.key, "compare");
        const pct = comparisonColumn(result, m.key, "change_pct");
        const change = comparisonColumn(result, m.key, "change");
        const dir = change ? trend(row[change.key]) : null;
        return (
          <div key={m.key} className="kpi">
            <button type="button" className="kpi-main" onClick={() => onDrill({}, m.key)}>
              <span className="kpi-label"><bdi>{m.label}</bdi></span>
              <strong className="kpi-value">{fmt.measure(row[m.key], m.format)}</strong>
            </button>
            {previous && (
              <button
                type="button"
                className={`kpi-compare ${dir ?? ""}`}
                title={previous.label}
                onClick={() => onDrill({}, m.key, true)}
              >
                <span className="kpi-change">
                  {dir === null
                    ? t("compare.noValue")
                    : signed(pct && row[pct.key] !== null ? fmt.measure(row[pct.key], "percent") : fmt.measure(row[change!.key], m.format), dir)}
                </span>
                <span className="muted">
                  {t("compare.was", { value: fmt.measure(row[previous.key], m.format) })}
                </span>
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Pivot({ result, measure, onDrill }: { result: Result; measure: Column; onDrill: DrillFn }) {
  const { t, fmt } = useI18n();
  const [rowDim, colDim] = dimensionColumns(result);
  const uniq = (key: string) => [...new Map(result.rows.map((r) => [JSON.stringify(r[key] ?? null), r[key] ?? null])).values()];
  const rowValues = uniq(rowDim.key);
  const colValues = uniq(colDim.key);
  const cells = new Map(result.rows.map((r) => [JSON.stringify([r[rowDim.key] ?? null, r[colDim.key] ?? null]), r[measure.key]]));

  return (
    <div className="table-wrap">
      <table className="grid pivot">
        <thead>
          <tr>
            <th>
              <bdi>{rowDim.label}</bdi> / {colDim.label}
            </th>
            {colValues.map((c) => (
              <th key={JSON.stringify(c)} className="num">
                <bdi>{dimensionText(colDim, c, fmt, t)}</bdi>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rowValues.map((r) => {
            const line = colValues.map((c) => cells.get(JSON.stringify([r, c])));
            return (
              <tr key={JSON.stringify(r)}>
                <th>
                  <bdi>{dimensionText(rowDim, r, fmt, t)}</bdi>
                </th>
                {colValues.map((c, i) => (
                  <td key={JSON.stringify(c)} className="num">
                    {line[i] === undefined ? (
                      <span className="muted">—</span>
                    ) : (
                      <button
                        type="button"
                        className="cell-link"
                        onClick={() => onDrill({ [rowDim.key]: r, [colDim.key]: c }, measure.key)}
                      >
                        {fmt.measure(line[i], measure.format)}
                      </button>
                    )}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
