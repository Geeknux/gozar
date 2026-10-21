import type { ReactNode } from "react";

import DatePicker from "../components/DatePicker";
import { type CalendarName, type MessageKey, toLatinDigits, useI18n } from "../i18n";
import ValuePicker from "./ValuePicker";
import {
  type CatalogDimension,
  type CatalogModel,
  COMPARE_TO,
  type CompareTo,
  defaultValue,
  type Filter,
  GRAINS,
  type Grain,
  type Op,
  opsFor,
  PERIODS,
  type Scalar,
  type Spec,
  type SpecError,
} from "./model";
import { isolate } from "../bidi";

type Props = {
  catalog: CatalogModel[];
  spec: Spec;
  onChange: (spec: Spec) => void;
  errors: SpecError[];
};

/** The report builder form; every change goes straight into the (URL) spec. */
export default function Builder({ catalog, spec, onChange, errors }: Props) {
  const { t, calendar: userCalendar } = useI18n();
  const model = catalog.find((m) => m.name === spec.semantic_model);
  if (!model) return null;

  const dims = Object.fromEntries(model.dimensions.map((d) => [d.key, d]));
  const timeDims = model.dimensions.filter((d) => d.type === "time");
  const timeDim = spec.time_dimension ?? model.time_dimension ?? timeDims[0]?.key ?? null;
  const filters = spec.filters ?? [];
  const rangeIndex = filters.findIndex((f) => f.field === timeDim && f.op === "between");
  const range = rangeIndex >= 0 ? (filters[rangeIndex].value as Scalar[]) : null;
  const periodMode = spec.relative_period ? spec.relative_period : range ? "custom" : "";
  const set = (patch: Partial<Spec>) => onChange({ ...spec, ...patch });

  const errorsAt = (match: (path: string) => boolean) => errors.filter((e) => match(e.path));
  const show = (list: SpecError[]) =>
    list.length > 0 && (
      <ul className="field-errors">
        {list.map((e, i) => (
          <li key={i} className="field-error">
            {e.message}
            {e.suggestions.length > 0 && e.code !== "no_measures" && (
              <span className="muted">
                {" "}
                {t("report.suggestions", {
                  items: e.suggestions
                    .map((s) => model.measures.find((m) => m.key === s)?.label ?? dims[s]?.label ?? s)
                    .join(t("common.listSeparator")),
                })}
              </span>
            )}
          </li>
        ))}
      </ul>
    );

  const toggle = (list: string[], key: string) =>
    list.includes(key) ? list.filter((k) => k !== key) : [...list, key];

  const setFilter = (i: number, f: Filter | null) => {
    const next = [...filters];
    if (f) next[i] = f;
    else next.splice(i, 1);
    set({ filters: next });
  };

  const setPeriod = (mode: string) => {
    let next = filters;
    if (mode !== "custom" && rangeIndex >= 0) next = filters.filter((_, i) => i !== rangeIndex);
    if (mode === "custom" && rangeIndex < 0 && timeDim) next = [...filters, { field: timeDim, op: "between", value: ["", ""] }];
    set({ relative_period: mode && mode !== "custom" ? mode : null, filters: next });
  };

  const setRange = (pos: 0 | 1, iso: string) => {
    const value = [...(range ?? ["", ""])];
    value[pos] = iso;
    setFilter(rangeIndex, { field: timeDim!, op: "between", value });
  };

  const sortable = [
    ...(spec.time_grain && timeDim && !(spec.dimensions ?? []).includes(timeDim) ? [timeDim] : []),
    ...(spec.dimensions ?? []),
    ...spec.measures,
  ];
  const labelOf = (key: string) => model.measures.find((m) => m.key === key)?.label ?? dims[key]?.label ?? key;
  const calendarName = (c: CalendarName) => t(c === "jalali" ? "settings.jalali" : "settings.gregorian");

  return (
    <div className="builder card">
      <Section title={t("report.model")} errors={show(errorsAt((p) => p === "semantic_model"))}>
        <select
          value={spec.semantic_model}
          onChange={(e) => {
            const next = catalog.find((m) => m.name === e.target.value)!;
            onChange({ semantic_model: next.name, measures: next.measures.slice(0, 1).map((m) => m.key), chart: spec.chart });
          }}
        >
          {catalog.map((m) => (
            <option key={m.name} value={m.name}>
              {isolate(m.label)}
            </option>
          ))}
        </select>
        {model.description && <small className="help"><bdi>{model.description}</bdi></small>}
      </Section>

      <Section title={t("report.measures")} errors={show(errorsAt((p) => p.startsWith("measures")))}>
        {model.measures.map((m) => (
          <label key={m.key} className="check-row" title={isolate(m.description)}>
            <input
              type="checkbox"
              checked={spec.measures.includes(m.key)}
              onChange={() => set({ measures: toggle(spec.measures, m.key) })}
            />
            <bdi>{m.label}</bdi>
          </label>
        ))}
      </Section>

      <Section title={t("report.dimensions")} errors={show(errorsAt((p) => p.startsWith("dimensions")))}>
        {model.dimensions.map((d) => (
          <label key={d.key} className="check-row" title={isolate(d.description)}>
            <input
              type="checkbox"
              checked={(spec.dimensions ?? []).includes(d.key)}
              onChange={() => set({ dimensions: toggle(spec.dimensions ?? [], d.key) })}
            />
            <bdi>{d.label}</bdi>
          </label>
        ))}
      </Section>

      {timeDims.length > 0 && (
        <Section
          title={t("report.time")}
          errors={show(
            errorsAt(
              (p) =>
                ["time_dimension", "time_grain", "relative_period", "calendar", "compare_to"].includes(p) ||
                (rangeIndex >= 0 && p.startsWith(`filters[${rangeIndex}]`)),
            ),
          )}
        >
          <label className="stack">
            {t("report.timeDimension")}
            <select value={timeDim ?? ""} onChange={(e) => set({ time_dimension: e.target.value || null })}>
              {timeDims.map((d) => (
                <option key={d.key} value={d.key}>
                  {isolate(d.label)}
                </option>
              ))}
            </select>
          </label>
          <label className="stack">
            {t("report.grain")}
            <select
              value={spec.time_grain ?? ""}
              onChange={(e) => set({ time_grain: (e.target.value || null) as Grain | null })}
            >
              <option value="">{t("report.noGrain")}</option>
              {GRAINS.map((g) => (
                <option key={g} value={g}>
                  {t(`grain.${g}` as MessageKey)}
                </option>
              ))}
            </select>
          </label>
          <label className="stack">
            {t("report.calendar")}
            <select
              value={spec.calendar ?? ""}
              onChange={(e) => set({ calendar: (e.target.value || null) as CalendarName | null })}
            >
              <option value="">{t("report.calendarDefault", { calendar: calendarName(userCalendar) })}</option>
              <option value="gregorian">{calendarName("gregorian")}</option>
              <option value="jalali">{calendarName("jalali")}</option>
            </select>
          </label>
          <label className="stack">
            {t("report.period")}
            <select value={periodMode} onChange={(e) => setPeriod(e.target.value)}>
              <option value="">{t("report.allTime")}</option>
              {PERIODS.map((p) => (
                <option key={p} value={p}>
                  {t(`period.${p}` as MessageKey)}
                </option>
              ))}
              {spec.relative_period && !(PERIODS as readonly string[]).includes(spec.relative_period) && (
                <option value={spec.relative_period}>{spec.relative_period}</option>
              )}
              <option value="custom">{t("report.customRange")}</option>
            </select>
          </label>
          {range && (
            <div className="range">
              <label className="stack">
                {t("report.from")}
                <DatePicker value={String(range[0] ?? "")} onChange={(v) => setRange(0, v)} calendar={spec.calendar ?? undefined} />
              </label>
              <label className="stack">
                {t("report.to")}
                <DatePicker value={String(range[1] ?? "")} onChange={(v) => setRange(1, v)} calendar={spec.calendar ?? undefined} />
              </label>
            </div>
          )}
          <label className="stack">
            {t("report.compareTo")}
            <select
              value={spec.compare_to ?? ""}
              onChange={(e) => set({ compare_to: (e.target.value || null) as CompareTo | null })}
            >
              <option value="">{t("compare.none")}</option>
              {COMPARE_TO.map((c) => (
                <option key={c} value={c}>
                  {t(`compare.${c}` as MessageKey)}
                </option>
              ))}
            </select>
          </label>
        </Section>
      )}

      <Section title={t("report.filters")} errors={null}>
        {filters.map((f, i) =>
          i === rangeIndex ? null : (
            <FilterRow
              key={i}
              filter={f}
              model={model}
              calendar={spec.calendar ?? undefined}
              onChange={(next) => setFilter(i, next)}
              errors={show(errorsAt((p) => p.startsWith(`filters[${i}]`)))}
            />
          ),
        )}
        <button
          type="button"
          className="link"
          onClick={() => {
            const d = model.dimensions.find((x) => x.type !== "time") ?? model.dimensions[0];
            set({ filters: [...filters, { field: d.key, op: "eq", value: "" }] });
          }}
        >
          {t("report.addFilter")}
        </button>
      </Section>

      <Section title={t("report.sort")} errors={show(errorsAt((p) => p.startsWith("sort")))}>
        {(spec.sort ?? []).map((s, i) => (
          <div key={i} className="inline-row">
            <select
              value={s.field}
              onChange={(e) => set({ sort: (spec.sort ?? []).map((x, j) => (j === i ? { ...x, field: e.target.value } : x)) })}
            >
              {[...new Set([s.field, ...sortable])].map((k) => (
                <option key={k} value={k}>
                  {labelOf(k)}
                </option>
              ))}
            </select>
            <select
              value={s.direction}
              onChange={(e) =>
                set({
                  sort: (spec.sort ?? []).map((x, j) => (j === i ? { ...x, direction: e.target.value as "asc" | "desc" } : x)),
                })
              }
            >
              <option value="desc">{t("report.descending")}</option>
              <option value="asc">{t("report.ascending")}</option>
            </select>
            <button
              type="button"
              className="link"
              aria-label={t("report.remove")}
              onClick={() => set({ sort: (spec.sort ?? []).filter((_, j) => j !== i) })}
            >
              ×
            </button>
          </div>
        ))}
        {sortable.length > 0 && (
          <button
            type="button"
            className="link"
            onClick={() => set({ sort: [...(spec.sort ?? []), { field: sortable[0], direction: "desc" }] })}
          >
            {t("report.addSort")}
          </button>
        )}
      </Section>

      <Section title={t("report.limit")} errors={show(errorsAt((p) => p === "limit"))}>
        <input
          type="text"
          inputMode="numeric"
          dir="ltr"
          className="narrow"
          value={spec.limit ?? ""}
          onChange={(e) => {
            const raw = toLatinDigits(e.target.value).replace(/\D/g, "");
            set({ limit: raw ? Number(raw) : null });
          }}
        />
      </Section>

      {show(
        errorsAt(
          (p) =>
            !/^(semantic_model|measures|dimensions|filters|sort|limit|time_dimension|time_grain|relative_period|calendar|compare_to)/.test(p),
        ),
      )}
    </div>
  );
}

