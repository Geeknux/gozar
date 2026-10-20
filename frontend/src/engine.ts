import type { MessageKey } from "./i18n";

type T = (key: MessageKey, params?: Record<string, string | number>) => string;

/** "Pre-aggregation daily_sales", "DuckDB", "Database (ORM)" for an engine name. */
export function engineLabel(t: T, engine: string, preAggregation?: string | null): string {
  if (engine === "preaggregation") return t("engine.preaggregation", { name: preAggregation ?? "" });
  if (engine === "duckdb") return t("engine.duckdb");
  if (engine === "orm" || !engine) return t("engine.orm");
  return engine;
}
