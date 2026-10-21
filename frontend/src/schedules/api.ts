/** E-mail schedules (bi_admin.api.schedules). */
import { api } from "../api";
import type { CalendarName } from "../i18n";
import type { Person, Texts } from "../sharing";

export type Frequency = "daily" | "weekly" | "monthly";
export type Attachment = "" | "csv" | "xlsx";

export type Delivery = {
  id: number;
  timestamp: string;
  schedule: number | null;
  schedule_name: string;
  user: string;
  email: string;
  status: "sent" | "failed" | "skipped";
  status_display: string;
  trigger: "schedule" | "manual";
  language: string;
  calendar: string;
  reports: number;
  attachments: number;
  error: string;
  duration_ms: number | null;
};

export type Schedule = {
  id: number;
  name: string;
  description: string;
  name_i18n: Texts;
  description_i18n: Texts;
  target: { kind: "saved_report" | "dashboard"; id: number; name: string };
  frequency: Frequency;
  frequency_display: string;
  time: string;
  weekday: number | null;
  day_of_month: number | null;
  calendar: CalendarName;
  timezone: string;
  recipients: Person[];
  attachment: Attachment;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  deliveries?: Delivery[];
};

export type ScheduleIn = {
  name_i18n: Texts;
  description_i18n: Texts;
  saved_report?: number | null;
  dashboard?: number | null;
  frequency: Frequency;
  time: string;
  weekday: number | null;
  day_of_month: number | null;
  calendar: CalendarName;
  timezone: string;
  recipients: number[];
  attachment: Attachment;
  enabled: boolean;
};

export const listSchedules = () => api<{ schedules: Schedule[] }>("/schedules").then((r) => r.schedules);
export const getSchedule = (id: number) => api<Schedule>(`/schedules/${id}`);
export const createSchedule = (body: ScheduleIn) => api<Schedule>("/schedules", { method: "POST", body });
export const updateSchedule = (id: number, body: Partial<ScheduleIn>) => api<Schedule>(`/schedules/${id}`, { method: "PATCH", body });
export const deleteSchedule = (id: number) => api(`/schedules/${id}`, { method: "DELETE" });
export const sendSchedule = (id: number, onlyMe = false) =>
  api<{ deliveries: Delivery[] }>(`/schedules/${id}/send`, { method: "POST", params: onlyMe ? { only_me: "true" } : undefined });

/** Time zones the browser knows (falls back to a short list). */
export function timeZones(): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  try {
    const zones = intl.supportedValuesOf?.("timeZone");
    if (zones?.length) return zones;
  } catch {
    // older browsers
  }
  return ["UTC", "Asia/Tehran", "Asia/Dubai", "Europe/London", "Europe/Berlin", "America/New_York"];
}
