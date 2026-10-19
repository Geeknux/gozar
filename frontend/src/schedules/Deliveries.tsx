import { type MessageKey, useI18n } from "../i18n";
import type { Delivery } from "./api";

const SKIP_REASONS = ["no_email_address", "not_shared", "inactive_user"];

/** Scheduled e-mails sent, skipped or failed, one row per recipient. */
export default function Deliveries({ items, showSchedule = false }: { items: Delivery[]; showSchedule?: boolean }) {
  const { t, fmt } = useI18n();
  if (!items.length) return <p className="muted">{t("schedule.noDeliveries")}</p>;
  return (
    <table className="grid deliveries">
      <thead>
        <tr>
          <th>{t("audit.time")}</th>
          {showSchedule && <th>{t("schedule.name")}</th>}
          <th>{t("schedule.recipient")}</th>
          <th>{t("audit.status")}</th>
          <th>{t("schedule.content")}</th>
          <th>{t("audit.error")}</th>
        </tr>
      </thead>
      <tbody>
        {items.map((d) => (
          <tr key={d.id}>
            <td>{fmt.date(d.timestamp)}</td>
            {showSchedule && (
              <td>
                <bdi>{d.schedule_name}</bdi>
              </td>
            )}
            <td>
              <bdi>{d.user}</bdi> <span className="muted small">{d.email}</span>
            </td>
            <td className={`status-${d.status}`}>
              {d.status_display}
              {d.trigger === "manual" && <span className="muted small"> · {t("schedule.manual")}</span>}
            </td>
            <td>
              {t("schedule.contentCount", { reports: d.reports, attachments: d.attachments })}
              <span className="muted small">
                {" "}
                · {d.language} / {t(d.calendar === "jalali" ? "settings.jalali" : "settings.gregorian")}
              </span>
            </td>
            <td className="muted small">{SKIP_REASONS.includes(d.error) ? t(`schedule.error.${d.error}` as MessageKey) : d.error}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
