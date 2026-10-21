import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../api";
import { type MessageKey, useI18n } from "../i18n";
import BarChart from "./BarChart";
import ScaleSection from "./ScaleSection";
import type { Check, Checks, Count, Database, Overview, TableInfo, TableKind } from "./types";

type SortKey = "name" | "model" | "kind" | "estimated_rows" | "exact" | "table_size" | "index_size" | "total_size" | "indexes";
type CountState = number | "loading" | "error";

const UNITS = ["B", "KB", "MB", "GB", "TB"] as const;
const KINDS: TableKind[] = ["model", "m2m", "unmanaged", "django", "preaggregation", "other"];
const LEVELS: Check["level_name"][] = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"];

/** "12.5 MB" in the user's digits. */
export function useBytes() {
  const { t, fmt } = useI18n();
  return useCallback(
    (value: number | null | undefined) => {
      if (value === null || value === undefined) return "—";
      let n = value;
      let unit = 0;
      while (n >= 1024 && unit < UNITS.length - 1) {
        n /= 1024;
        unit += 1;
      }
      const number = fmt.measure(n, unit === 0 ? "integer" : "number");
      return t(`system.bytes.${UNITS[unit]}` as MessageKey, { n: number });
    },
    [t, fmt],
  );
}

/** The System page: database statistics, checks, migrations, queue and usage
 * (GET /system/*, for superusers and bi_admin.view_system). */
