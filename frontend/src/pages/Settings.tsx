import { useEffect, useState } from "react";

import { ApiError, api } from "../api";
import { type CalendarName, type Lang, makeFormat, type Prefs, useI18n } from "../i18n";
import { timeZones } from "../schedules/api";
import { useSession } from "../session";
import type { Preferences } from "../types";

function today(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Per-user interface preferences; every change is saved and applied at once. */
export default function Settings({ onSaved }: { onSaved: (p: Prefs) => void }) {
  const { t, lang, calendar } = useI18n();
  const [prefs, setPrefs] = useState<Preferences | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const session = useSession();
  const zones = timeZones();
  const storedZone = typeof session.options.timezone === "string" ? session.options.timezone : "";

  async function saveZone(zone: string) {
    setError("");
    setSaved(false);
    try {
      await session.setOption("timezone", zone || null);
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("app.networkError"));
    }
  }

  useEffect(() => {
    api<Preferences>("/preferences")
      .then(setPrefs)
      .catch((e: Error) => setError(e.message));
  }, []);

  async function save(change: { language?: Lang; calendar?: CalendarName }) {
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const next = await api<Preferences>("/preferences", { method: "PATCH", body: change });
      setPrefs(next);
      onSaved({ language: next.language, calendar: next.calendar });
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("app.networkError"));
    } finally {
      setBusy(false);
    }
  }

  if (error && !prefs) return <p className="error">{error}</p>;
  if (!prefs) return <p className="muted">{t("app.loading")}</p>;

  const calendarLabel = (c: CalendarName) => t(c === "jalali" ? "settings.jalali" : "settings.gregorian");
  const sample = makeFormat(lang, calendar);

  return (
    <div className="settings-page">
      <h1>{t("settings.title")}</h1>
      <p className="muted">{t("settings.intro")}</p>
      {saved && <p className="notice">{t("settings.saved")}</p>}
      {error && <p className="error">{error}</p>}

      <section className="card settings">
        <fieldset disabled={busy}>
          <legend>{t("settings.language")}</legend>
          {prefs.available.languages.map((l) => (
            <label key={l.value} className="choice">
              <input
                type="radio"
                name="language"
                value={l.value}
                checked={prefs.language === l.value}
                onChange={() => save({ language: l.value })}
              />
              <span lang={l.value} dir={l.dir}>
                {l.label}
              </span>
              {prefs.defaults.language === l.value && <small className="muted">{t("settings.default")}</small>}
            </label>
          ))}
        </fieldset>

        <fieldset disabled={busy}>
          <legend>{t("settings.calendar")}</legend>
          {prefs.available.calendars.map((c) => (
            <label key={c.value} className="choice">
              <input
                type="radio"
                name="calendar"
                value={c.value}
                checked={prefs.calendar === c.value}
                onChange={() => save({ calendar: c.value })}
              />
              <span>{calendarLabel(c.value)}</span>
              {prefs.defaults.calendar === c.value && <small className="muted">{t("settings.default")}</small>}
            </label>
          ))}
          <small className="help">{t("settings.calendarHelp")}</small>
        </fieldset>

        <fieldset disabled={busy}>
          <legend>{t("settings.timezone")}</legend>
          <select value={storedZone} onChange={(e) => saveZone(e.target.value)} dir="ltr" aria-label={t("settings.timezone")}>
            <option value="">
              {prefs.defaults.timezone} {t("settings.default")}
            </option>
            {zones.map((z) => (
              <option key={z} value={z}>
                {z}
              </option>
            ))}
          </select>
          <small className="help">{t("settings.timezoneHelp")}</small>
        </fieldset>

        <dl className="preview" aria-label={t("settings.preview")}>
          <dt>{t("settings.today")}</dt>
          <dd>{sample.date(today())}</dd>
          <dt>{t("settings.number")}</dt>
          <dd>{sample.number(1234567.89)}</dd>
        </dl>
      </section>
    </div>
  );
}
