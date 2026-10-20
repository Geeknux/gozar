import { Link } from "react-router-dom";

import type { InlineErrors } from "../api";
import { type Errors, filePayload, fromApi, toPayload, type Values } from "../forms";
import { useI18n } from "../i18n";
import type { FieldMeta, InlineMeta, InlineRow } from "../types";
import FieldInput from "./FieldInput";
import ReadonlyValue from "./ReadonlyValue";

/** One row of an inline as the form holds it. */
export type RowState = {
  key: string; // stable client id
  pk: string | number | null;
  label: string;
  values: Values;
  initial: Values;
  dirty: Set<string>;
  deleted: boolean;
};

let counter = 0;
const newKey = () => `new-${++counter}`;

export function inlineState(meta: InlineMeta, rows: InlineRow[] | undefined, withExtra: boolean): RowState[] {
  const fields = Object.fromEntries(meta.field_meta.map((f) => [f.name, f]));
  const toValues = (raw: Record<string, unknown>) =>
    Object.fromEntries(meta.fields.filter((n) => fields[n]).map((n) => [n, fromApi(fields[n], raw[n])]));
  const out: RowState[] = (rows ?? []).map((r) => ({
    key: `pk-${r.pk}`,
    pk: r.pk,
    label: r.label,
    values: toValues(r.values),
    initial: toValues(r.values),
    dirty: new Set(),
    deleted: false,
  }));
  if (withExtra && meta.permissions.add) {
    const room = meta.max_num === null ? meta.extra : Math.max(0, Math.min(meta.extra, meta.max_num - out.length));
    for (let i = 0; i < room; i++) out.push(blankRow(meta));
  }
  return out;
}

export function blankRow(meta: InlineMeta): RowState {
  const fields = Object.fromEntries(meta.field_meta.map((f) => [f.name, f]));
  const values = Object.fromEntries(meta.fields.filter((n) => fields[n]).map((n) => [n, fromApi(fields[n], meta.defaults[n])]));
  return { key: newKey(), pk: null, label: "", values, initial: values, dirty: new Set(), deleted: false };
}

/** The ``__inlines__`` rows to send and the row keys in the same order. */
export function inlinePayload(
  meta: InlineMeta,
  rows: RowState[],
  files: Map<string, File>,
  saveAsNew = false,
): { rows: Values[]; keys: string[] } {
  const fields = Object.fromEntries(meta.field_meta.map((f) => [f.name, f]));
  const editable = meta.fields.filter((n) => fields[n] && !meta.readonly_fields.includes(n));
  const out: Values[] = [];
  const keys: string[] = [];
  for (const row of rows) {
    const isNew = row.pk === null || saveAsNew;
    if (row.deleted && isNew) continue;
    if (row.deleted) {
      out.push({ pk: row.pk, delete: true });
      keys.push(row.key);
      continue;
    }
    // untouched rows stay as they are; empty extra rows are never sent (save as new
    // copies the stored rows, not the blank ones)
    if (!row.dirty.size && (row.pk === null || !saveAsNew)) continue;
    const index = out.length;
    const values: Values = {};
    for (const name of editable) {
      if (!isNew && !row.dirty.has(name)) continue;
      const field = fields[name];
      if (field.kind === "file") filePayload(row.values[name], `${meta.name}.${index}.${name}`, values, name, files, saveAsNew);
      else values[name] = toPayload(field, row.values[name]);
    }
    out.push({ pk: isNew ? null : row.pk, values });
    keys.push(row.key);
  }
  return { rows: out, keys };
}

type Props = {
  meta: InlineMeta;
  model: string; // the parent model's label
  rows: RowState[];
  hidden: number;
  readOnly: boolean;
  errors?: InlineErrors;
  errorKeys: string[]; // row keys in the order of the last submission (maps error indexes)
  onChange: (rows: RowState[]) => void;
};

