import { Fragment, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { UserText } from "../bidi";
import { type MessageKey, useI18n } from "../i18n";
import { useSession } from "../session";
import { errorText, personName } from "../sharing";
import { type Delivery, deleteSchedule, getSchedule, listSchedules, type Schedule, sendSchedule, updateSchedule } from "./api";
import Deliveries from "./Deliveries";
import ScheduleDialog from "./ScheduleDialog";

/** My e-mail schedules: when they run, who gets them, and what was sent. */
export default function SchedulesPage() {
  const { t, fmt } = useI18n();
  const session = useSession();
  const [items, setItems] = useState<Schedule[] | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<Schedule | null>(null);
  const [history, setHistory] = useState<Record<number, Delivery[]>>({});

  const load = useCallback(() => {
    listSchedules()
      .then(setItems)
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setError("");
    setNotice("");
    try {
      await fn();
      load();
    } catch (err) {
      setError(errorText(err));
    }
  };

  const send = (s: Schedule, onlyMe: boolean) =>
    act(async () => {
      const { deliveries } = await sendSchedule(s.id, onlyMe);
      const sent = deliveries.filter((d) => d.status === "sent").length;
      setNotice(t("schedule.sentCount", { sent, total: deliveries.length }));
      setHistory((h) => ({ ...h, [s.id]: [...deliveries, ...(h[s.id] ?? [])] }));
    });

  const toggleHistory = async (s: Schedule) => {
    if (history[s.id]) {
      setHistory(({ [s.id]: _drop, ...rest }) => rest);
      return;
    }
    const full = await getSchedule(s.id);
    setHistory((h) => ({ ...h, [s.id]: full.deliveries ?? [] }));
  };

  const when = (s: Schedule) => {
    const day =
      s.frequency === "weekly" && s.weekday !== null
        ? t(`weekday.${s.weekday}` as MessageKey)
        : s.frequency === "monthly" && s.day_of_month !== null
          ? t("schedule.onDay", { day: s.day_of_month === 31 ? t("schedule.lastDay") : fmt.digits(s.day_of_month) })
          : "";
    return [t(`schedule.${s.frequency}` as MessageKey), day, fmt.digits(s.time)].filter(Boolean).join(" · ");
  };

  return (
    <div className="list-page">
      <div className="page-head">
        <h1>{t("nav.schedules")}</h1>
      </div>
      <p className="muted">{t("schedule.intro")}</p>
      {notice && <p className="notice">{notice}</p>}
      {error && <p className="error">{error}</p>}
      {!items && !error && <p className="muted">{t("app.loading")}</p>}
      {items && items.length === 0 && (
        <p className="empty">
          {t("schedule.empty")} <Link to="/reports/saved">{t("nav.savedReports")}</Link> · <Link to="/dashboards">{t("nav.dashboards")}</Link>
        </p>
      )}
      {items && items.length > 0 && (
        <div className="table-wrap">
          <table className="grid">
            <thead>
              <tr>
                <th>{t("schedule.name")}</th>
                <th>{t("schedule.when")}</th>
                <th>{t("schedule.recipients")}</th>
                <th>{t("schedule.nextRun")}</th>
                <th>{t("schedule.enabled")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <Fragment key={s.id}>
                  <tr>
                    <td>
                      <bdi>{s.name || s.target.name}</bdi>
                      {s.description && <UserText className="muted small">{s.description}</UserText>}
                      <div className="muted small">
                        {t(s.target.kind === "dashboard" ? "schedule.ofDashboard" : "schedule.ofReport")}{" "}
                        <Link to={s.target.kind === "dashboard" ? `/dashboards/${s.target.id}` : `/reports?saved=${s.target.id}`}>
                          <bdi>{s.target.name}</bdi>
                        </Link>
                      </div>
                    </td>
                    <td>
                      {when(s)}
                      <div className="muted small" dir="ltr">
                        {s.timezone}
                      </div>
                    </td>
                    <td>{s.recipients.map(personName).join(t("common.listSeparator"))}</td>
                    <td>{s.next_run_at ? fmt.date(s.next_run_at) : "—"}</td>
                    <td>
                      <input
                        type="checkbox"
                        checked={s.enabled}
                        aria-label={t("schedule.enabled")}
                        onChange={(e) => act(() => updateSchedule(s.id, { enabled: e.target.checked }))}
                      />
                    </td>
                    <td className="actions-cell">
                      <button type="button" className="button small" onClick={() => send(s, true)}>
                        {t("schedule.sendTest")}
                      </button>{" "}
                      <button type="button" className="button small" onClick={() => send(s, false)}>
                        {t("schedule.sendNow")}
                      </button>{" "}
                      <button type="button" className="button small" onClick={() => toggleHistory(s)}>
                        {t("schedule.history")}
                      </button>{" "}
                      <button type="button" className="button small" onClick={() => setEditing(s)}>
                        {t("dash.edit")}
                      </button>{" "}
                      <button
                        type="button"
                        className="button small danger"
                        onClick={() => window.confirm(t("schedule.confirmDelete")) && act(() => deleteSchedule(s.id))}
                      >
                        {t("common.delete")}
                      </button>
                    </td>
                  </tr>
                  {history[s.id] && (
                    <tr className="history-row">
                      <td colSpan={6}>
                        <Deliveries items={history[s.id]} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && (
        <ScheduleDialog schedule={editing} me={session.user} onSaved={load} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}
