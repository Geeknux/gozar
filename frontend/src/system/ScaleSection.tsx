import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "../api";
import { type MessageKey, useI18n } from "../i18n";
import type { ColumnarInfo, Freshness, MaterializationState, PreAggregationInfo, Scale, ScaleJob } from "./types";

type Bytes = (value: number | null | undefined) => string;
type Running = Record<string, ScaleJob>; // target → its latest job while it runs

const FINISHED = new Set(["done", "failed", "cancelled"]);

/** System → Scale (M7): pre-aggregations and columnar copies, with refresh,
 * rebuild and sync buttons that queue jobs (GET/POST /system/scale/*). */
export default function ScaleSection({ bytes }: { bytes: Bytes }) {
  const { t } = useI18n();
  const [scale, setScale] = useState<Scale | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState<Running>({});
  const [notice, setNotice] = useState<{ target: string; text: string; failed: boolean } | null>(null);
  const timers = useRef<number[]>([]);

  const load = useCallback(() => {
    api<Scale>("/system/scale")
      .then((s) => {
        setScale(s);
        setError("");
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    const list = timers.current;
    return () => list.forEach((id) => window.clearTimeout(id));
  }, [load]);

  const follow = useCallback(
    (job: ScaleJob, delay = 500) => {
      if (FINISHED.has(job.status)) {
        setRunning((r) => {
          const next = { ...r };
          delete next[job.target];
          return next;
        });
        const failed = job.status !== "done";
        setNotice({
          target: job.target,
          failed,
          text: failed
            ? t("scale.jobFailed", { detail: job.error?.detail ?? job.status })
            : t("scale.jobDone", { rows: job.outcome?.rows ?? 0, ms: job.outcome?.duration_ms ?? 0 }),
        });
        load();
        return;
      }
      setRunning((r) => ({ ...r, [job.target]: job }));
      const id = window.setTimeout(() => {
        api<ScaleJob>(`/system/scale/jobs/${job.id}`)
          .then((next) => follow(next, Math.min(delay * 1.5, 3000)))
          .catch((e: Error) => setNotice({ target: job.target, failed: true, text: e.message }));
      }, delay);
      timers.current.push(id);
    },
    [load, t],
  );

  async function submit(path: string, target: string) {
    setNotice(null);
    try {
      const { job } = await api<{ job: ScaleJob }>(path, { method: "POST" });
      follow(job);
    } catch (e) {
      setNotice({ target, failed: true, text: e instanceof ApiError ? e.message : String(e) });
    }
  }

  return (
    <section className="card sys-section sys-scale">
      <div className="sys-section-head">
        <h2>{t("scale.title")}</h2>
        <button type="button" onClick={load}>
          {t("system.refresh")}
        </button>
      </div>
      <p className="muted">{t("scale.help")}</p>
      {error && <p className="error">{error}</p>}
      {!scale && !error && <p className="muted">{t("app.loading")}</p>}
      {scale && (
        <>
          {!scale.enabled && <p className="notice-warn">{t("scale.disabled")}</p>}
          {!scale.worker_refreshes && <p className="notice-warn">{t("scale.noWorker")}</p>}
          <h3>{t("scale.preaggregations")}</h3>
          {scale.preaggregations.length ? (
            <PreAggregations
              items={scale.preaggregations}
              running={running}
              bytes={bytes}
              onRefresh={(p, mode) =>
                submit(`/system/scale/preaggregations/${encodeURIComponent(p.key)}/refresh?mode=${mode}`, p.key)
              }
            />
          ) : (
            <p className="empty">{t("scale.noPreaggregations")}</p>
          )}
          {notice && <p className={notice.failed ? "error" : "notice"}>{notice.text}</p>}
          <h3>{t("scale.columnar")}</h3>
          <div className="sys-columns">
            {scale.columnar.map((c) => (
              <ColumnarCard
                key={c.name}
                info={c}
                bytes={bytes}
                job={running[c.name]}
                onSync={() => submit(`/system/scale/columnar/${encodeURIComponent(c.name)}/sync`, c.name)}
              />
            ))}
          </div>
          <Jobs jobs={scale.jobs} />
        </>
      )}
    </section>
  );
}

function FreshBadge({ state, freshness }: { state?: MaterializationState | null; freshness?: Freshness | null }) {
  const { t, fmt } = useI18n();
  if (state && state.status === "failed") return <span className="badge status-failed">{t("scale.failed")}</span>;
  if (state && state.definition_current === false) return <span className="badge status-invalid">{t("scale.definitionChanged")}</span>;
  if (!freshness) return <span className="badge">{t("scale.notBuilt")}</span>;
  if (freshness.fresh) return <span className="badge status-ok">{t("scale.fresh")}</span>;
  return (
    <span className="badge status-invalid" title={freshness.stale_since ? fmt.date(freshness.stale_since) : undefined}>
      {freshness.lag_seconds !== null
        ? t("scale.staleFor", { minutes: fmt.number(Math.max(1, Math.round(freshness.lag_seconds / 60))) })
        : t("scale.stale")}
    </span>
  );
}

function grainLabel(t: (k: MessageKey, p?: Record<string, string | number>) => string, p: PreAggregationInfo) {
  if (!p.time_grain) return t("scale.noTime");
  const grain = t(`grain.${p.time_grain}` as MessageKey);
  return p.calendar ? `${grain} · ${t(`settings.${p.calendar}` as MessageKey)}` : t("scale.anyCalendar", { grain });
}

function PreAggregations({
  items,
  running,
  bytes,
  onRefresh,
}: {
  items: PreAggregationInfo[];
  running: Running;
  bytes: Bytes;
  onRefresh: (p: PreAggregationInfo, mode: "auto" | "rebuild") => void;
}) {
  const { t, fmt, lang } = useI18n();
  return (
    <div className="table-wrap">
      <table className="grid sys-preaggs">
        <thead>
          <tr>
            <th>{t("scale.name")}</th>
            <th>{t("scale.grain")}</th>
            <th className="num">{t("system.rows")}</th>
            <th className="num">{t("system.size")}</th>
            <th>{t("scale.lastRefresh")}</th>
            <th>{t("scale.freshness")}</th>
            <th className="num">{t("scale.hits")}</th>
            <th>{t("scale.actions")}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((p) => {
            const s = p.state;
            const job = running[p.key];
            const busy = !!job || !!s?.building;
            return (
              <tr key={p.key}>
                <td>
                  <strong dir="auto">{p.label[lang] ?? p.name}</strong>
                  <div className="muted small">
                    <code dir="ltr">{p.key}</code>
                  </div>
                  <div className="muted small" dir="ltr">
                    {p.measures.join(", ")}
                    {p.dimensions.length > 0 && <> · {p.dimensions.join(", ")}</>}
                  </div>
                  {p.scope_fields.length > 0 && (
                    <div className="muted small">
                      {t("scale.scopeFields")} <code dir="ltr">{p.scope_fields.join(", ")}</code>
                    </div>
                  )}
                  {s?.last_error && (
                    <div className="error small" dir="ltr">
                      {s.last_error}
                    </div>
                  )}
                </td>
                <td>
                  {grainLabel(t, p)}
                  {p.incremental_window !== null && (
                    <div className="muted small">{t("scale.incremental", { days: fmt.number(p.incremental_window) })}</div>
                  )}
                </td>
                <td className="num">{s?.rows === null || s?.rows === undefined ? "—" : fmt.number(s.rows)}</td>
                <td className="num">{bytes(s?.size_bytes)}</td>
                <td>
                  {s?.refreshed_at ? (
                    <>
                      {fmt.date(s.refreshed_at)}
                      <div className="muted small">
                        {t(`scale.mode.${s.refresh_mode || "rebuild"}` as MessageKey)}
                        {s.duration_ms !== null && <> · {t("audit.ms", { ms: s.duration_ms })}</>}
                      </div>
                    </>
                  ) : (
                    "—"
                  )}
                </td>
                <td>
                  <FreshBadge state={s} freshness={s?.freshness} />
                  {s?.needs_rebuild && <div className="muted small">{t("scale.needsRebuild")}</div>}
                  {!s?.needs_rebuild && s?.dirty_from && (
                    <div className="muted small">
                      {t("scale.changedDays", { start: fmt.date(s.dirty_from), end: fmt.date(s.dirty_to ?? s.dirty_from) })}
                    </div>
                  )}
                  {p.due && <div className="muted small">{t(`scale.due.${p.due}` as MessageKey)}</div>}
                </td>
                <td className="num">
                  {fmt.number(s?.hits ?? 0)}
                  {s?.last_hit_at && <div className="muted small">{fmt.date(s.last_hit_at)}</div>}
                </td>
                <td className="sys-actions">
                  {busy ? (
                    <span className="muted">{t(job?.status === "queued" ? "scale.queued" : "scale.running")}</span>
                  ) : (
                    <>
                      <button type="button" onClick={() => onRefresh(p, "auto")} disabled={!s?.table}>
                        {t("scale.refresh")}
                      </button>
                      <button type="button" onClick={() => onRefresh(p, "rebuild")}>
                        {s?.table ? t("scale.rebuild") : t("scale.build")}
                      </button>
                    </>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ColumnarCard({
  info,
  bytes,
  job,
  onSync,
}: {
  info: ColumnarInfo;
  bytes: Bytes;
  job?: ScaleJob;
  onSync: () => void;
}) {
  const { t, fmt } = useI18n();
  const s = info.state;
  const ready = info.installed && info.configured;
  return (
    <div className="card sys-section">
      <h3>
        {info.label}{" "}
        <span className={`badge ${info.in_use ? "status-ok" : ""}`}>{info.in_use ? t("scale.inUse") : t("scale.notInUse")}</span>
      </h3>
      {info.error && <p className="error">{info.error}</p>}
      {!info.installed && !info.error && <p className="notice-warn">{t("scale.notInstalled", { name: info.name })}</p>}
      <dl className="sys-dl">
        <dt>{t("scale.models")}</dt>
        <dd dir="ltr">{info.semantic_models?.length ? info.semantic_models.join(", ") : "—"}</dd>
        <dt>{t("scale.file")}</dt>
        <dd>
          {info.file ? <code dir="ltr">{info.file}</code> : "—"} {info.exists && <span className="muted">{bytes(info.size_bytes)}</span>}
        </dd>
        <dt>{t("scale.lastSync")}</dt>
        <dd>
          {s?.synced_at ? (
            <>
              {fmt.date(s.synced_at)}
              {s.duration_ms !== null && <span className="muted"> · {t("audit.ms", { ms: s.duration_ms })}</span>}
            </>
          ) : (
            "—"
          )}
        </dd>
        <dt>{t("scale.freshness")}</dt>
        <dd>
          <FreshBadge freshness={s?.freshness} />
        </dd>
        {s && (
          <>
            <dt>{t("system.rows")}</dt>
            <dd>
              {fmt.number(s.rows ?? 0)}
              {Object.keys(s.tables).length > 0 && (
                <div className="muted small" dir="ltr">
                  {Object.entries(s.tables)
                    .map(([table, rows]) => `${table} ${fmt.number(rows)}`)
                    .join(" · ")}
                </div>
              )}
            </dd>
            <dt>{t("scale.hits")}</dt>
            <dd>{fmt.number(s.hits)}</dd>
          </>
        )}
      </dl>
      {s?.last_error && (
        <p className="error small" dir="ltr">
          {s.last_error}
        </p>
      )}
      <p>
        {job ? (
          <span className="muted">{t(job.status === "queued" ? "scale.queued" : "scale.running")}</span>
        ) : (
          <button type="button" onClick={onSync} disabled={!ready}>
            {t("scale.sync")}
          </button>
        )}
      </p>
    </div>
  );
}

function Jobs({ jobs }: { jobs: ScaleJob[] }) {
  const { t, fmt } = useI18n();
  if (!jobs.length) return null;
  return (
    <>
      <h3>{t("scale.jobs")}</h3>
      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th>{t("audit.time")}</th>
              <th>{t("scale.job")}</th>
              <th>{t("audit.user")}</th>
              <th>{t("audit.status")}</th>
              <th>{t("scale.result")}</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}>
                <td>{fmt.date(j.created_at)}</td>
                <td>
                  {t(`scale.kind.${j.kind}` as MessageKey)} <code dir="ltr">{j.target}</code>
                  {j.mode && <span className="muted"> · {t(`scale.mode.${j.mode}` as MessageKey)}</span>}
                </td>
                <td>
                  <bdi>{j.user ?? "—"}</bdi>
                </td>
                <td>
                  <span className={`badge status-${j.status === "done" ? "ok" : j.status === "failed" ? "failed" : "invalid"}`}>
                    {t(`scale.status.${j.status}` as MessageKey)}
                  </span>
                </td>
                <td>
                  {j.status === "done" && j.outcome ? (
                    <>
                      {j.outcome.mode && t(`scale.mode.${j.outcome.mode}` as MessageKey)}
                      {j.outcome.rows !== undefined && <> · {t("scale.rowsCount", { rows: fmt.number(j.outcome.rows) })}</>}
                      {j.outcome.duration_ms !== undefined && <> · {t("audit.ms", { ms: j.outcome.duration_ms })}</>}
                    </>
                  ) : (
                    <bdi className="muted">{j.error?.detail ?? ""}</bdi>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

