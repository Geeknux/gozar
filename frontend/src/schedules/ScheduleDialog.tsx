import { type FormEvent, useEffect, useState } from "react";

import Dialog from "../components/Dialog";
import PeoplePicker from "../components/PeoplePicker";
import TranslatedInput, { cleanTexts } from "../components/TranslatedInput";
import { type CalendarName, type MessageKey, useI18n } from "../i18n";
import { errorText, type Person, type Texts } from "../sharing";
import { type Attachment, createSchedule, type Frequency, type Schedule, timeZones, updateSchedule } from "./api";

type Props = {
  /** a new schedule for this report or dashboard … */
  target?: { kind: "saved_report" | "dashboard"; id: number; name: string };
  /** … or an existing one to edit */
  schedule?: Schedule;
  /** the current user: the first recipient of a new schedule */
  me?: Person | null;
  defaultTimezone?: string;
  onSaved: (s: Schedule) => void;
  onClose: () => void;
};

export const WEEKDAYS = [5, 6, 0, 1, 2, 3, 4]; // Saturday first (Monday = 0)

/** Create or edit an e-mail schedule: when, to whom, with which attachment. */
export default function ScheduleDialog({ target, schedule, me, defaultTimezone, onSaved, onClose }: Props) {
  const { t, calendar: myCalendar, lang } = useI18n();
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const [names, setNames] = useState<Texts>(schedule?.name_i18n ?? {});
  const [descriptions, setDescriptions] = useState<Texts>(schedule?.description_i18n ?? {});
  const [frequency, setFrequency] = useState<Frequency>(schedule?.frequency ?? "weekly");
  const [time, setTime] = useState(schedule?.time ?? "08:00");
  const [weekday, setWeekday] = useState<number>(schedule?.weekday ?? (lang === "fa" ? 5 : 0));
  const [day, setDay] = useState<number>(schedule?.day_of_month ?? 1);
  const [calendar, setCalendar] = useState<CalendarName>(schedule?.calendar ?? myCalendar);
  const [timezone, setTimezone] = useState(schedule?.timezone ?? defaultTimezone ?? browserZone ?? "UTC");
  const [recipients, setRecipients] = useState<Person[]>(schedule?.recipients ?? (me ? [me] : []));
  const [attachment, setAttachment] = useState<Attachment>(schedule?.attachment ?? "xlsx");
  const [enabled, setEnabled] = useState(schedule?.enabled ?? true);
  const [zones, setZones] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => setZones(timeZones()), []);
  const subject = schedule?.target ?? target;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    const body = {
      name_i18n: cleanTexts(names),
      description_i18n: cleanTexts(descriptions),
      frequency,
      time,
      weekday: frequency === "weekly" ? weekday : null,
      day_of_month: frequency === "monthly" ? day : null,
      calendar,
      timezone,
      recipients: recipients.map((r) => r.id),
      attachment,
      enabled,
    };
    try {
      const saved = schedule
        ? await updateSchedule(schedule.id, body)
        : await createSchedule({
            ...body,
            saved_report: target?.kind === "saved_report" ? target.id : null,
            dashboard: target?.kind === "dashboard" ? target.id : null,
          });
      onSaved(saved);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title={schedule ? t("schedule.editTitle") : t("schedule.newTitle")} onClose={onClose}>
      <form className="dialog-form" onSubmit={submit}>
        {subject && (
          <p className="muted">
            {t(subject.kind === "dashboard" ? "schedule.ofDashboard" : "schedule.ofReport")} <bdi>{subject.name}</bdi>
          </p>
        )}
        <TranslatedInput label={t("schedule.name")} value={names} maxLength={200} onChange={setNames} />
        <TranslatedInput label={t("schedule.description")} value={descriptions} multiline onChange={setDescriptions} />
        {subject && <p className="muted small">{t("schedule.nameHint", { name: subject.name })}</p>}
        <div className="form-grid">
          <label className="stack">
            {t("schedule.frequency")}
            <select value={frequency} onChange={(e) => setFrequency(e.target.value as Frequency)}>
              {(["daily", "weekly", "monthly"] as Frequency[]).map((f) => (
                <option key={f} value={f}>
                  {t(`schedule.${f}` as MessageKey)}
                </option>
              ))}
            </select>
          </label>
          {frequency === "weekly" && (
            <label className="stack">
              {t("schedule.weekday")}
              <select value={weekday} onChange={(e) => setWeekday(Number(e.target.value))}>
                {WEEKDAYS.map((d) => (
                  <option key={d} value={d}>
                    {t(`weekday.${d}` as MessageKey)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {frequency === "monthly" && (
            <>
              <label className="stack">
                {t("schedule.dayOfMonth")}
                <select value={day} onChange={(e) => setDay(Number(e.target.value))}>
                  {Array.from({ length: 31 }, (_, i) => i + 1).map((d) => (
                    <option key={d} value={d}>
                      {d === 31 ? t("schedule.lastDay") : d}
                    </option>
                  ))}
                </select>
              </label>
              <label className="stack">
                {t("schedule.calendar")}
                <select value={calendar} onChange={(e) => setCalendar(e.target.value as CalendarName)}>
                  <option value="gregorian">{t("settings.gregorian")}</option>
                  <option value="jalali">{t("settings.jalali")}</option>
                </select>
              </label>
            </>
          )}
          <label className="stack">
            {t("schedule.time")}
            <input type="time" value={time} required onChange={(e) => setTime(e.target.value)} dir="ltr" />
          </label>
          <label className="stack">
            {t("schedule.timezone")}
            <select value={timezone} onChange={(e) => setTimezone(e.target.value)} dir="ltr">
              {(zones.includes(timezone) ? zones : [timezone, ...zones]).map((z) => (
                <option key={z} value={z}>
                  {z}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="stack">
          {t("schedule.recipients")}
          <PeoplePicker users={recipients} withGroups={false} onChange={(users) => setRecipients(users)} />
          <span className="muted small">{t("schedule.recipientsNote")}</span>
        </div>
        <label className="stack">
          {t("schedule.attachment")}
          <select value={attachment} onChange={(e) => setAttachment(e.target.value as Attachment)}>
            <option value="xlsx">{t("schedule.xlsx")}</option>
            <option value="csv">CSV</option>
            <option value="">{t("schedule.noAttachment")}</option>
          </select>
        </label>
        <label className="check-row">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          {t("schedule.enabled")}
        </label>
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="submit" className="primary" disabled={busy || recipients.length === 0}>
            {t("form.save")}
          </button>
          <button type="button" onClick={onClose}>
            {t("common.cancel")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