function Section({ title, errors, children }: { title: string; errors: ReactNode; children: ReactNode }) {
  return (
    <fieldset className="builder-section">
      <legend>{title}</legend>
      {children}
      {errors}
    </fieldset>
  );
}

type FilterRowProps = {
  filter: Filter;
  model: CatalogModel;
  calendar?: CalendarName;
  onChange: (f: Filter | null) => void;
  errors: ReactNode;
};

function FilterRow({ filter, model, calendar, onChange, errors }: FilterRowProps) {
  const { t } = useI18n();
  const dim: CatalogDimension | undefined = model.dimensions.find((d) => d.key === filter.field);
  const ops = dim ? opsFor(dim.type) : (["eq"] as Op[]);
  const op = ops.includes(filter.op) ? filter.op : ops[0];
  const setValue = (value: Filter["value"]) => onChange({ ...filter, op, value });

  return (
    <div className="filter-row">
      <div className="inline-row">
        <select
          value={filter.field}
          onChange={(e) => {
            const next = model.dimensions.find((d) => d.key === e.target.value)!;
            const nextOp = opsFor(next.type)[0];
            onChange({ field: next.key, op: nextOp, value: defaultValue(nextOp) });
          }}
        >
          {model.dimensions.map((d) => (
            <option key={d.key} value={d.key}>
              {isolate(d.label)}
            </option>
          ))}
        </select>
        <select value={op} onChange={(e) => onChange({ ...filter, op: e.target.value as Op, value: defaultValue(e.target.value as Op) })}>
          {ops.map((o) => (
            <option key={o} value={o}>
              {t(`op.${o}` as MessageKey)}
            </option>
          ))}
        </select>
        <button type="button" className="link" aria-label={t("report.remove")} onClick={() => onChange(null)}>
          ×
        </button>
      </div>
      {dim && <FilterValue dim={dim} model={model.name} op={op} value={filter.value} onChange={setValue} calendar={calendar} />}
      {errors}
    </div>
  );
}

