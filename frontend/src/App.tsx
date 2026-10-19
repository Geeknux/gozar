import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { api, setUnauthorizedHandler } from "./api";
import Layout from "./components/Layout";
import { config } from "./config";
import { clearMetaCache } from "./hooks";
import { applyDocument, I18nProvider, type Prefs, useI18n } from "./i18n";
import AuditLog from "./pages/AuditLog";
import Home from "./pages/Home";
import Login from "./pages/Login";
import ModelList from "./pages/ModelList";
import ObjectForm from "./pages/ObjectForm";
import QuestionLog from "./pages/QuestionLog";
import Settings from "./pages/Settings";
import type { AppEntry, Preferences } from "./types";

// ECharts is large: load the Reports area on demand.
const ReportsPage = lazy(() => import("./reports/ReportsPage"));

export type User = {
  username: string;
  full_name: string;
  is_superuser: boolean;
  permissions?: { audit?: boolean; questions?: boolean };
};

type Me = { authenticated: boolean; user: User | null; preferences: Preferences };

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [apps, setApps] = useState<AppEntry[]>([]);
  const [prefs, setPrefsState] = useState<Prefs>({ language: config.language, calendar: config.calendar });

  const setPrefs = useCallback((next: Prefs) => {
    setPrefsState((current) => {
      if (current.language !== next.language) clearMetaCache();
      return current.language === next.language && current.calendar === next.calendar ? current : next;
    });
  }, []);

  useEffect(() => applyDocument(prefs.language), [prefs.language]);

  const refresh = useCallback(async () => {
    await api("/auth/csrf");
    const me = await api<Me>("/auth/me");
    setPrefs({ language: me.preferences.language, calendar: me.preferences.calendar });
    setUser(me.authenticated ? me.user : null);
    if (me.authenticated) {
      const index = await api<{ apps: AppEntry[] }>("/meta/models");
      setApps(index.apps);
    }
  }, [setPrefs]);

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    refresh().catch(() => setUser(null));
  }, [refresh]);

  // The model index is translated server-side: reload it when the language changes.
  useEffect(() => {
    if (!user) return;
    api<{ apps: AppEntry[] }>("/meta/models")
      .then((index) => setApps(index.apps))
      .catch(() => undefined);
  }, [user, prefs.language]);

  const logout = async () => {
    await api("/auth/logout", { method: "POST" });
    setUser(null);
    setApps([]);
  };

  return (
    <I18nProvider prefs={prefs}>
      <Shell
        user={user}
        apps={apps}
        prefs={prefs}
        onLogin={refresh}
        onLogout={logout}
        onPrefs={setPrefs}
      />
    </I18nProvider>
  );
}

type ShellProps = {
  user: User | null | undefined;
  apps: AppEntry[];
  prefs: Prefs;
  onLogin: () => Promise<void>;
  onLogout: () => void;
  onPrefs: (p: Prefs) => void;
};

function Shell({ user, apps, prefs, onLogin, onLogout, onPrefs }: ShellProps) {
  const { t } = useI18n();
  const location = useLocation();

  if (user === undefined) return <div className="splash">{t("app.loading")}</div>;

  if (user === null) {
    return (
      <Routes>
        <Route path="/login" element={<Login onLogin={onLogin} onLanguage={(language) => onPrefs({ ...prefs, language })} />} />
        <Route path="*" element={<Navigate to="/login" replace state={{ from: location.pathname }} />} />
      </Routes>
    );
  }

  return (
    <Layout user={user} apps={apps} onLogout={onLogout}>
      {/* Keyed by language so pages refetch their (server-translated) data. */}
      <Routes key={prefs.language}>
        <Route path="/" element={<Home apps={apps} />} />
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="/settings" element={<Settings onSaved={onPrefs} />} />
        <Route
          path="/reports"
          element={
            <Suspense fallback={<p className="muted">{t("app.loading")}</p>}>
              <ReportsPage />
            </Suspense>
          }
        />
        {user.permissions?.audit && <Route path="/audit" element={<AuditLog />} />}
        {user.permissions?.questions && <Route path="/questions" element={<QuestionLog />} />}
        <Route path="/:app/:model" element={<ModelList />} />
        <Route path="/:app/:model/add" element={<ObjectForm />} />
        <Route path="/:app/:model/:pk" element={<ObjectForm />} />
        <Route path="*" element={<p className="empty">{t("app.notFound")}</p>} />
      </Routes>
    </Layout>
  );
}
