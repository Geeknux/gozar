import { Fragment, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, ApiError } from "../api";
import { isolate, UserText } from "../bidi";
import { type MessageKey, useI18n } from "../i18n";
import type { AlertChannel, AlertEntry, AlertPage, AlertsStatus, AlertSourceInfo, Severity } from "./types";

type Filters = { severity: string; source: string; delivery: string; acknowledged: string; q: string };

/** "15 min", or "24 h" for whole hours. */
function useDuration() {
  const { t, fmt } = useI18n();
  return (seconds: number) =>
    seconds >= 3600 && seconds % 3600 === 0
      ? t("alerts.hours", { n: fmt.number(seconds / 3600) })
      : t("alerts.minutes", { n: fmt.number(Math.round(seconds / 60)) });
}

const EMPTY: Filters = { severity: "", source: "", delivery: "", acknowledged: "", q: "" };
const SEVERITY_BADGE: Record<Severity, string> = {
  info: "",
  warning: "status-invalid",
  error: "status-failed",
  critical: "status-failed",
};

/** System → Alerts: the Telegram channel's status (never its token or chat ids),
 * the per-source switches, a test button and the recorded alerts (GET/POST/PATCH
 * /system/alerts*). A link from an alert message opens it with ?alert=<id>. */
export default function AlertsSection() {
  const { t } = useI18n();
  const [params, setParams] = useSearchParams();
  const linked = params.get("alert");
  const [status, setStatus] = useState<AlertsStatus | null>(null);
  const [page, setPage] = useState<AlertPage | null>(null);
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [number, setNumber] = useState(1);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const loadStatus = useCallback(() => {
    api<AlertsStatus>("/system/alerts/status")
      .then(setStatus)
      .catch((e: Error) => setError(e.message));
  }, []);

  const loadAlerts = useCallback(() => {
    const query: Record<string, string> = { page: String(number), page_size: "25" };
    for (const [k, v] of Object.entries(filters)) if (v) query[k] = v;
    if (linked) query.id = linked;
    api<AlertPage>("/system/alerts", { params: query })
      .then((p) => {
        setPage(p);
        setError("");
      })
      .catch((e: Error) => setError(e.message));
  }, [filters, number, linked]);

  useEffect(loadStatus, [loadStatus]);
  useEffect(loadAlerts, [loadAlerts]);
  useEffect(() => {
    if (linked) document.getElementById("system-alerts")?.scrollIntoView();
  }, [linked]);

  function refresh() {
    loadStatus();
    loadAlerts();
  }

  async function sendTest() {
    setNotice("");
    try {
      await api("/system/alerts/test", { method: "POST" });
      setNotice(t("alerts.testQueued"));
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function updateSource(name: string, body: { enabled?: boolean | null; min_severity?: string | null }) {
    try {
      setStatus(await api<AlertsStatus>(`/system/alerts/sources/${encodeURIComponent(name)}`, { method: "PATCH", body }));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function acknowledge(alert: AlertEntry) {
    try {
      const updated = await api<AlertEntry>(`/system/alerts/${alert.id}/ack`, { method: "POST" });
      setPage((p) => p && { ...p, entries: p.entries.map((a) => (a.id === updated.id ? updated : a)) });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  function filter(next: Partial<Filters>) {
    setFilters((f) => ({ ...f, ...next }));
    setNumber(1);
  }

  return (
    <section className="card sys-section sys-alerts" id="system-alerts">
      <div className="sys-section-head">
        <h2>{t("alerts.title")}</h2>
        <span className="sys-tools">
          <button type="button" onClick={sendTest} disabled={!status?.enabled}>
            {t("alerts.sendTest")}
          </button>
          <button type="button" onClick={refresh}>
            {t("system.refresh")}
          </button>
        </span>
      </div>
      <p className="muted">{t("alerts.help")}</p>
      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}
      {status && <Overview status={status} />}
      {status && <Sources status={status} onChange={updateSource} />}
      <h3>{t("alerts.recent")}</h3>
      {linked ? (
        <p>
          {t("alerts.showingOne", { id: linked })}{" "}
          <button
            type="button"
            className="link"
            onClick={() => {
              params.delete("alert");
              setParams(params, { replace: true });
            }}
          >
            {t("alerts.showAll")}
          </button>
        </p>
      ) : (
        page && <FilterBar page={page} filters={filters} onChange={filter} />
      )}
      {page && <AlertTable page={page} onAck={acknowledge} expanded={linked ? Number(linked) : null} />}
      {page && page.num_pages > 1 && (
        <p className="sys-tools">
          <button type="button" disabled={number <= 1} onClick={() => setNumber(number - 1)}>
            {t("alerts.previous")}
          </button>
          <span className="muted">{t("alerts.page", { page: page.page, pages: page.num_pages })}</span>
          <button type="button" disabled={number >= page.num_pages} onClick={() => setNumber(number + 1)}>
            {t("alerts.next")}
          </button>
        </p>
      )}
    </section>
  );
}

function Overview({ status }: { status: AlertsStatus }) {
  const { t, fmt } = useI18n();
  const duration = useDuration();
  const w = status.worker;
  return (
    <>
      {!status.enabled && <p className="notice-warn">{t("alerts.disabled")}</p>}
      {status.channels.map((c) => (
        <ChannelCard key={c.name} channel={c} />
      ))}
      {w.stale && <p className="notice-warn">{t("alerts.workerStale", { minutes: fmt.number(Math.round(w.stale_seconds / 60)) })}</p>}
      <dl className="sys-dl">
        <dt>{t("alerts.worker")}</dt>
        <dd>{w.last_beat_at ? fmt.date(w.last_beat_at) : t("alerts.noWorker")}</dd>
        <dt>{t("alerts.outbox")}</dt>
        <dd>
          {t("alerts.outboxCounts", {
            queued: fmt.number(status.outbox.queued),
            sent: fmt.number(status.outbox.sent),
            failed: fmt.number(status.outbox.failed),
          })}
          {status.outbox.last_sent_at && (
            <span className="muted"> · {t("alerts.lastSent", { time: fmt.date(status.outbox.last_sent_at) })}</span>
          )}
          {status.outbox.last_error && (
            <div className="error small" dir="ltr">
              {status.outbox.last_error}
            </div>
          )}
        </dd>
        <dt>{t("alerts.window")}</dt>
        <dd>{duration(status.window)}</dd>
        <dt>{t("alerts.quietHours")}</dt>
        <dd>
          {status.quiet_hours ? (
            <span dir="ltr">
              {status.quiet_hours.start}–{status.quiet_hours.end} ({status.quiet_hours.timezone})
            </span>
          ) : (
            t("alerts.none")
          )}
        </dd>
        <dt>{t("alerts.language")}</dt>
        <dd>{status.language === "fa" ? "فارسی" : "English"}</dd>
      </dl>
    </>
  );
}

function ChannelCard({ channel }: { channel: AlertChannel }) {
  const { t, fmt } = useI18n();
  return (
    <div className="sys-channel">
      <h3>
        {t("alerts.telegram")}{" "}
        <span className={`badge ${channel.enabled ? "status-ok" : channel.problems.length ? "status-failed" : ""}`}>
          {t(channel.enabled ? "alerts.on" : "alerts.off")}
        </span>
      </h3>
      {channel.problems.map((p) => (
        <p key={p.code} className="notice-warn">
          {t("alerts.channelRefused")} {p.message}
        </p>
      ))}
      {!channel.enabled && !channel.problems.length && <p className="muted">{t("alerts.notConfigured")}</p>}
      <dl className="sys-dl">
        <dt>{t("alerts.token")}</dt>
        <dd>
          {t(channel.token_configured ? "alerts.tokenSet" : "alerts.tokenMissing")}{" "}
          <code dir="ltr">{channel.token_env}</code>
        </dd>
        <dt>{t("alerts.allowed")}</dt>
        <dd>{t("alerts.allowedCount", { n: fmt.number(channel.allowed_count) })}</dd>
        <dt>{t("alerts.interactive")}</dt>
        <dd>{t(channel.interactive ? "alerts.on" : channel.interactive_requested ? "alerts.interactiveBlocked" : "alerts.off")}</dd>
      </dl>
    </div>
  );
}

function Sources({
  status,
  onChange,
}: {
  status: AlertsStatus;
  onChange: (name: string, body: { enabled?: boolean | null; min_severity?: string | null }) => void;
}) {
  const { t } = useI18n();
  const duration = useDuration();
  return (
    <>
      <h3>{t("alerts.sources")}</h3>
      <div className="table-wrap">
        <table className="grid sys-alert-sources">
          <thead>
            <tr>
              <th>{t("alerts.source")}</th>
              <th>{t("alerts.enabled")}</th>
              <th>{t("alerts.minSeverity")}</th>
              <th>{t("alerts.window")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {status.sources.map((s: AlertSourceInfo) => (
              <tr key={s.name}>
                <td>
                  <bdi>{s.label}</bdi>
                  <div className="muted small">
                    <code dir="ltr">{s.name}</code>
                  </div>
                </td>
                <td>
                  <label className="sys-toggle">
                    <input type="checkbox" checked={s.enabled} onChange={(e) => onChange(s.name, { enabled: e.target.checked })} />
                    {t(s.enabled ? "alerts.on" : "alerts.off")}
                  </label>
                </td>
                <td>
                  <select
                    value={s.min_severity}
                    aria-label={t("alerts.minSeverity")}
                    onChange={(e) => onChange(s.name, { min_severity: e.target.value })}
                  >
                    {status.severities.map((v) => (
                      <option key={v} value={v}>
                        {t(`alerts.severity.${v}` as MessageKey)}
                      </option>
                    ))}
                  </select>
                </td>
                <td>{s.window ? duration(s.window) : "—"}</td>
                <td>
                  {s.overridden && (
                    <button type="button" className="link" onClick={() => onChange(s.name, { enabled: null, min_severity: null })}>
                      {t("alerts.reset")}
                    </button>
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

function FilterBar({ page, filters, onChange }: { page: AlertPage; filters: Filters; onChange: (f: Partial<Filters>) => void }) {
  const { t } = useI18n();
  const select = (name: "severity" | "source" | "delivery", label: MessageKey) => (
    <select value={filters[name]} aria-label={t(label)} onChange={(e) => onChange({ [name]: e.target.value })}>
      <option value="">{t(label)}: {t("common.all")}</option>
      {page.filters[name].map((o) => (
        <option key={o.value} value={o.value}>
          {isolate(o.label)}
        </option>
      ))}
    </select>
  );
  return (
    <p className="sys-tools">
      {select("severity", "alerts.severity")}
      {select("source", "alerts.source")}
      {select("delivery", "alerts.delivery")}
      <select value={filters.acknowledged} aria-label={t("alerts.acknowledged")} onChange={(e) => onChange({ acknowledged: e.target.value })}>
        <option value="">{t("alerts.acknowledged")}: {t("common.all")}</option>
        <option value="false">{t("alerts.unacknowledged")}</option>
        <option value="true">{t("alerts.acknowledgedOnly")}</option>
      </select>
      <input
        type="search"
        placeholder={t("common.searchPlaceholder")}
        defaultValue={filters.q}
        onKeyDown={(e) => {
          if (e.key === "Enter") onChange({ q: (e.target as HTMLInputElement).value });
        }}
      />
    </p>
  );
}

function AlertTable({ page, onAck, expanded }: { page: AlertPage; onAck: (a: AlertEntry) => void; expanded: number | null }) {
  const { t, fmt } = useI18n();
  const [open, setOpen] = useState<number | null>(expanded);
  if (!page.entries.length) return <p className="empty">{t("alerts.empty")}</p>;
  return (
    <div className="table-wrap">
      <table className="grid sys-alert-list">
        <thead>
          <tr>
            <th>{t("audit.time")}</th>
            <th>{t("alerts.severity")}</th>
            <th>{t("alerts.alert")}</th>
            <th>{t("alerts.delivery")}</th>
            <th>{t("alerts.acknowledged")}</th>
          </tr>
        </thead>
        <tbody>
          {page.entries.map((a) => (
            <Fragment key={a.id}>
              <tr>
                <td>
                  {fmt.date(a.created_at)}
                  <div className="muted small">#{fmt.number(a.id)}</div>
                </td>
                <td>
                  {a.kind === "resolved" ? (
                    <span className="badge status-ok">{t("alerts.resolved")}</span>
                  ) : (
                    <span className={`badge ${SEVERITY_BADGE[a.severity]}`}>{a.severity_display}</span>
                  )}
                </td>
                <td className="sys-alert-title">
                  <button type="button" className="link" onClick={() => setOpen(open === a.id ? null : a.id)}>
                    <bdi>{a.title}</bdi>
                  </button>
                  <div className="muted small">
                    <code dir="ltr">{a.source}</code>
                    {a.repeats > 0 && <> · {t("alerts.repeats", { n: fmt.number(a.repeats) })}</>}
                    {a.suppressed_before > 0 && <> · {t("alerts.heldBack", { n: fmt.number(a.suppressed_before) })}</>}
                  </div>
                </td>
                <td>
                  <span className={`badge ${a.delivery === "sent" ? "status-ok" : a.delivery === "failed" ? "status-failed" : ""}`}>
                    {a.delivery_display}
                  </span>
                </td>
                <td>
                  {a.acknowledged_at ? (
                    <span className="muted">
                      {fmt.date(a.acknowledged_at)}
                      {a.acknowledged_by && (
                        <>
                          {" · "}
                          <bdi>{a.acknowledged_by}</bdi>
                        </>
                      )}
                      {a.acknowledged_via === "telegram" && <> · {t("alerts.telegram")}</>}
                    </span>
                  ) : (
                    a.kind === "alert" && (
                      <button type="button" onClick={() => onAck(a)}>
                        {t("alerts.ack")}
                      </button>
                    )
                  )}
                </td>
              </tr>
              {open === a.id && (
                <tr className="sys-alert-details">
                  <td colSpan={5}>
                    {a.message && <UserText as="p">{a.message}</UserText>}
                    {a.details.traceback && (
                      <pre dir="ltr" className="sys-traceback">
                        {a.details.traceback}
                      </pre>
                    )}
                    <dl className="sys-dl">
                      <dt>{t("alerts.key")}</dt>
                      <dd>
                        <code dir="ltr">{a.key}</code>
                      </dd>
                      {a.release && (
                        <>
                          <dt>{t("alerts.release")}</dt>
                          <dd>
                            <code dir="ltr">{a.release}</code>
                          </dd>
                        </>
                      )}
                      {a.last_seen_at && a.repeats > 0 && (
                        <>
                          <dt>{t("alerts.lastSeen")}</dt>
                          <dd>{fmt.date(a.last_seen_at)}</dd>
                        </>
                      )}
                      {a.deliveries.map((d, i) => (
                        <Fragment key={i}>
                          <dt>
                            {t("alerts.telegram")} {fmt.number(i + 1)}
                          </dt>
                          <dd>
                            {t(`alerts.outboxStatus.${d.status}` as MessageKey)}
                            {d.attempts > 0 && <span className="muted"> · {t("alerts.attempts", { n: fmt.number(d.attempts) })}</span>}
                            {d.status === "queued" && <span className="muted"> · {fmt.date(d.next_attempt_at)}</span>}
                            {d.error && (
                              <div className="error small" dir="ltr">
                                {d.error}
                              </div>
                            )}
                          </dd>
                        </Fragment>
                      ))}
                    </dl>
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}
