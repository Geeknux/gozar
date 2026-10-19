import DatePicker from "../components/DatePicker";
import { type MessageKey, useI18n } from "../i18n";
import { PERIODS, type Scalar } from "../reports/model";
import ValuePicker from "../reports/ValuePicker";
import type { FilterOption, Filters } from "./api";

type Props = {
  value: Filters;
  dimensions: string[];
  options: FilterOption[];
  onChange: (f: Filters) => void;
};

/** Global filters: a period (or date range) and the dashboard's category filters;
 * each applies to the widgets whose data has that field. */
export default function FilterBar({ value, dimensions, options, onChange }: Props) {
  const { t } = useI18n();
  const mode = value.period ? value.period : value.range ? "custom" : "";
  const setMode = (m: string) => {
    if (m === "custom") onChange({ ...value, period: null, range: value.range ?? ["", ""] });
    else onChange({ ...value, period: m || null, range: null });
  };
  const setRange = (i: 0 | 1, iso: string) => {
    const range: [string, string] = [...(value.range ?? ["", ""])] as [string, string];
    range[i] = iso;
    onChange({ ...value, range });
  };
  const setValues = (key: string, values: Scalar[]) => onChange({ ...value, values: { ...(value.values ?? {}), [key]: values } });

  return (
    <div className="filter-bar card" role="group" aria-label={t("dash.filters")}>
      <label className="stack">
        {t("dash.period")}
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="">{t("dash.ownPeriod")}</option>
          {PERIODS.map((p) => (
            <option key={p} value={p}>
              {t(`period.${p}` as MessageKey)}
            </option>
          ))}
          {value.period && !(PERIODS as readonly string[]).includes(value.period) && <option value={value.period}>{value.period}</option>}
          <option value="custom">{t("report.customRange")}</option>
        </select>
      </label>
      {value.range && (
        <>
          <label className="stack">
            {t("report.from")}
            <DatePicker value={value.range[0]} onChange={(v) => setRange(0, v)} />
          </label>
          <label className="stack">
            {t("report.to")}
            <DatePicker value={value.range[1]} onChange={(v) => setRange(1, v)} />
          </label>
        </>
      )}
      {dimensions.map((key) => {
        const option = options.find((o) => o.key === key);
        if (!option) return null;
        return (
          <label key={key} className="stack filter-values">
            {option.label}
            <ValuePicker
              model={option.semantic_model}
              dimension={key}
              multiple
              value={value.values?.[key] ?? []}
              onChange={(v) => setValues(key, Array.isArray(v) ? v : [v])}
            />
          </label>
        );
      })}
    </div>
  );
}