type FilterValueProps = {
  dim: CatalogDimension;
  model: string;
  op: Op;
  value: Filter["value"];
  onChange: (v: Filter["value"]) => void;
  calendar?: CalendarName;
};

function FilterValue({ dim, model, op, value, onChange, calendar }: FilterValueProps) {
  const { t } = useI18n();
  if (op === "is_null") {
    return (
      <select value={value === false ? "false" : "true"} onChange={(e) => onChange(e.target.value === "true")}>
        <option value="true">{t("common.yes")}</option>
        <option value="false">{t("common.no")}</option>
      </select>
    );
  }
  if (dim.type === "boolean") {
    return (
      <select value={String(value ?? "")} onChange={(e) => onChange(e.target.value === "true")}>
        <option value="" disabled>
          {t("relation.choose")}
        </option>
        <option value="true">{t("common.yes")}</option>
        <option value="false">{t("common.no")}</option>
      </select>
    );
  }
  if (dim.type === "time") {
    if (op === "between") {
      const pair = Array.isArray(value) ? value : ["", ""];
      return (
        <div className="range">
          <DatePicker value={String(pair[0] ?? "")} onChange={(v) => onChange([v, pair[1] ?? ""])} calendar={calendar} />
          <DatePicker value={String(pair[1] ?? "")} onChange={(v) => onChange([pair[0] ?? "", v])} calendar={calendar} />
        </div>
      );
    }
    return <DatePicker value={typeof value === "string" ? value : ""} onChange={onChange} calendar={calendar} />;
  }
  if (dim.type === "number") {
    const numberInput = (v: Scalar | null | undefined, set: (s: string) => void) => (
      <input type="text" inputMode="decimal" dir="ltr" value={v === null || v === undefined ? "" : String(v)} onChange={(e) => set(toLatinDigits(e.target.value))} />
    );
    if (op === "between") {
      const pair = Array.isArray(value) ? value : ["", ""];
      return (
        <div className="range">
          {numberInput(pair[0], (s) => onChange([s, pair[1] ?? ""]))}
          {numberInput(pair[1], (s) => onChange([pair[0] ?? "", s]))}
        </div>
      );
    }
    if (op === "in" || op === "not_in") {
      return <ValuePicker model={model} dimension={dim.key} multiple value={value} onChange={onChange} />;
    }
    return numberInput(Array.isArray(value) ? "" : value, onChange);
  }
  if (op === "contains") {
    return <input dir="auto" value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value)} />;
  }
  return (
    <ValuePicker model={model} dimension={dim.key} multiple={op === "in" || op === "not_in"} value={value} onChange={onChange} />
  );
}
