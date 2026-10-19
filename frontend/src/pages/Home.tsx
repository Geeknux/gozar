import { lazy, Suspense, useState } from "react";
import { Link } from "react-router-dom";

import { useI18n } from "../i18n";
import { homeDashboard, useSession } from "../session";
import type { AppEntry } from "../types";

// Loaded on demand like the Reports page (it shares the report components).
const AskPanel = lazy(() => import("../reports/AskPanel"));
const DashboardView = lazy(() => import("../dashboards/DashboardView"));

export default function Home({ apps }: { apps: AppEntry[] }) {
  const { t, cap } = useI18n();
  const session = useSession();
  const [pinnedMissing, setPinnedMissing] = useState(false);
  const pinned = homeDashboard(session);
  if (!apps.length) return <p className="empty">{t("home.noAccess")}</p>;
  if (pinned !== null && !pinnedMissing) {
    return (
      <div className="home home-dashboard">
        <Suspense fallback={<p className="muted">{t("app.loading")}</p>}>
          <DashboardView key={pinned} id={pinned} titleLevel="h1" onMissing={() => setPinnedMissing(true)} />
        </Suspense>
        <details className="card home-ask">
          <summary>{t("ask.title")}</summary>
          <Suspense fallback={<p className="muted">{t("app.loading")}</p>}>
            <AskPanel />
          </Suspense>
        </details>
      </div>
    );
  }
  return (
    <div className="home">
      <h1>{t("nav.dashboard")}</h1>
      <Suspense fallback={<p className="muted">{t("app.loading")}</p>}>
        <AskPanel />
      </Suspense>
      {apps.map((app) => (
        <section key={app.app_label} className="card">
          <h2>{cap(app.verbose_name)}</h2>
          <table className="grid">
            <tbody>
              {app.models.map((m) => (
                <tr key={m.label}>
                  <td>
                    <Link to={`/${m.label.replace(".", "/")}`}>{cap(m.verbose_name_plural)}</Link>
                  </td>
                  <td className="actions-cell">
                    {m.permissions.add && (
                      <Link to={`/${m.label.replace(".", "/")}/add`} className="button small">
                        {t("home.add")}
                      </Link>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}
