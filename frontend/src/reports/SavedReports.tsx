import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { type MessageKey, useI18n } from "../i18n";
import ScheduleDialog from "../schedules/ScheduleDialog";
import { useSession } from "../session";
import { deleteSaved, duplicateSaved, errorText, listSaved, personName, type SavedReport } from "../sharing";

/** Saved reports: mine and those shared with me (each runs with my own access). */
export default function SavedReports() {
  const { t, fmt } = useI18n();
  const session = useSession();
  const navigate = useNavigate();
  const [items, setItems] = useState<SavedReport[] | null>(null);
  const [q, setQ] = useState("");
  const [mine, setMine] = useState(false);
  const [error, setError] = useState("");
  const [schedule, setSchedule] = useState<SavedReport | null>(null);

  const load = useCallback(() => {
    const params: Record<string, string> = {};
    if (q) params.q = q;
    if (mine) params.mine = "true";
    listSaved(params)
      .then(setItems)
      .catch((e: Error) => setError(e.message));
  }, [q, mine]);

  useEffect(() => {
    const timer = setTimeout(load, 200);
    return () => clearTimeout(timer);
  }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setError("");
    try {
      await fn();
      load();
    } catch (err) {
      setError(errorText(err));
    }
  };

  return (
    <div className="list-page">
      <div className="page-head">
        <h1>{t("nav.savedReports")}</h1>
        <Link to="/reports" className="button primary">
          {t("saved.newReport")}
        </Link>
      </div>
      <p className="muted">{t("saved.intro")}</p>
      <div className="toolbar">
        <input type="search" value={q} placeholder={t("common.searchPlaceholder")} aria-label={t("common.search")} onChange={(e) => setQ(e.target.value)} />
        <label className="check-row">
          <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} />
          {t("saved.onlyMine")}
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      {!items && !error && <p className="muted">{t("app.loading")}</p>}
      {items && items.length === 0 && <p className="empty">{t("saved.empty")}</p>}
      {items && items.length > 0 && (
        <div className="table-wrap">
          <table className="grid">
            <thead>
              <tr>
                <th>{t("saved.name")}</th>
                <th>{t("saved.owner")}</th>
                <th>{t("share.title")}</th>
                <th>{t("saved.updated")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <tr key={r.id}>
                  <td>
                    <Link to={`/reports?saved=${r.id}`}>
                      <bdi>{r.name}</bdi>
                    </Link>
                    {r.description && (
                      <div className="muted small" dir="auto">
                        {r.description}
                      </div>
                    )}
                    {r.available === false && <div className="error small">{r.problem}</div>}
                  </td>
                  <td>
                    <bdi>{r.is_owner ? t("saved.me") : personName(r.owner)}</bdi>
                  </td>
                  <td>{t(`share.short.${r.sharing.visibility}` as MessageKey)}</td>
                  <td>{fmt.date(r.updated_at)}</td>
                  <td className="actions-cell">
                    <button type="button" className="button small" onClick={() => act(async () => navigate(`/reports?saved=${(await duplicateSaved(r.id)).id}`))}>
                      {t("saved.duplicate")}
                    </button>{" "}
                    <button type="button" className="button small" onClick={() => setSchedule(r)}>
                      {t("schedule.emailIt")}
                    </button>{" "}
                    {r.can_edit && (
                      <button
                        type="button"
                        className="button small danger"
                        onClick={() => window.confirm(t("saved.confirmDelete", { name: r.name })) && act(() => deleteSaved(r.id))}
                      >
                        {t("common.delete")}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {schedule && (
        <ScheduleDialog
          target={{ kind: "saved_report", id: schedule.id, name: schedule.name }}
          me={session.user}
          defaultTimezone={session.timezone}
          onSaved={() => navigate("/schedules")}
          onClose={() => setSchedule(null)}
        />
      )}
    </div>
  );
}
