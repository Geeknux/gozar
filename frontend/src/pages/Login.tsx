import { type FormEvent, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError, api } from "../api";
import { type Lang, useI18n } from "../i18n";

const LANGUAGES: { value: Lang; label: string }[] = [
  { value: "en", label: "English" },
  { value: "fa", label: "فارسی" },
];

type Props = { onLogin: () => Promise<void>; onLanguage: (lang: Lang) => void };

export default function Login({ onLogin, onLanguage }: Props) {
  const { t, lang } = useI18n();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  function chooseLanguage(next: Lang) {
    // Before login the server follows the language cookie (then Accept-Language).
    document.cookie = `django_language=${next}; path=/; max-age=31536000; SameSite=Lax`;
    setError("");
    onLanguage(next);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/auth/csrf");
      await api("/auth/login", { method: "POST", body: { username, password } });
      await onLogin();
      // ?next= comes from server pages outside the SPA (e.g. the API docs): a full load.
      const next = new URLSearchParams(location.search).get("next");
      if (next && next.startsWith("/") && !next.startsWith("//")) {
        window.location.assign(next);
        return;
      }
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== "/login" ? from : "/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("app.networkError"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form onSubmit={submit} className="card">
        <h1>{t("login.title")}</h1>
        <label>
          {t("login.username")}
          <input dir="auto" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
        </label>
        <label>
          {t("login.password")}
          <input type="password" dir="ltr" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "…" : t("login.submit")}
        </button>
        <div className="segmented" role="group" aria-label={t("login.language")}>
          {LANGUAGES.map((l) => (
            <button
              key={l.value}
              type="button"
              lang={l.value}
              className={l.value === lang ? "active" : ""}
              aria-pressed={l.value === lang}
              onClick={() => chooseLanguage(l.value)}
            >
              <bdi>{l.label}</bdi>
            </button>
          ))}
        </div>
      </form>
    </div>
  );
}
