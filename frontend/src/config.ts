import { type CalendarName, isCalendar, isLang, type Lang } from "./i18n";

type Config = {
  basePath: string;
  apiBase: string;
  language: Lang; // request language: the user's preference, else browser/project default
  calendar: CalendarName;
  defaultLanguage: Lang;
  defaultCalendar: CalendarName;
};

function read(): Config {
  const el = document.getElementById("gozar-config");
  const raw = el?.textContent ? (JSON.parse(el.textContent) as Partial<Config>) : {};
  const browser = navigator.language?.toLowerCase().startsWith("fa") ? "fa" : "en";
  const defaultLanguage = isLang(raw.defaultLanguage) ? raw.defaultLanguage : "en";
  const defaultCalendar = isCalendar(raw.defaultCalendar) ? raw.defaultCalendar : "gregorian";
  return {
    basePath: raw.basePath ?? "/gozar",
    apiBase: raw.apiBase ?? "/gozar/api",
    language: isLang(raw.language) ? raw.language : el ? defaultLanguage : browser,
    calendar: isCalendar(raw.calendar) ? raw.calendar : defaultCalendar,
    defaultLanguage,
    defaultCalendar,
  };
}

export const config = read();
