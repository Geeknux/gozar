import { type FormEvent, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError, api } from "../api";

export default function Login({ onLogin }: { onLogin: () => Promise<void> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/auth/csrf");
      await api("/auth/login", { method: "POST", body: { username, password } });
      await onLogin();
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== "/login" ? from : "/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "خطا در ارتباط با سرور");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form onSubmit={submit} className="card">
        <h1>ورود به گذر</h1>
        <label>
          نام کاربری
          <input dir="auto" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
        </label>
        <label>
          گذرواژه
          <input type="password" dir="ltr" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "…" : "ورود"}
        </button>
      </form>
    </div>
  );
}
