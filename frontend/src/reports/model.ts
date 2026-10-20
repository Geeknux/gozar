/** Report types (mirroring bi_admin.engine.spec / runner) and pure helpers. */
import { partsOf } from "../calendar";
import type { CalendarName, Format, MessageKey } from "../i18n";

export type Scalar = string | number | boolean;
export type Op = "eq" | "neq" | "in" | "not_in" | "gte" | "lte" | "between" | "contains" | "is_null";
export type Grain = "day" | "week" | "month" | "season" | "year";
export type ChartType = "table" | "bar" | "line" | "area" | "pie" | "pivot" | "kpi";
export type CompareTo = "previous_period" | "same_period_last_year";

export type Filter = { field: string; op: Op; value?: Scalar | Scalar[] | null };
export type Sort = { field: string; direction: "asc" | "desc" };

export type Spec = {
  semantic_model: string;
  measures: string[];
  dimensions?: string[];
  filters?: Filter[];
  time_dimension?: string | null;
  time_grain?: Grain | null;
  calendar?: CalendarName | null;
  relative_period?: string | null;
  compare_to?: CompareTo | null;
  sort?: Sort[];
  limit?: number | null;
  chart?: ChartType;
};

export type Choice = { value: Scalar; label: string };

export type CatalogDimension = {
  key: string;
  type: "time" | "category" | "number" | "boolean" | "geo";
  label: string;
  description: string;
  synonyms: string[];
  choices?: Choice[];
  grains?: Grain[];
};

export type CatalogMeasure = { key: string; label: string; description: string; format: string };

export type CatalogModel = {
  name: string;
  model: string;
  label: string;
  description: string;
  time_dimension: string | null;
  dimensions: CatalogDimension[];
  measures: CatalogMeasure[];
};

export type Column = {
  key: string;
  role: "dimension" | "measure" | "comparison";
  /** comparison columns: the measure compared and which part this is */
  measure?: string;
  comparison?: "compare" | "change" | "change_pct";
  type?: CatalogDimension["type"];
  label: string;
  format?: string;
  grain?: Grain;
  calendar?: CalendarName;
  label_key?: string;
  choices?: Choice[];
};

export type Row = Record<string, Scalar | null>;

export type Period = { name: string; calendar: CalendarName; start: string; end: string };

export type Result = {
  semantic_model: string;
  spec: Spec;
  chart: ChartType;
  calendar: CalendarName;
  time_dimension: string | null;
  time_grain: Grain | null;
  period: Period | null;
  compare_to: CompareTo | null;
  comparison: (Period & { kind: CompareTo }) | null;
  columns: Column[];
  rows: Row[];
  row_count: number;
  truncated: boolean;
  duration_ms: number;
  cache: { enabled: boolean; hit: boolean; stored_at: string | null };
  execution?: Execution;
};

/** Which engine computed the rows (M7): the ORM, a pre-aggregation or a columnar copy. */
export type Execution = {
  engine: string;
  pre_aggregation: string | null;
  stale?: boolean;
  stale_since?: string | null;
  lag_seconds?: number | null;
  considered?: { engine: string; code: string; detail: string; pre_aggregation?: string }[];
};

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

/** A background report job (POST /reports/run answers 202 with one). */
export type Job = {
  id: string;
  status: JobStatus;
  status_display: string;
  semantic_model: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: { detail?: string; code?: string } | null;
  error_status: number | null;
};

export type SpecError = {
  code: string;
  message: string;
  path: string;
  value: unknown;
  suggestions: string[];
};

export type Cell = Record<string, Scalar | null>;

export const CHARTS: ChartType[] = ["table", "bar", "line", "area", "pie", "pivot", "kpi"];
export const COMPARE_TO: CompareTo[] = ["previous_period", "same_period_last_year"];
export const GRAINS: Grain[] = ["day", "week", "month", "season", "year"];
export const PERIODS = [
  "today",
  "yesterday",
  "this_week",
  "last_week",
  "past_7_days",
  "past_30_days",
  "this_month",
  "last_month",
  "last_3_months",
  "past_12_months",
  "this_season",
  "last_season",
  "this_year",
  "last_year",
] as const;

export function opsFor(type: CatalogDimension["type"]): Op[] {
  switch (type) {
    case "time":
      return ["between", "gte", "lte", "eq", "is_null"];
    case "number":
      return ["eq", "neq", "gte", "lte", "between", "in", "not_in", "is_null"];
    case "boolean":
      return ["eq", "is_null"];
    default:
      return ["eq", "neq", "in", "not_in", "contains", "is_null"];
  }
}

