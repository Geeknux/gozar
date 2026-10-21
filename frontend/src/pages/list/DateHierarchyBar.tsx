import { useI18n } from "../../i18n";
import type { DateHierarchy } from "../../types";

type Props = {
  dh: DateHierarchy;
  update: (changes: Record<string, string | null>) => void;
};

/** admin's date_hierarchy: years → months → days with row counts, in the user's
 * calendar (Jalali months are real Jalali months, counted in the database). */
export default function DateHierarchyBar({ dh, update }: Props) {
  const { t, fmt } = useI18n();
  const { year, month, day } = dh.selected;
  const cal = dh.calendar;
  const go = (y: number | null, m: number | null = null, d: number | null = null) =>
    update({
      dh_year: y === null ? null : String(y),
      dh_month: m === null ? null : String(m),
      dh_day: d === null ? null : String(d),
      dh_calendar: y === null ? null : cal,
    });
  const label = (iso: string, opts: Intl.DateTimeFormatOptions) => fmt.dateIn(iso, cal, opts);
  const start = dh.start;

  const path: { text: string; onClick?: () => void }[] = [];
  if (year !== null) path.push({ text: t("dh.all"), onClick: () => go(null) });
  if (year !== null && start) path.push({ text: label(start, { year: "numeric" }), onClick: month !== null ? () => go(year) : undefined });
  if (month !== null && start) path.push({ text: label(start, { month: "long" }), onClick: day !== null ? () => go(year, month) : undefined });
  if (day !== null && start) path.push({ text: label(start, { day: "numeric" }) });

  return (
    <nav className="date-hierarchy" aria-label={dh.label}>
      <span className="muted"><bdi>{dh.label}</bdi></span>
      {path.map((p, i) =>
        p.onClick ? (
          <button key={i} type="button" className="link" onClick={p.onClick}>
            {p.text}
          </button>
        ) : (
          <strong key={i}>{p.text}</strong>
        ),
      )}
      {dh.level !== null && (
        <span className="dh-buckets">
          {dh.buckets.map((b) => (
            <button
              key={b.start}
              type="button"
              className="chip-button"
              title={t("list.count", { count: b.count })}
              onClick={() => go(b.year, b.month ?? null, b.day ?? null)}
            >
              {dh.level === "year"
                ? label(b.start, { year: "numeric" })
                : dh.level === "month"
                  ? label(b.start, { month: "long" })
                  : label(b.start, { day: "numeric" })}{" "}
              <span className="muted">{fmt.number(b.count)}</span>
            </button>
          ))}
          {!dh.buckets.length && <span className="muted">{t("list.empty")}</span>}
        </span>
      )}
    </nav>
  );
}
