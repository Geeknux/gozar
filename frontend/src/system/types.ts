/** Shapes of GET /system/overview, /system/database, /system/checks. */

export type TableKind = "model" | "m2m" | "unmanaged" | "django" | "other" | "preaggregation";

export type IndexInfo = { name: string; columns: string[]; unique: boolean; primary: boolean; size: number | null };

export type TableInfo = {
  name: string;
  schema?: string;
  model: string | null;
  app: string | null;
  app_verbose_name: string | null;
  verbose_name: string | null;
  kind: TableKind;
  estimated_rows: number | null;
  table_size: number | null;
  index_size: number | null;
  total_size: number | null;
  seq_scans?: number | null;
  index_scans?: number | null;
  last_vacuum?: string | null;
  last_analyze?: string | null;
  indexes: IndexInfo[];
};

export type DatabaseSummary = {
  alias: string;
  vendor: string;
  engine: string;
  display_name: string;
  version: string | null;
  name: string;
  host: string | null;
  port: string | null;
  size_method: "postgresql" | "dbstat" | "page_count" | null;
  total_size: number | null;
  table_count: number;
  notes: string[];
  duration_ms: number;
};

export type Database = DatabaseSummary & { tables: TableInfo[]; generated_at: string; cached: boolean };

export type ReportUsage = {
  semantic_model: string;
  kind: string;
  kind_display: string;
  count: number;
  failed: number;
  cache_hits: number;
  avg_ms: number | null;
  max_ms: number | null;
  last: string | null;
};

export type Overview = {
  generated_at: string;
  cached: boolean;
  database: DatabaseSummary;
  versions: { gozar: string; django: string; python: string; python_implementation: string };
  apps: { name: string; label: string; verbose_name: string }[];
  caches: { alias: string; backend: string; reports: boolean }[];
  cache_check: { alias: string; backend: string; ok: boolean; error: string | null; duration_ms: number };
  jobs: {
    queued: number;
    running: number;
    done: number;
    failed: number;
    cancelled: number;
    stale: number;
    last_heartbeat: string | null;
    oldest_queued: string | null;
    lease_seconds: number;
  };
  users: {
    total: number;
    active: number | null;
    staff: number | null;
    superusers: number | null;
    sessions: number | null;
    session_engine: string;
  };
  activity: {
    days: number;
    series: ({ date: string } & Record<string, number | string>)[];
    totals: Record<string, number>;
    actions: { value: string; label: string }[];
  };
  reports: { days: number; total: number; failed: number; most_used: ReportUsage[]; slowest: ReportUsage[] };
  deliveries: {
    id: number;
    timestamp: string;
    schedule: string;
    schedule_id: number | null;
    user: string | null;
    status: "sent" | "failed" | "skipped";
    status_display: string;
    trigger: string;
    error: string;
  }[];
  media: {
    backend: string;
    url: string | null;
    method: "filesystem" | "referenced" | null;
    bytes: number | null;
    files: number | null;
    truncated: boolean;
  };
  migrations: {
    alias: string;
    applied?: number;
    unapplied: { app: string; name: string }[];
    conflicts: { app: string; names: string[] }[];
    error: string | null;
  }[];
};

export type Check = {
  level: number;
  level_name: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  id: string | null;
  message: string;
  hint: string | null;
  object: string | null;
  deployment: boolean;
};

export type Checks = { checks: Check[]; counts: Record<string, number>; debug: boolean; generated_at: string; cached: boolean };

export type Count = { table: string; alias: string; rows: number; duration_ms: number };

/** GET /system/scale (M7). */
export type Freshness = { fresh: boolean; stale_since: string | null; lag_seconds: number | null };

export type MaterializationState = {
  status: "empty" | "ready" | "failed";
  table: string;
  generation: number;
  rows: number | null;
  size_bytes: number | null;
  built_at: string | null;
  refreshed_at: string | null;
  refresh_mode: string;
  duration_ms: number | null;
  freshness: Freshness | null;
  needs_rebuild: boolean;
  dirty_from: string | null;
  dirty_to: string | null;
  hits: number;
  last_hit_at: string | null;
  last_error: string;
  building: boolean;
  window: [string, string] | null;
  definition_current?: boolean;
};

export type PreAggregationInfo = {
  key: string;
  name: string;
  semantic_model: string;
  label: { en: string; fa: string };
  description: { en: string; fa: string };
  measures: string[];
  dimensions: string[];
  time_dimension: string | null;
  time_grain: string | null;
  calendar: "gregorian" | "jalali" | null;
  scope_fields: string[];
  incremental_window: number | null;
  refresh_every: number;
  rebuild_every: number;
  max_staleness: number;
  state: MaterializationState | null;
  due: "rebuild" | "auto" | null;
};

export type ColumnarInfo = {
  name: string;
  label: string;
  installed?: boolean;
  configured?: boolean;
  file?: string | null;
  exists?: boolean;
  size_bytes?: number | null;
  semantic_models?: string[];
  max_staleness?: number;
  sync_every?: number;
  in_use: boolean;
  error?: string;
  state?: {
    status: string;
    synced_at: string | null;
    duration_ms: number | null;
    rows: number | null;
    tables: Record<string, number>;
    last_error: string;
    hits: number;
    last_hit_at: string | null;
    freshness: Freshness | null;
  } | null;
};

export type ScaleJob = {
  id: string;
  kind: "preaggregate" | "columnar_sync";
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  target: string;
  mode: string | null;
  user: string | null;
  created_at: string;
  finished_at: string | null;
  error: { detail?: string; code?: string } | null;
  outcome: { rows?: number; mode?: string; duration_ms?: number } | null;
};

export type Scale = {
  enabled: boolean;
  database: string;
  worker_refreshes: boolean;
  preaggregations: PreAggregationInfo[];
  columnar: ColumnarInfo[];
  jobs: ScaleJob[];
};
