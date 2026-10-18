import { createContext, type ReactNode, useContext, useMemo } from "react";

import { en, type MessageKey } from "./en";
import { fa } from "./fa";

export type Lang = "en" | "fa";
export type CalendarName = "gregorian" | "jalali";
export type Prefs = { language: Lang; calendar: CalendarName };
export type { MessageKey };

const DICTS: Record<Lang, Record<MessageKey, string>> = { en, fa };
const FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹";
const ISO_DATE = /^\d{4}-\d{2}-\d{2}(T|$)/;

export const isLang = (v: unknown): v is Lang => v === "en" || v === "fa";
export const isCalendar = (v: unknown): v is CalendarName => v === "gregorian" || v === "jalali";
export const dirOf = (lang: Lang) => (lang === "fa" ? "rtl" : "ltr");

export function toLatinDigits(value: string): string {
  return value
    .replace(/[۰-۹]/g, (d) => String(FA_DIGITS.indexOf(d)))
    .replace(/[٠-٩]/g, (d) => String("٠١٢٣٤٥٦٧٨٩".indexOf(d)));
}

/** Formatting bound to one language + calendar (digits, numbers, dates, cells). */
export function makeFormat(lang: Lang, calendar: CalendarName) {
  const base = lang === "fa" ? "fa-IR" : "en-US";
  const numbering = lang === "fa" ? "arabext" : "latn";
  const dateLocale = `${base}-u-ca-${calendar === "jalali" ? "persian" : "gregory"}-nu-${numbering}`;
  const number = new Intl.NumberFormat(`${base}-u-nu-${numbering}`);
  const date = new Intl.DateTimeFormat(dateLocale, { dateStyle: "medium" });
  const dateTime = new Intl.DateTimeFormat(dateLocale, { dateStyle: "medium", timeStyle: "short" });
  const sep = DICTS[lang]["common.listSeparator"];

  const digits = (value: string | number) =>
    lang === "fa" ? String(value).replace(/[0-9]/g, (d) => FA_DIGITS[Number(d)]) : String(value);

  const formatNumber = (value: string | number) => {
    const n = typeof value === "number" ? value : Number(value);
    return Number.isFinite(n) ? number.format(n) : String(value);
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
    if (Number.isNaN(d.getTime())) return iso;
    return iso.length === 10 ? date.format(d) : dateTime.format(d);
  };

  /** Render any API cell value for display. */
  const cell = (value: unknown, kind?: string): string => {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "boolean") return value ? "✔" : "✘";
    if (typeof value === "object") {
      if (Array.isArray(value)) return value.map((v) => cell(v)).join(sep);
      const ref = value as { label?: string; name?: string };
      return ref.label ?? ref.name ?? JSON.stringify(value);
    }
    if (typeof value === "number" || kind === "decimal" || kind === "integer" || kind === "float") {
      return formatNumber(value as number);
    }
    if (typeof value === "string" && (kind === "date" || kind === "datetime" || ISO_DATE.test(value))) {
      return formatDate(value);
    }
    return digits(String(value));
  };

  return { digits, number: formatNumber, date: formatDate, cell, list: (items: string[]) => items.join(sep) };
}

export type Format = ReturnType<typeof makeFormat>;

export function translate(lang: Lang, key: MessageKey, params?: Record<string, string | number>): string {
  const template = DICTS[lang][key] ?? en[key];
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (m, name: string) => (name in params ? String(params[name]) : m));
}

type I18n = {
  lang: Lang;
  calendar: CalendarName;
  dir: "ltr" | "rtl";
  t: (key: MessageKey, params?: Record<string, string | number>) => string;
  fmt: Format;
  /** Capitalise the first letter (Django verbose names are lower-case). */
  cap: (s: string) => string;
};

function build(prefs: Prefs): I18n {
  const fmt = makeFormat(prefs.language, prefs.calendar);
  return {
    lang: prefs.language,
    calendar: prefs.calendar,
    dir: dirOf(prefs.language),
    // Numbers in messages get the language's digits.
    t: (key, params) =>
      translate(
        prefs.language,
        key,
        params &&
          Object.fromEntries(
            Object.entries(params).map(([k, v]) => [k, typeof v === "number" ? fmt.number(v) : v]),
          ),
      ),
    fmt,
    cap: (s) => (s ? s.charAt(0).toLocaleUpperCase(prefs.language) + s.slice(1) : s),
  };
}

const I18nContext = createContext<I18n>(build({ language: "en", calendar: "gregorian" }));

export function I18nProvider({ prefs, children }: { prefs: Prefs; children: ReactNode }) {
  const value = useMemo(() => build(prefs), [prefs]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export const useI18n = () => useContext(I18nContext);

/** Sync <html lang/dir> with the active language. */
export function applyDocument(lang: Lang) {
  document.documentElement.lang = lang;
  document.documentElement.dir = dirOf(lang);
}