export default function InlineEditor({ meta, model, rows, hidden, readOnly, errors, errorKeys, onChange }: Props) {
  const { t, cap, fmt } = useI18n();
  const fields: Record<string, FieldMeta> = Object.fromEntries(meta.field_meta.map((f) => [f.name, f]));
  const names = meta.fields.filter((n) => fields[n]);
  const ro = new Set(meta.readonly_fields);
  const live = rows.filter((r) => !r.deleted || r.pk !== null);
  const count = rows.filter((r) => !r.deleted && (r.pk !== null || r.dirty.size)).length;
  const canAdd = !readOnly && meta.permissions.add && (meta.max_num === null || rows.filter((r) => !r.deleted).length < meta.max_num);
  const rowErrors = (key: string): Errors | undefined => {
    const i = errorKeys.indexOf(key);
    return i >= 0 ? errors?.rows[String(i)] : undefined;
  };

  const update = (key: string, change: (r: RowState) => RowState) => onChange(rows.map((r) => (r.key === key ? change(r) : r)));
  const setValue = (key: string, name: string, value: unknown) =>
    update(key, (r) => ({ ...r, values: { ...r.values, [name]: value }, dirty: new Set(r.dirty).add(name) }));
  const editableRow = (r: RowState) => !readOnly && (r.pk === null ? meta.permissions.add : meta.permissions.change) && !r.deleted;
  const canDelete = (r: RowState) => !readOnly && (r.pk === null || (meta.can_delete && meta.permissions.delete));
  const toggleDelete = (r: RowState) => {
    if (r.pk === null) onChange(rows.filter((x) => x.key !== r.key));
    else update(r.key, (x) => ({ ...x, deleted: !x.deleted }));
  };
  const changeLink = (r: RowState) =>
    meta.show_change_link && r.pk !== null ? (
      <Link to={`/${meta.model.replace(".", "/")}/${r.pk}`}>{t("inline.changeLink")}</Link>
    ) : null;

  const cell = (r: RowState, name: string, compact: boolean) => {
    const field = fields[name];
    const errs = rowErrors(r.key)?.[name];
    const body =
      ro.has(name) || !editableRow(r) ? (
        <ReadonlyValue field={field} value={r.values[name]} />
      ) : (
        <FieldInput
          id={`${meta.name}-${r.key}-${name}`}
          field={field}
          widget={meta.widgets[name]}
          model={model}
          inline={meta.name}
          value={r.values[name]}
          initial={r.initial[name]}
          onChange={(v) => setValue(r.key, name, v)}
          compact={compact}
        />
      );
    return (
      <>
        {body}
        {errs?.map((m) => (
          <small key={m} className="field-error">
            {m}
          </small>
        ))}
      </>
    );
  };

  return (
    <section className={`card inline inline-${meta.style}`} aria-label={cap(meta.verbose_name_plural)}>
      <div className="inline-head">
        <h2>{cap(meta.verbose_name_plural)}</h2>
        <span className="muted">
          {meta.max_num !== null ? t("inline.countMax", { count, max: meta.max_num }) : t("inline.count", { count })}
        </span>
      </div>
      {hidden > 0 && <p className="help">{t("inline.hidden", { count: hidden })}</p>}
      {errors?.non_form?.map((m) => (
        <p key={m} className="error">
          {m}
        </p>
      ))}

      {meta.style === "tabular" ? (
        <div className="table-wrap">
          <table className="grid inline-table">
            <thead>
              <tr>
                {names.map((n) => (
                  <th key={n} className={fields[n].required && !ro.has(n) && !readOnly ? "required" : ""}>
                    {cap(fields[n].verbose_name)}
                  </th>
                ))}
                <th className="inline-delete">{readOnly ? "" : t("common.delete")}</th>
              </tr>
            </thead>
            <tbody>
              {live.map((r) => {
                const general = rowErrors(r.key)?.__all__;
                return (
                  <FragmentRow key={r.key} span={names.length + 1} general={general}>
                    <tr className={r.deleted ? "deleted" : r.pk === null ? "new-row" : ""}>
                      {names.map((n) => (
                        <td key={n} className={`kind-${fields[n].kind}`}>
                          {cell(r, n, true)}
                        </td>
                      ))}
                      <td className="inline-delete">
                        {canDelete(r) &&
                          (r.pk === null ? (
                            <button type="button" className="link" aria-label={t("inline.removeRow")} onClick={() => toggleDelete(r)}>
                              ×
                            </button>
                          ) : (
                            <input type="checkbox" aria-label={t("common.delete")} checked={r.deleted} onChange={() => toggleDelete(r)} />
                          ))}
                        {changeLink(r)}
                      </td>
                    </tr>
                  </FragmentRow>
                );
              })}
            </tbody>
          </table>
          {!live.length && <p className="empty">{t("inline.empty")}</p>}
        </div>
      ) : (
        <div className="stacked-rows">
          {live.map((r, i) => (
            <fieldset key={r.key} className={`stacked-row${r.deleted ? " deleted" : ""}`}>
              <legend>
                <bdi>{r.pk === null ? t("inline.newRow", { model: meta.verbose_name }) : r.label || `#${fmt.digits(String(r.pk))}`}</bdi>{" "}
                <span className="muted">#{fmt.number(i + 1)}</span>
              </legend>
              <div className="stacked-tools">
                {changeLink(r)}
                {canDelete(r) &&
                  (r.pk === null ? (
                    <button type="button" className="link" onClick={() => toggleDelete(r)}>
                      {t("inline.removeRow")}
                    </button>
                  ) : (
                    <label className="check-row">
                      <input type="checkbox" checked={r.deleted} onChange={() => toggleDelete(r)} /> {t("common.delete")}
                    </label>
                  ))}
              </div>
              {rowErrors(r.key)?.__all__?.map((m) => (
                <p key={m} className="error">
                  {m}
                </p>
              ))}
              {names.map((n) => (
                <div key={n} className={`form-row${rowErrors(r.key)?.[n] ? " has-error" : ""}`}>
                  <label htmlFor={`${meta.name}-${r.key}-${n}`} className={fields[n].required && !ro.has(n) ? "required" : ""}>
                    {cap(fields[n].verbose_name)}
                  </label>
                  <div className="form-input">{cell(r, n, false)}</div>
                </div>
              ))}
            </fieldset>
          ))}
          {!live.length && <p className="empty">{t("inline.empty")}</p>}
        </div>
      )}

      {canAdd && (
        <button type="button" className="link inline-add" onClick={() => onChange([...rows, blankRow(meta)])}>
          {t("inline.add", { model: meta.verbose_name })}
        </button>
      )}
    </section>
  );
}

function FragmentRow({ children, general, span }: { children: React.ReactNode; general?: string[]; span: number }) {
  return (
    <>
      {general?.length ? (
        <tr className="row-errors">
          <td colSpan={span}>
            {general.map((m) => (
              <span key={m} className="error">
                {m}
              </span>
            ))}
          </td>
        </tr>
      ) : null}
      {children}
    </>
  );
}
