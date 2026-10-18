import type { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";

import type { User } from "../App";
import type { AppEntry } from "../types";

type Props = { user: User; apps: AppEntry[]; onLogout: () => void; children: ReactNode };

export default function Layout({ user, apps, onLogout, children }: Props) {
  return (
    <div className="shell">
      <aside className="sidebar">
        <Link to="/" className="brand">
          گذر <small dir="ltr">Django-Bi-Admin</small>
        </Link>
        <nav>
          {apps.map((app) => (
            <section key={app.app_label}>
              <h3>{app.verbose_name}</h3>
              {app.models.map((m) => (
                <NavLink key={m.label} to={`/${m.label.replace(".", "/")}`}>
                  {m.verbose_name_plural}
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
          <button type="button" className="link" onClick={onLogout}>
            خروج
          </button>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}
