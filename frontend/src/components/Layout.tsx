import type { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";

import type { User } from "../App";
import { useI18n } from "../i18n";
import type { AppEntry } from "../types";

type Props = { user: User; apps: AppEntry[]; onLogout: () => void; children: ReactNode };

export default function Layout({ user, apps, onLogout, children }: Props) {
  const { t, cap } = useI18n();
  return (
    <div className="shell">
      <aside className="sidebar">
        <Link to="/" className="brand">
          {t("app.brand")} <small dir="ltr">Django-Bi-Admin</small>
        </Link>
        <nav>
          <section>
            <NavLink to="/reports">{t("nav.reports")}</NavLink>
            {user.permissions?.audit && <NavLink to="/audit">{t("nav.audit")}</NavLink>}
          </section>
          {apps.map((app) => (
            <section key={app.app_label}>
              <h3>{cap(app.verbose_name)}</h3>
              {app.models.map((m) => (
                <NavLink key={m.label} to={`/${m.label.replace(".", "/")}`}>
                  {cap(m.verbose_name_plural)}
                </NavLink>
              ))}
            </section>
          ))}
        </nav>
      </aside>
      <div className="main">
        <header className="topbar">
          <span>
            <bdi>{user.full_name || user.username}</bdi>
          </span>
          <span className="topbar-actions">
            <NavLink to="/settings" className="link">
              {t("nav.settings")}
            </NavLink>
            <button type="button" className="link" onClick={onLogout}>
              {t("nav.logout")}
            </button>
          </span>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}