export default function SystemPage() {
  const { t, fmt } = useI18n();
  const bytes = useBytes();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [database, setDatabase] = useState<Database | null>(null);
  const [checks, setChecks] = useState<Checks | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [counts, setCounts] = useState<Record<string, CountState>>({});
  const [countingAll, setCountingAll] = useState<{ done: number; total: number } | null>(null);

  const load = useCallback((refresh: boolean) => {
    setBusy(true);
    setError("");
    const params = refresh ? { refresh: "true" } : undefined;
    // the overview reuses the cached database statistics, so fetch those first
    api<Database>("/system/database", { params })
      .then((d) => {
        setDatabase(d);
        return Promise.all([
          api<Overview>("/system/overview", { params }).then(setOverview),
          api<Checks>("/system/checks", { params }).then(setChecks),
        ]);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => load(false), [load]);

  const count = useCallback(async (table: string) => {
    setCounts((c) => ({ ...c, [table]: "loading" }));
    try {
      const r = await api<Count>("/system/database/count", { params: { table } });
      setCounts((c) => ({ ...c, [table]: r.rows }));
    } catch {
      setCounts((c) => ({ ...c, [table]: "error" }));
    }
  }, []);

  async function countAll() {
    if (!database) return;
    const names = database.tables.map((x) => x.name);
    setCountingAll({ done: 0, total: names.length });
    for (const [i, name] of names.entries()) {
      await count(name);
      setCountingAll({ done: i + 1, total: names.length });
    }
    setCountingAll(null);
  }

  return (
    <div className="system-page">
      <div className="page-head">
        <h1>{t("system.title")}</h1>
        <span className="sys-refresh">
          {overview && (
            <span className="muted">
              {t(overview.cached ? "system.updatedCached" : "system.updated", { time: fmt.date(overview.generated_at) })}
            </span>
          )}
          <button type="button" onClick={() => load(true)} disabled={busy}>
            {busy ? t("system.refreshing") : t("system.refresh")}
          </button>
        </span>
      </div>
      {error && <p className="error">{error}</p>}
      {!overview && !error && <p className="muted">{t("app.loading")}</p>}

      {overview && (
        <>
          <Summary overview={overview} bytes={bytes} />
          {overview.database.notes.map((n) => (
            <p key={n} className="notice-warn">
              {n}
            </p>
          ))}
        </>
      )}

      {overview && <ScaleSection bytes={bytes} />}

      {database && <TableCharts database={database} counts={counts} bytes={bytes} />}

      {database && (
        <Tables
          database={database}
          counts={counts}
          onCount={count}
          onCountAll={countAll}
          countingAll={countingAll}
          bytes={bytes}
        />
      )}

      {checks && <ChecksCard checks={checks} />}

      {overview && (
        <>
          <Migrations overview={overview} />
          <div className="sys-columns">
            <Queue overview={overview} />
            <UsersCard overview={overview} />
            <CacheCard overview={overview} />
            <MediaCard overview={overview} bytes={bytes} />
          </div>
          <Activity overview={overview} />
          <Reports overview={overview} />
          <Deliveries overview={overview} />
          <details className="card sys-section">
            <summary>
              <h2>{t("system.apps", { count: overview.apps.length })}</h2>
            </summary>
            <ul className="sys-apps">
              {overview.apps.map((a) => (
                <li key={a.name}>
                  <code dir="ltr">{a.name}</code> <span className="muted">{a.verbose_name}</span>
                </li>
              ))}
            </ul>
          </details>
        </>
      )}
    </div>
  );
}

type Bytes = ReturnType<typeof useBytes>;

function Summary({ overview, bytes }: { overview: Overview; bytes: Bytes }) {
  const { t, fmt } = useI18n();
  const db = overview.database;
  const server = [db.host, db.port].filter(Boolean).join(":");
  const items: [string, string, string?][] = [
    [t("system.engine"), `${db.display_name} ${db.version ?? ""}`.trim(), db.engine],
    [t("system.databaseName"), db.name || "—", server ? t("system.server", { server }) : undefined],
    [t("system.size"), bytes(db.total_size), db.size_method ? t(`system.method.${db.size_method}` as MessageKey) : undefined],
    [t("system.tables"), fmt.number(db.table_count)],
    ["Gozar", overview.versions.gozar, `Django ${overview.versions.django} · ${overview.versions.python_implementation} ${overview.versions.python}`],
  ];
  return (
    <section className="sys-cards">
      {items.map(([label, value, sub]) => (
        <div key={label} className="card sys-card">
          <span className="muted">{label}</span>
          <strong dir="auto">{fmt.digits(value)}</strong>
          {sub && (
            <small className="muted" dir="auto">
              {sub}
            </small>
          )}
        </div>
      ))}
      {db.alias !== "default" && <p className="muted sys-alias">{t("system.alias", { alias: db.alias })}</p>}
    </section>
  );
}

function TableCharts({ database, counts, bytes }: { database: Database; counts: Record<string, CountState>; bytes: Bytes }) {
  const { t, fmt } = useI18n();
  const rows = (x: TableInfo) => (typeof counts[x.name] === "number" ? (counts[x.name] as number) : x.estimated_rows);
  const largest = database.tables
    .filter((x) => x.total_size !== null)
    .sort((a, b) => (b.total_size ?? 0) - (a.total_size ?? 0))
    .slice(0, 10);
  const most = database.tables
    .filter((x) => rows(x) !== null)
    .sort((a, b) => (rows(b) ?? 0) - (rows(a) ?? 0))
    .slice(0, 10);
  return (
    <section className="sys-columns">
      <div className="card sys-section">
        <h2>{t("system.largestTables")}</h2>
        {largest.length ? (
          <BarChart
            horizontal
            label={t("system.largestTables")}
            categories={largest.map((x) => x.name)}
            series={[{ name: t("system.totalSize"), data: largest.map((x) => x.total_size) }]}
            format={bytes}
          />
        ) : (
          <p className="muted">{t("system.noSizes")}</p>
        )}
      </div>
      <div className="card sys-section">
        <h2>{t("system.mostRows")}</h2>
        {most.length ? (
          <BarChart
            horizontal
            label={t("system.mostRows")}
            categories={most.map((x) => x.name)}
            series={[{ name: t("system.rows"), data: most.map((x) => rows(x)) }]}
            format={(v) => fmt.compact(v)}
          />
        ) : (
          <p className="muted">{t("system.noEstimates")}</p>
        )}
      </div>
    </section>
  );
}

type TablesProps = {
  database: Database;
  counts: Record<string, CountState>;
  onCount: (table: string) => void;
  onCountAll: () => void;
  countingAll: { done: number; total: number } | null;
  bytes: Bytes;
};

function Tables({ database, counts, onCount, onCountAll, countingAll, bytes }: TablesProps) {
  const { t, fmt, cap } = useI18n();
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "total_size", desc: true });
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<TableKind | "">("");
  const [open, setOpen] = useState<string | null>(null);
  const [sizes, setSizes] = useState(false); // the table and index size columns (total size shows by default)

  const value = useCallback(
    (x: TableInfo, key: SortKey): string | number | null => {
      if (key === "exact") return typeof counts[x.name] === "number" ? (counts[x.name] as number) : null;
      if (key === "indexes") return x.indexes.length;
      if (key === "model") return x.model;
      return x[key];
    },
    [counts],
  );

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = database.tables.filter(
      (x) =>
        (!kind || x.kind === kind) &&
        (!q || x.name.toLowerCase().includes(q) || (x.model ?? "").includes(q) || (x.verbose_name ?? "").toLowerCase().includes(q)),
    );
    return rows.sort((a, b) => {
      const va = value(a, sort.key);
      const vb = value(b, sort.key);
      if (va === vb) return a.name.localeCompare(b.name);
      if (va === null) return 1; // unknown values last, either direction
      if (vb === null) return -1;
      const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      return sort.desc ? -cmp : cmp;
    });
  }, [database.tables, query, kind, sort, value]);

  const head = (key: SortKey, label: string, numeric = false, className = "") => (
    <th className={`${numeric ? "num" : ""} ${className}`.trim() || undefined}>
      <button
        type="button"
        className="link"
        onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : numeric }))}
      >
        {label} {sort.key === key ? (sort.desc ? "▼" : "▲") : ""}
      </button>
    </th>
  );

  return (
    <section className="card sys-section sys-tables-card">
      <div className="sys-section-head">
        <h2>{t("system.tableList", { count: database.table_count })}</h2>
        <span className="sys-tools">
          <input dir="auto" type="search" placeholder={t("system.filterTables")} value={query} onChange={(e) => setQuery(e.target.value)} />
          <select value={kind} onChange={(e) => setKind(e.target.value as TableKind | "")} aria-label={t("system.kind")}>
            <option value="">{t("system.allKinds")}</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {t(`system.kind.${k}` as MessageKey)}
              </option>
            ))}
          </select>
          <button type="button" onClick={onCountAll} disabled={!!countingAll}>
            {countingAll
              ? t("system.counting", { done: countingAll.done, total: countingAll.total })
              : t("system.countAll")}
          </button>
          <label className="sys-toggle">
            <input type="checkbox" checked={sizes} onChange={(e) => setSizes(e.target.checked)} /> {t("system.sizeColumns")}
          </label>
        </span>
      </div>
      <p className="muted">{t("system.estimateHelp")}</p>
      <div className="table-wrap">
        <table className={sizes ? "grid sys-tables" : "grid sys-tables no-sizes"}>
          <thead>
            <tr>
              {head("name", t("system.table"))}
              {head("model", t("system.model"), false, "sys-col-model")}
              {head("kind", t("system.kind"), false, "sys-col-kind")}
              {head("estimated_rows", t("system.estimatedRows"), true, "sys-col-estimate")}
              {head("exact", t("system.exactRows"), true)}
              {head("table_size", t("system.tableSize"), true, "sys-col-size")}
              {head("index_size", t("system.indexSize"), true, "sys-col-size")}
              {head("total_size", t("system.totalSize"), true)}
              {head("indexes", t("system.indexes"), true)}
            </tr>
          </thead>
          <tbody>
            {shown.map((x) => {
              const c = counts[x.name];
              return (
                <Fragment key={x.name}>
                  <tr>
                    <td>
                      <code dir="ltr" className="sys-name" title={x.name}>
                        {x.name}
                      </code>
                    </td>
                    <td className="sys-col-model">
                      {x.model ? (
                        <span title={x.model}>
                          <bdi>{cap(x.verbose_name ?? x.model)}</bdi> <small className="muted"><bdi>{x.app_verbose_name ?? x.app}</bdi></small>
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="sys-col-kind">
                      <span className={`badge kind-${x.kind}`}>{t(`system.kind.${x.kind}` as MessageKey)}</span>
                    </td>
                    <td className="num sys-col-estimate">{x.estimated_rows === null ? "—" : fmt.number(x.estimated_rows)}</td>
                    <td className="num">
                      {typeof c === "number" ? (
                        fmt.number(c)
                      ) : c === "loading" ? (
                        <span className="muted">…</span>
                      ) : (
                        <button type="button" className="link" onClick={() => onCount(x.name)}>
                          {c === "error" ? t("system.countFailed") : t("system.count")}
                        </button>
                      )}
                    </td>
                    <td className="num sys-col-size">{bytes(x.table_size)}</td>
                    <td className="num sys-col-size">{bytes(x.index_size)}</td>
                    <td className="num" title={t("system.sizeBreakdown", { table: bytes(x.table_size), indexes: bytes(x.index_size) })}>
                      {bytes(x.total_size)}
                    </td>
                    <td className="num">
                      {x.indexes.length ? (
                        <button type="button" className="link" onClick={() => setOpen(open === x.name ? null : x.name)} aria-expanded={open === x.name}>
                          {fmt.number(x.indexes.length)} {open === x.name ? "▴" : "▾"}
                        </button>
                      ) : (
                        fmt.number(0)
                      )}
                    </td>
                  </tr>
                  {open === x.name && (
                    <tr className="sys-indexes">
                      <td colSpan={9}>
                        <table className="grid">
                          <thead>
                            <tr>
                              <th>{t("system.index")}</th>
                              <th>{t("system.columns")}</th>
                              <th>{t("system.indexFlags")}</th>
                              <th className="num">{t("system.size")}</th>
                            </tr>
                          </thead>
                          <tbody>
                            {x.indexes.map((i) => (
                              <tr key={i.name}>
                                <td>
                                  <code dir="ltr">{i.name}</code>
                                </td>
                                <td>
                                  <code dir="ltr">{i.columns.join(", ") || "—"}</code>
                                </td>
                                <td>
                                  {i.primary && <span className="badge">{t("system.primary")}</span>}{" "}
                                  {i.unique && <span className="badge">{t("system.unique")}</span>}
                                </td>
                                <td className="num">{bytes(i.size)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
        {!shown.length && <p className="empty">{t("system.noTables")}</p>}
      </div>
    </section>
  );
}

function ChecksCard({ checks }: { checks: Checks }) {
  const { t, fmt } = useI18n();
  const groups = LEVELS.map((level) => [level, checks.checks.filter((c) => c.level_name === level)] as const).filter(
    ([, items]) => items.length,
  );
  return (
    <section className="card sys-section">
      <h2>{t("system.checks")}</h2>
      {!checks.checks.length && <p className="notice">{t("system.checksOk")}</p>}
      {groups.map(([level, items]) => (
        <div key={level} className="sys-check-group">
          <h3>
            <span className={`badge level-${level.toLowerCase()}`}>{t(`system.level.${level}` as MessageKey)}</span>{" "}
            <span className="muted">{fmt.number(items.length)}</span>
          </h3>
          <ul className="sys-checks">
            {items.map((c, i) => (
              <li key={`${c.id}-${i}`}>
                <code dir="ltr">{c.id}</code> {c.deployment && <span className="badge">{t("system.deployment")}</span>}
                <p dir="ltr">{c.message}</p>
                {c.hint && (
                  <p className="muted" dir="ltr">
                    {c.hint}
                  </p>
                )}
                {c.object && (
                  <small className="muted" dir="ltr">
                    {c.object}
                  </small>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
      <p className="muted">{t("system.checksHelp")}</p>
    </section>
  );
}

function Migrations({ overview }: { overview: Overview }) {
  const { t, fmt } = useI18n();
  return (
    <section className="card sys-section">
      <h2>{t("system.migrations")}</h2>
      {overview.migrations.map((m) => (
        <div key={m.alias}>
          {overview.migrations.length > 1 && <h3>{t("system.alias", { alias: m.alias })}</h3>}
          {m.error ? (
            <p className="error">{m.error}</p>
          ) : m.unapplied.length ? (
            <>
              <p className="error">{t("system.unapplied", { count: m.unapplied.length })}</p>
              <ul>
                {m.unapplied.map((u) => (
                  <li key={`${u.app}.${u.name}`}>
                    <code dir="ltr">
                      {u.app}.{u.name}
                    </code>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="notice">{t("system.allApplied", { count: fmt.number(m.applied ?? 0) })}</p>
          )}
          {m.conflicts.map((c) => (
            <p key={c.app} className="error">
              {t("system.conflict", { app: c.app, names: c.names.join(", ") })}
            </p>
          ))}
        </div>
      ))}
    </section>
  );
}

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <>
      <dt>{label}</dt>
      <dd className={warn ? "error" : undefined}>{value}</dd>
    </>
  );
}

function Queue({ overview }: { overview: Overview }) {
  const { t, fmt } = useI18n();
  const j = overview.jobs;
  return (
    <section className="card sys-section">
      <h2>{t("system.jobs")}</h2>
      <dl className="sys-stats">
        <Stat label={t("system.jobs.queued")} value={fmt.number(j.queued)} />
        <Stat label={t("system.jobs.running")} value={fmt.number(j.running)} />
        <Stat label={t("system.jobs.stale")} value={fmt.number(j.stale)} warn={j.stale > 0} />
        <Stat label={t("system.jobs.failed")} value={fmt.number(j.failed)} warn={j.failed > 0} />
        <Stat label={t("system.jobs.done")} value={fmt.number(j.done)} />
        <Stat
          label={t("system.jobs.heartbeat")}
          value={j.last_heartbeat ? fmt.date(j.last_heartbeat) : t("system.never")}
        />
        {j.oldest_queued && <Stat label={t("system.jobs.oldestQueued")} value={fmt.date(j.oldest_queued)} />}
      </dl>
      <p className="muted">{t("system.jobs.help", { seconds: j.lease_seconds })}</p>
    </section>
  );
}

function UsersCard({ overview }: { overview: Overview }) {
  const { t, fmt } = useI18n();
  const u = overview.users;
  const n = (v: number | null) => (v === null ? "—" : fmt.number(v));
  return (
    <section className="card sys-section">
      <h2>{t("system.users")}</h2>
      <dl className="sys-stats">
        <Stat label={t("system.users.total")} value={n(u.total)} />
        <Stat label={t("system.users.active")} value={n(u.active)} />
        <Stat label={t("system.users.staff")} value={n(u.staff)} />
        <Stat label={t("system.users.superusers")} value={n(u.superusers)} />
        <Stat label={t("system.users.sessions")} value={u.sessions === null ? t("system.unavailable") : n(u.sessions)} />
      </dl>
    </section>
  );
}

function CacheCard({ overview }: { overview: Overview }) {
  const { t, fmt } = useI18n();
  const c = overview.cache_check;
  return (
    <section className="card sys-section">
      <h2>{t("system.cache")}</h2>
      <p className={c.ok ? "notice" : "error"}>
        {c.ok
          ? t("system.cacheOk", { ms: fmt.measure(c.duration_ms, "number") })
          : t("system.cacheFailed", { error: c.error ?? "—" })}
      </p>
      <ul className="sys-plain">
        {overview.caches.map((x) => (
          <li key={x.alias}>
            <strong dir="ltr">{x.alias}</strong> <code dir="ltr">{x.backend.split(".").pop()}</code>
            {x.reports && <span className="badge">{t("system.cacheReports")}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function MediaCard({ overview, bytes }: { overview: Overview; bytes: Bytes }) {
  const { t, fmt } = useI18n();
  const m = overview.media;
  return (
    <section className="card sys-section">
      <h2>{t("system.media")}</h2>
      <dl className="sys-stats">
        <Stat label={t("system.size")} value={bytes(m.bytes)} />
        <Stat label={t("system.media.files")} value={m.files === null ? "—" : fmt.number(m.files)} />
        <Stat label={t("system.media.storage")} value={m.backend.split(".").pop() ?? m.backend} />
      </dl>
      <p className="muted">
        {m.method ? t(`system.media.${m.method}` as MessageKey) : t("system.unavailable")}
        {m.truncated && ` ${t("system.media.truncated")}`}
      </p>
    </section>
  );
}

function Activity({ overview }: { overview: Overview }) {
  const { t, fmt, calendar } = useI18n();
  const a = overview.activity;
  const total = Object.values(a.totals).reduce((s, v) => s + v, 0);
  return (
    <section className="card sys-section">
      <h2>{t("system.activity", { days: a.days })}</h2>
      {total ? (
        <BarChart
          stacked
          label={t("system.activity", { days: a.days })}
          categories={a.series.map((d) => fmt.dateIn(String(d.date), calendar, { month: "short", day: "numeric" }))}
          series={a.actions.map((act) => ({ name: act.label, data: a.series.map((d) => Number(d[act.value] ?? 0)) }))}
          format={(v) => fmt.number(v)}
          height={240}
        />
      ) : (
        <p className="muted">{t("system.noActivity")}</p>
      )}
    </section>
  );
}

function Reports({ overview }: { overview: Overview }) {
  const { t, fmt } = useI18n();
  const r = overview.reports;
  const ms = (v: number | null) => (v === null ? "—" : t("system.ms", { ms: fmt.number(v) }));
  const table = (title: string, rows: typeof r.most_used) => (
    <div>
      <h3>{title}</h3>
      {rows.length ? (
        <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th>{t("system.reports.model")}</th>
              <th>{t("system.reports.kind")}</th>
              <th className="num">{t("system.reports.count")}</th>
              <th className="num">{t("system.reports.avg")}</th>
              <th className="num">{t("system.reports.max")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((x) => (
              <tr key={`${x.semantic_model}-${x.kind}`}>
                <td>
                  <code dir="ltr">{x.semantic_model}</code>
                </td>
                <td>{x.kind_display}</td>
                <td className="num">
                  {fmt.number(x.count)}
                  {x.failed > 0 && <small className="error"> ({t("system.reports.failed", { count: x.failed })})</small>}
                </td>
                <td className="num">{ms(x.avg_ms)}</td>
                <td className="num">{ms(x.max_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : (
        <p className="muted">{t("system.reports.none")}</p>
      )}
    </div>
  );
  return (
    <section className="card sys-section">
      <h2>{t("system.reports", { days: r.days })}</h2>
      <p className="muted">{t("system.reports.summary", { total: r.total, failed: r.failed })}</p>
      <div className="sys-columns">
        {table(t("system.reports.mostUsed"), r.most_used)}
        {table(t("system.reports.slowest"), r.slowest)}
      </div>
    </section>
  );
}

function Deliveries({ overview }: { overview: Overview }) {
  const { t, fmt } = useI18n();
  return (
    <section className="card sys-section">
      <h2>{t("system.deliveries")}</h2>
      {overview.deliveries.length ? (
        <div className="table-wrap">
          <table className="grid">
            <thead>
              <tr>
                <th>{t("system.deliveries.time")}</th>
                <th>{t("system.deliveries.schedule")}</th>
                <th>{t("system.deliveries.recipient")}</th>
                <th>{t("system.deliveries.status")}</th>
              </tr>
            </thead>
            <tbody>
              {overview.deliveries.map((d) => (
                <tr key={d.id}>
                  <td>{fmt.date(d.timestamp)}</td>
                  <td>
                    <bdi>{d.schedule || "—"}</bdi>
                  </td>
                  <td>
                    <bdi>{d.user ?? "—"}</bdi>
                  </td>
                  <td className={`status-${d.status}`} title={d.error || undefined}>
                    {d.status_display}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">{t("system.deliveries.none")}</p>
      )}
    </section>
  );
}
