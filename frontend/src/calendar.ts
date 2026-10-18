/**
 * Calendar arithmetic for the date picker and report labels.
 *
 * Dates travel as ISO "YYYY-MM-DD" strings (always Gregorian, like the API).
 * Jalali parts come from Intl's Persian calendar; the reverse conversion starts
 * from an estimate and corrects it with Intl, so no conversion tables are needed.
 */
import type { CalendarName } from "./i18n";

export type YMD = { y: number; m: number; d: number };

const DAY = 86_400_000;
const persian = new Intl.DateTimeFormat("en-u-ca-persian-nu-latn", {
  year: "numeric",
  month: "numeric",
  day: "numeric",
  timeZone: "UTC",
});

const pad = (n: number, w = 2) => String(n).padStart(w, "0");

export function isoOf(y: number, m: number, d: number): string {
  return `${pad(y, 4)}-${pad(m)}-${pad(d)}`;
}

/** UTC midnight timestamp of an ISO date. */
export function utc(iso: string): number {
  const [y, m, d] = iso.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
}

export function fromUtc(ms: number): string {
  const dt = new Date(ms);
  return isoOf(dt.getUTCFullYear(), dt.getUTCMonth() + 1, dt.getUTCDate());
}

export const addDays = (iso: string, n: number) => fromUtc(utc(iso) + n * DAY);
export const daysBetween = (a: string, b: string) => Math.round((utc(b) - utc(a)) / DAY);
/** 0 = Sunday … 6 = Saturday */
export const weekday = (iso: string) => new Date(utc(iso)).getUTCDay();

export function todayIso(): string {
  const now = new Date();
  return isoOf(now.getFullYear(), now.getMonth() + 1, now.getDate());
}

export function isIsoDate(v: unknown): v is string {
  return typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) && !Number.isNaN(utc(v));
}

export function toJalali(iso: string): YMD {
  const parts = Object.fromEntries(
    persian.formatToParts(new Date(utc(iso))).map((p) => [p.type, p.value]),
  );
  return { y: Number(parts.year), m: Number(parts.month), d: Number(parts.day) };
}

const jalaliDayOfYear = (m: number, d: number) => (m <= 6 ? (m - 1) * 31 + d : 186 + (m - 7) * 30 + d);

export function fromJalali(y: number, m: number, d: number): string {
  let guess = addDays(isoOf(y + 621, 3, 21), jalaliDayOfYear(m, d) - 1);
  for (let i = 0; i < 8; i++) {
    const j = toJalali(guess);
    if (j.y === y && j.m === m && j.d === d) return guess;
    // 365-day years are an estimate; when it cancels out across a leap year, step a day.
    const diff = (y - j.y) * 365 + jalaliDayOfYear(m, d) - jalaliDayOfYear(j.m, j.d) || Math.sign(y - j.y);
    guess = addDays(guess, diff);
  }
  return guess;
}

/** Year/month/day of ``iso`` in ``calendar``. */
export function partsOf(iso: string, calendar: CalendarName): YMD {
  if (calendar === "jalali") return toJalali(iso);
  const [y, m, d] = iso.split("-").map(Number);
  return { y, m, d };
}

/** ISO date of y/m/d in ``calendar``. */
export function isoFrom(calendar: CalendarName, y: number, m: number, d: number): string {
  return calendar === "jalali" ? fromJalali(y, m, d) : isoOf(y, m, d);
}

/** Normalise a (year, month±n) pair. */
export function addMonths(y: number, m: number, n: number): [number, number] {
  const index = y * 12 + (m - 1) + n;
  return [Math.floor(index / 12), (index % 12) + 1];
}

export function monthLength(calendar: CalendarName, y: number, m: number): number {
  const [ny, nm] = addMonths(y, m, 1);
  return daysBetween(isoFrom(calendar, y, m, 1), isoFrom(calendar, ny, nm, 1));
}

/** First weekday of the week: Saturday for Jalali, Monday (ISO) for Gregorian. */
export const weekStartDay = (calendar: CalendarName) => (calendar === "jalali" ? 6 : 1);
