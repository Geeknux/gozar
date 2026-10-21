import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { UserText } from "../bidi";
import SharedDialog from "../components/SharedDialog";
import { type MessageKey, useI18n } from "../i18n";
import { homeDashboard, useSession } from "../session";
import { errorText, personName, sharingIn } from "../sharing";
import { createDashboard, type DashboardSummary, deleteDashboard, duplicateDashboard, listDashboards } from "./api";

/** Dashboards I own or that are shared with me; pin one as my home page. */
export default function DashboardsList() {
  const { t, fmt } = useI18n();
  const session = useSession();
  const navigate = useNavigate();
  const [items, setItems] = useState<DashboardSummary[] | null>(null);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const home = homeDashboard(session);

  const load = useCallback(() => {
    listDashboards()
      .then((r) => setItems(r.dashboards))
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(load, [load]);

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
        <h1>{t("nav.dashboards")}</h1>
        <button type="button" className="primary" onClick={() => setCreating(true)}>
          {t("dash.new")}
        </button>
      </div>
      <p className="muted">{t("dash.intro")}</p>
      {error && <p className="error">{error}</p>}
      {!items && !error && <p className="muted">{t("app.loading")}</p>}
      {items && items.length === 0 && <p className="empty">{t("dash.empty")}</p>}
      {items && items.length > 0 && (
        <div className="dashboard-cards">
          {items.map((b) => (
            <article key={b.id} className="card dashboard-card">
              <h2>
                <Link to={`/dashboards/${b.id}`}>
                  <bdi>{b.name}</bdi>
                </Link>
                {home === b.id && <span className="badge">{t("dash.home")}</span>}
              </h2>
              {b.description && (
                <UserText className="muted small">{b.description}</UserText>
              )}
              <p className="muted small">
                {t("dash.widgets", { count: b.widget_count ?? 0 })} · {b.is_owner ? t("saved.mine") : t("saved.sharedBy", { name: personName(b.owner) })} ·{" "}
                {t(`share.short.${b.sharing.visibility}` as MessageKey)} · {fmt.date(b.updated_at)}
              </p>
              <div className="card-actions">
                <button type="button" className="button small" onClick={() => act(() => session.setOption("home_dashboard", home === b.id ? null : b.id))}>
                  {home === b.id ? t("dash.unpin") : t("dash.pin")}
                </button>
                <button type="button" className="button small" onClick={() => act(async () => navigate(`/dashboards/${(await duplicateDashboard(b.id)).id}`))}>
                  {t("saved.duplicate")}
                </button>
                {b.can_edit && (
                  <button
                    type="button"
                    className="button small danger"
                    onClick={() => window.confirm(t("dash.confirmDelete", { name: b.name })) && act(() => deleteDashboard(b.id))}
                  >
                    {t("common.delete")}
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
      {creating && (
        <SharedDialog
          title={t("dash.new")}
          submitLabel={t("dash.create")}
          onSubmit={async (v) => {
            const board = await createDashboard({ ...v, sharing: sharingIn(v.sharing), filters: { dimensions: [] } });
            navigate(`/dashboards/${board.id}?edit=1`);
          }}
          onClose={() => setCreating(false)}
        />
      )}
    </div>
  );
}
