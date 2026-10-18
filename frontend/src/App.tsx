import { useCallback, useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { api, setUnauthorizedHandler } from "./api";
import Layout from "./components/Layout";
import Home from "./pages/Home";
import Login from "./pages/Login";
import ModelList from "./pages/ModelList";
import ObjectForm from "./pages/ObjectForm";
import type { AppEntry } from "./types";

export type User = { username: string; full_name: string; is_superuser: boolean };

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [apps, setApps] = useState<AppEntry[]>([]);
  const location = useLocation();

  const refresh = useCallback(async () => {
    await api("/auth/csrf");
    const me = await api<{ authenticated: boolean; user: User | null }>("/auth/me");
    setUser(me.authenticated ? me.user : null);
    if (me.authenticated) {
      const index = await api<{ apps: AppEntry[] }>("/meta/models");
      setApps(index.apps);
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    refresh().catch(() => setUser(null));
  }, [refresh]);

  if (user === undefined) return <div className="splash">در حال بارگذاری…</div>;

  if (user === null) {
    return (
      <Routes>
        <Route path="/login" element={<Login onLogin={refresh} />} />
        <Route
          path="*"
          element={<Navigate to="/login" replace state={{ from: location.pathname }} />}
        />
      </Routes>
    );
  }

  const logout = async () => {
    await api("/auth/logout", { method: "POST" });
    setUser(null);
    setApps([]);
  };

  return (
    <Layout user={user} apps={apps} onLogout={logout}>
      <Routes>
        <Route path="/" element={<Home apps={apps} />} />
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="/:app/:model" element={<ModelList />} />
        <Route path="/:app/:model/add" element={<ObjectForm />} />
        <Route path="/:app/:model/:pk" element={<ObjectForm />} />
        <Route path="*" element={<p className="empty">صفحه پیدا نشد.</p>} />
      </Routes>
    </Layout>
  );
}
