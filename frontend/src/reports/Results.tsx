import { useMemo, useState } from "react";

import { ApiError } from "../api";
import { config } from "../config";
import { type MessageKey, useI18n } from "../i18n";
import Chart from "./Chart";
import Drilldown from "./Drilldown";
import {
  type Cell,
  CHARTS,
  type ChartType,
  type Column,
  cellOf,
  dimensionColumns,
  dimensionText,
  measureColumns,
  type Result,
  type Row,
  type Scalar,
  type Spec,
} from "./model";

type Props = {
  result: Result;
  spec: Spec; // the spec that produced ``result`` (for drill-down and export)
  jobId?: string; // set when a background job produced it: export its stored result
  chart: ChartType;
  onChart: (chart: ChartType) => void;
};

type Drill = { cell: Cell; measure: string | null; title: string };

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export default function Results({ result, spec, jobId, chart, onChart }: Props) {
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
  const open = (cell: Cell, measureKey: string | null) => setDrill({ cell, measure: measureKey, title: describe(cell, measureKey) });

  async function download(format: "csv" | "xlsx") {
    setExportError("");
    try {
      const resp = jobId
        ? await fetch(`${config.apiBase}/reports/jobs/${jobId}/export?format=${format}`, { credentials: "same-origin" })
        : await fetch(`${config.apiBase}/reports/export?format=${format}`, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
            body: JSON.stringify(spec),
          });
      if (!resp.ok) throw new ApiError(resp.status, await resp.json().catch(() => ({})));
      const name = /filename="([^"]+)"/.exec(resp.headers.get("Content-Disposition") ?? "")?.[1] ?? `report.${format}`;
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

  return (
    <section className="results card">
      <div className="results-head">
        <div className="segmented chart-switch" role="group">
          {CHARTS.map((c) => (
            <button key={c} type="button" className={c === chart ? "active" : ""} aria-pressed={c === chart} onClick={() => onChart(c)}>
              {t(`chart.${c}` as MessageKey)}
            </button>
          ))}
        </div>
        <div className="results-actions">
          {needsMeasurePicker && (
            <select value={measure.key} aria-label={t("report.pivotMeasure")} onChange={(e) => setMeasureKey(e.target.value)}>
              {measures.map((m) => (
                <option key={m.key} value={m.key}>
                  {m.label}
                </option>
              ))}
            </select>
          )}
          <span className="muted">{t("report.export")}:</span>
          <button type="button" onClick={() => download("csv")}>
            {t("report.exportCsv")}
          </button>
          <button type="button" onClick={() => download("xlsx")}>
            {t("report.exportXlsx")}
          </button>
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
        {jobId && <> · {t("job.fromJob")}</>}
        {" · "}
        {t("report.clickToDrill")}
      </p>
      {result.truncated && <p className="notice">{t("report.truncated", { count: result.row_count })}</p>}
      {exportError && <p className="error">{exportError}</p>}
      {body}
      {drill && (
        <Drilldown spec={spec} cell={drill.cell} measure={drill.measure} title={drill.title} onClose={() => setDrill(null)} />
      )}
    </section>
  );
}

type DrillFn = (cell: Cell, measure: string | null) => void;

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
              <th key={c.key} className={c.role === "measure" ? "num" : ""}>
                <button type="button" className="link" onClick={() => toggle(c.key)}>
                  {c.label} {sort?.key === c.key ? (sort.dir === 1 ? "▲" : "▼") : ""}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {result.columns.map((c) => (
                <td key={c.key} className={c.role === "measure" ? "num" : ""}>
                  <button
                    type="button"
                    className="cell-link"
                    onClick={() => onDrill(cellOf(result, row), c.role === "measure" ? c.key : null)}
                  >
                    <bdi>{c.role === "measure" ? fmt.measure(row[c.key], c.format) : dimensionText(c, row[c.key] ?? null, fmt, t)}</bdi>
                  </button>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Kpi({ result, onDrill }: { result: Result; onDrill: DrillFn }) {
  const { fmt } = useI18n();
  const row: Row = result.rows[0] ?? {};
  return (
    <div className="kpis">
      {measureColumns(result).map((m) => (
        <button key={m.key} type="button" className="kpi" onClick={() => onDrill({}, m.key)}>
          <span className="kpi-label">{m.label}</span>
          <strong className="kpi-value">{fmt.measure(row[m.key], m.format)}</strong>
        </button>
      ))}
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
              {rowDim.label} / {colDim.label}
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
