import { type FormEvent, useState } from "react";

import { ApiError, api, crudPath, errorText } from "../../api";
import Dialog from "../../components/Dialog";
import FieldInput from "../../components/FieldInput";
import { fromApi, toPayload, type Values } from "../../forms";
import { useI18n } from "../../i18n";
import type { ModelMeta } from "../../types";

type Props = {
  meta: ModelMeta;
  selection: { pks: string[] } | { all: true; params: Record<string, string> };
  count: number;
  onClose: () => void;
  onDone: (updated: number) => void;
};

/** Set chosen fields to the same value on every selected row (each row is
 * validated and logged; one failing row cancels the whole edit). */
export default function BulkEditDialog({ meta, selection, count, onClose, onDone }: Props) {
  const { t, cap } = useI18n();
  const fields = meta.fields.filter((f) => meta.list.bulk_edit_fields.includes(f.name));
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [values, setValues] = useState<Values>(() => Object.fromEntries(fields.map((f) => [f.name, fromApi(f, null)])));
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [rowErrors, setRowErrors] = useState<Record<string, Record<string, string[]>>>({});
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    setRowErrors({});
    const body = Object.fromEntries(fields.filter((f) => chosen.has(f.name)).map((f) => [f.name, toPayload(f, values[f.name])]));
    try {
      const r = await api<{ updated: number }>(crudPath(meta.label, "bulk-edit"), {
        method: "POST",
        body: { ...selection, values: body },
      });
      onDone(r.updated);
    } catch (err) {
      if (err instanceof ApiError && (err.data.errors || err.data.row_errors)) {
        setErrors(err.data.errors ?? {});
        setRowErrors(err.data.row_errors ?? {});
      } else setErrors({ __all__: [errorText(err)] });
    } finally {
      setBusy(false);
    }
  }

  const failing = Object.keys(rowErrors).length;
  return (
    <Dialog title={t("bulk.title", { count, model: meta.verbose_name_plural })} onClose={onClose} wide>
      <form className="dialog-form" onSubmit={submit}>
        <p className="help">{t("bulk.intro")}</p>
        {errors.__all__?.map((m) => (
          <p key={m} className="error">
            {m}
          </p>
        ))}
        {failing > 0 && <p className="error">{t("bulk.rowsFailed", { count: failing })}</p>}
        {fields.map((f) => (
          <div key={f.name} className={`form-row bulk-row${errors[f.name] ? " has-error" : ""}`}>
            <label className="check-row">
              <input
                type="checkbox"
                checked={chosen.has(f.name)}
                onChange={(e) => {
                  const next = new Set(chosen);
                  if (e.target.checked) next.add(f.name);
                  else next.delete(f.name);
                  setChosen(next);
                }}
              />
              {cap(f.verbose_name)}
            </label>
            <div className="form-input">
              {chosen.has(f.name) ? (
                <FieldInput
                  id={`bulk-${f.name}`}
                  field={f}
                  widget={meta.form.widgets[f.name]}
                  model={meta.label}
                  value={values[f.name]}
                  onChange={(v) => setValues((cur) => ({ ...cur, [f.name]: v }))}
                  isNew
                />
              ) : (
                <span className="muted">{t("bulk.unchanged")}</span>
              )}
              {errors[f.name]?.map((m) => (
                <small key={m} className="field-error">
                  {m}
                </small>
              ))}
            </div>
          </div>
        ))}
        <div className="dialog-actions">
          <button type="submit" className="primary" disabled={busy || !chosen.size}>
            {t("bulk.apply", { count })}
          </button>
          <button type="button" onClick={onClose}>
            {t("common.cancel")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
