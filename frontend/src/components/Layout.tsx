import { type ReactNode, useEffect, useRef, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";

import type { User } from "../App";
import { useShortcuts } from "../hooks";
import { useI18n } from "../i18n";
import type { AppEntry } from "../types";
import Dialog from "./Dialog";

type Props = { user: User; apps: AppEntry[]; onLogout: () => void; children: ReactNode };

export default function Layout({ user, apps, onLogout, children }: Props) {
  const { t, cap } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const searchBox = useRef<HTMLInputElement>(null);
  const [q, setQ] = useState("");
  const [help, setHelp] = useState(false);

  useEffect(() => {
    if (location.pathname === "/search") setQ(new URLSearchParams(location.search).get("q") ?? "");
  }, [location]);

  const focusSearch = () => {
    searchBox.current?.focus();
    searchBox.current?.select();
  };
  useShortcuts({
    "mod+k": focusSearch,
    // "/" focuses the list's own search box on list pages (see ModelList)
    "/": () => {
      if (!document.querySelector(".list-page .search input")) focusSearch();
    },
    "?": () => setHelp(true),
  });

  return (
    <div className="shell">
      <aside className="sidebar">
        <Link to="/" className="brand">
          {t("app.brand")} <small dir="ltr">Django-Bi-Admin</small>
        </Link>
        <nav>
          <section>
            <NavLink to="/dashboards">{t("nav.dashboards")}</NavLink>
            <NavLink to="/reports" end>
              {t("nav.reports")}
            </NavLink>
            <NavLink to="/reports/saved">{t("nav.savedReports")}</NavLink>
            <NavLink to="/schedules">{t("nav.schedules")}</NavLink>
            {user.permissions?.audit && <NavLink to="/audit">{t("nav.audit")}</NavLink>}
            {user.permissions?.questions && <NavLink to="/questions">{t("nav.questions")}</NavLink>}
            {user.permissions?.system && <NavLink to="/system">{t("nav.system")}</NavLink>}
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
          <form
            className="global-search"
            role="search"
            onSubmit={(e) => {
              e.preventDefault();
              if (q.trim()) navigate(`/search?q=${encodeURIComponent(q.trim())}`);
            }}
          >
            <input
              ref={searchBox}
              type="search"
              aria-label={t("search.title")}
              placeholder={t("search.placeholder")}
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && (e.currentTarget as HTMLInputElement).blur()}
            />
            <kbd className="kbd-hint" aria-hidden>
              Ctrl K
            </kbd>
          </form>
          <span className="topbar-actions">
            <span>
              <bdi>{user.full_name || user.username}</bdi>
            </span>
            <button type="button" className="link" onClick={() => setHelp(true)} title={t("shortcuts.title")}>
              {t("shortcuts.short")}
            </button>
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
      {help && <ShortcutsHelp onClose={() => setHelp(false)} />}
    </div>
  );
}

function ShortcutsHelp({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const rows: [string, string][] = [
    ["Ctrl K", t("shortcuts.search")],
    ["/", t("shortcuts.listSearch")],
    ["Ctrl S", t("shortcuts.save")],
    ["Esc", t("shortcuts.close")],
    ["?", t("shortcuts.help")],
  ];
  return (
    <Dialog title={t("shortcuts.title")} onClose={onClose}>
      <table className="grid shortcuts">
        <tbody>
          {rows.map(([keys, what]) => (
            <tr key={keys}>
              <td>
                <kbd dir="ltr">{keys}</kbd>
              </td>
              <td>{what}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small muted">{t("shortcuts.mac")}</p>
      <div className="dialog-actions">
        <button type="button" onClick={onClose}>
          {t("common.close")}
        </button>
      </div>
    </Dialog>
  );
}