/** A sensible empty value when a filter's operator changes. */
export function defaultValue(op: Op): Filter["value"] {
  if (op === "is_null") return true;
  if (op === "in" || op === "not_in") return [];
  if (op === "between") return ["", ""];
  return "";
}

// --- URL state ----------------------------------------------------------------------

/** The spec lives in the URL (?spec=<json>) so a report can be shared and reloaded. */
export function specFromParam(raw: string | null): Spec | null {
  if (!raw) return null;
  try {
    const spec = JSON.parse(raw) as Spec;
    return spec && typeof spec.semantic_model === "string" && Array.isArray(spec.measures) ? spec : null;
  } catch {
    return null;
  }
}

/** Drop empty/default keys so URLs stay short and readable. */
export function compact(spec: Spec): Spec {
  const out: Spec = { semantic_model: spec.semantic_model, measures: spec.measures };
  if (spec.dimensions?.length) out.dimensions = spec.dimensions;
  if (spec.filters?.length) out.filters = spec.filters;
  if (spec.time_dimension) out.time_dimension = spec.time_dimension;
  if (spec.time_grain) out.time_grain = spec.time_grain;
  if (spec.calendar) out.calendar = spec.calendar;
  if (spec.relative_period) out.relative_period = spec.relative_period;
  if (spec.compare_to) out.compare_to = spec.compare_to;
  if (spec.sort?.length) out.sort = spec.sort;
  if (spec.limit !== undefined && spec.limit !== null && spec.limit !== 1000) out.limit = spec.limit;
  if (spec.chart && spec.chart !== "table") out.chart = spec.chart;
  return out;
}

/** What determines the data (the chart type only changes the presentation). */
export const dataKey = (spec: Spec) => JSON.stringify({ ...compact(spec), chart: undefined });

// --- display ------------------------------------------------------------------------

type T = (key: MessageKey, params?: Record<string, string | number>) => string;

/** Human label for a time bucket that starts on ``iso`` (in the report's calendar). */
export function bucketLabel(iso: string, grain: Grain, calendar: CalendarName, fmt: Format, t: T): string {
  switch (grain) {
    case "day":
      return fmt.dateIn(iso, calendar, { dateStyle: "medium" });
    case "week":
      return t("report.weekOf", { date: fmt.dateIn(iso, calendar, { month: "short", day: "numeric", year: "numeric" }) });
    case "month":
      return fmt.dateIn(iso, calendar, { month: "short", year: "numeric" });
    case "season": {
      const { y, m } = partsOf(iso, calendar);
      const n = Math.floor((m - 1) / 3) + 1;
      if (calendar === "jalali") return `${t(`season.${n}` as MessageKey)} ${fmt.digits(y)}`;
      return t("report.quarter", { q: fmt.digits(n), year: fmt.digits(y) });
    }
    default:
      return fmt.digits(partsOf(iso, calendar).y);
  }
}

/** Display text for a dimension value in a result row. */
export function dimensionText(col: Column, value: Scalar | null, fmt: Format, t: T): string {
  if (value === null || value === undefined) return t("report.null");
  if (col.grain && typeof value === "string") return bucketLabel(value, col.grain, col.calendar ?? "gregorian", fmt, t);
  if (col.choices) {
    const choice = col.choices.find((c) => c.value === value);
    if (choice) return choice.label;
  }
  if (typeof value === "boolean") return t(value ? "common.yes" : "common.no");
  if (typeof value === "number") return fmt.number(value);
  return fmt.digits(String(value));
}

export const dimensionColumns = (r: Result) => r.columns.filter((c) => c.role === "dimension");
export const measureColumns = (r: Result) => r.columns.filter((c) => c.role === "measure");

/** The comparison column of ``measure`` for ``part`` (when the report has a comparison). */
export const comparisonColumn = (r: Result, measure: string, part: "compare" | "change" | "change_pct") =>
  r.columns.find((c) => c.role === "comparison" && c.measure === measure && c.comparison === part);

/** "up" | "down" | "flat" for a change value (null: no comparison value). */
export function trend(value: Scalar | null | undefined): "up" | "down" | "flat" | null {
  if (value === null || value === undefined || typeof value !== "number") return null;
  return value > 0 ? "up" : value < 0 ? "down" : "flat";
}

/** The group values of a row — the cell to drill into. */
export function cellOf(r: Result, row: Row, keys?: string[]): Cell {
  const cell: Cell = {};
  for (const c of dimensionColumns(r)) if (!keys || keys.includes(c.key)) cell[c.key] = row[c.key] ?? null;
  return cell;
}
