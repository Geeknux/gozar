import { useEffect, useMemo, useRef, useState } from "react";

import {
  addDays,
  addMonths,
  isIsoDate,
  isoFrom,
  monthLength,
  partsOf,
  todayIso,
  weekday,
  weekStartDay,
} from "../calendar";
import { type CalendarName, useI18n } from "../i18n";

type Props = {
  value: string; // ISO "YYYY-MM-DD" or ""
  onChange: (iso: string) => void;
  /** Defaults to the user's calendar. */
  calendar?: CalendarName;
  id?: string;
  placeholder?: string;
  disabled?: boolean;
  clearable?: boolean;
};

/**
 * Date input that shows a Jalali month grid (Saturday-first weeks) when the
 * user's calendar is Jalali and a Gregorian one (Monday-first) otherwise. The
 * value is always a Gregorian ISO date, as the API expects.
 */
export default function DatePicker({ value, onChange, calendar, id, placeholder, disabled, clearable = true }: Props) {
  const i18n = useI18n();
  const { t, fmt } = i18n;
  const cal = calendar ?? i18n.calendar;
  const valid = isIsoDate(value) ? value : "";
  const [open, setOpen] = useState(false);
  const [view, setView] = useState(() => partsOf(valid || todayIso(), cal));
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) setView(partsOf(valid || todayIso(), cal));
  }, [open, valid, cal]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const grid = useMemo(() => {
    const first = isoFrom(cal, view.y, view.m, 1);
    const lead = (weekday(first) - weekStartDay(cal) + 7) % 7;
    const days = monthLength(cal, view.y, view.m);
    const cells: (string | null)[] = Array(lead).fill(null);
    for (let d = 0; d < days; d++) cells.push(addDays(first, d));
    while (cells.length % 7) cells.push(null);
    return { first, cells };
  }, [cal, view.y, view.m]);

  const weekdays = useMemo(() => {
    // Any week works: start from a known date with the right weekday.
    const start = addDays("2026-01-04", (weekStartDay(cal) + 7) % 7); // 2026-01-04 is a Sunday
    return Array.from({ length: 7 }, (_, i) => fmt.dateIn(addDays(start, i), cal, { weekday: "narrow" }));
  }, [cal, fmt]);

  const move = (n: number) => {
    const [y, m] = addMonths(view.y, view.m, n);
    setView({ y, m, d: 1 });
  };
  const pick = (iso: string) => {
    onChange(iso);
    setOpen(false);
  };
  const today = todayIso();

  return (
    <div className="date-picker" ref={box}>
      <button
        type="button"
        id={id}
        className="date-value"
        disabled={disabled}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={valid ? "" : "muted"}>
          {valid ? fmt.dateIn(valid, cal, { dateStyle: "medium" }) : (placeholder ?? t("date.choose"))}
        </span>
        <span aria-hidden>📅</span>
      </button>
      {open && (
        <div className="date-popup" role="dialog" aria-label={t("date.choose")}>
          <div className="date-head">
            <button type="button" className="link" aria-label={t("date.previousMonth")} onClick={() => move(-1)}>
              {i18n.dir === "rtl" ? "›" : "‹"}
            </button>
            <strong>{fmt.dateIn(grid.first, cal, { month: "long", year: "numeric" })}</strong>
            <button type="button" className="link" aria-label={t("date.nextMonth")} onClick={() => move(1)}>
              {i18n.dir === "rtl" ? "‹" : "›"}
            </button>
          </div>
          <div className="date-grid">
            {weekdays.map((w, i) => (
              <span key={`w${i}`} className="date-weekday">
                {w}
              </span>
            ))}
            {grid.cells.map((iso, i) =>
              iso ? (
                <button
                  key={iso}
                  type="button"
                  data-date={iso}
                  className={[iso === valid ? "selected" : "", iso === today ? "today" : ""].join(" ")}
                  onClick={() => pick(iso)}
                >
                  {fmt.digits(partsOf(iso, cal).d)}
                </button>
              ) : (
                <span key={`e${i}`} />
              ),
            )}
          </div>
          <div className="date-foot">
            <button type="button" className="link" onClick={() => pick(today)}>
              {t("date.today")}
            </button>
            {clearable && valid && (
              <button type="button" className="link" onClick={() => pick("")}>
                {t("date.clear")}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

type DateTimeProps = { value: string; onChange: (local: string) => void; id?: string };

function pad(n: number) {
  return String(n).padStart(2, "0");
}

/** Local "YYYY-MM-DDTHH:MM" from an ISO datetime with offset (browser time zone). */
export function toLocalDateTime(value: string): string {
  if (!value) return "";
  if (!/[zZ]|[+-]\d\d:?\d\d$/.test(value)) return value.slice(0, 16);
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Date picker + time input; emits a local "YYYY-MM-DDTHH:MM" string (or ""). */
export function DateTimePicker({ value, onChange, id }: DateTimeProps) {
  const { t } = useI18n();
  const local = toLocalDateTime(value);
  const date = local.slice(0, 10);
  const time = local.slice(11, 16) || "00:00";
  return (
    <div className="date-time">
      <DatePicker id={id} value={date} onChange={(d) => onChange(d ? `${d}T${time}` : "")} />
      <input
        type="time"
        aria-label={t("date.time")}
        value={date ? time : ""}
        disabled={!date}
        onChange={(e) => onChange(`${date}T${e.target.value || "00:00"}`)}
      />
    </div>
  );
}
