import { type FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError, api, crudPath } from "../api";
import DeleteDialog from "../components/DeleteDialog";
import RelationSelect from "../components/RelationSelect";
import { useModelMeta } from "../hooks";
import { toLatinDigits, useI18n } from "../i18n";
import type { DeletePlan, FieldMeta, HistoryEntry, ObjectData, Ref } from "../types";

type Values = Record<string, unknown>;
type Errors = Record<string, string[]>;

const NUMERIC = new Set(["integer", "decimal", "float"]);

function pad(n: number) {
  return String(n).padStart(2, "0");
}

/** ISO datetime (any offset) → value for <input type="datetime-local"> in browser time. */
function toLocalInput(iso: unknown): string {
  if (typeof iso !== "string" || !iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Convert form state to the JSON the API expects. */
function toPayload(field: FieldMeta, value: unknown): unknown {
  if (value === "" || value === undefined) {
    if (field.kind === "boolean") return false;
    return field.null || NUMERIC.has(field.kind) || field.kind === "datetime" || field.kind === "date" ? null : "";
  }
  if (NUMERIC.has(field.kind) && typeof value === "string") return toLatinDigits(value).replace(/[,٬]/g, "");
  if (field.kind === "datetime" && typeof value === "string" && !/[zZ]|[+-]\d\d:?\d\d$/.test(value)) {
    return new Date(value).toISOString();
  }
  if (field.kind === "fk" || field.kind === "o2o") return value ? (value as Ref).pk : null;
  if (field.kind === "m2m") return ((value as Ref[]) ?? []).map((r) => r.pk);
  if (field.kind === "json" && typeof value === "string") {
    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  }
  return value;
}

function fromApi(field: FieldMeta, value: unknown): unknown {
  if (field.kind === "json") return value === null ? "" : JSON.stringify(value, null, 2);
  if (field.kind === "datetime") return value === null ? "" : value;
  return value ?? "";
}

export default function ObjectForm() {
  const { app, model, pk } = useParams();
  const { t, fmt, cap } = useI18n();
  const label = `${app}.${model}`;
  const isNew = !pk;
  const navigate = useNavigate();
  const { meta, error: metaError } = useModelMeta(app, model);
  const [obj, setObj] = useState<ObjectData | null>(null);
  const [values, setValues] = useState<Values>({});
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Errors>({});
  const [message, setMessage] = useState("");
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<DeletePlan | null>(null);
  const [history, setHistory] = useState<HistoryEntry[] | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    if (!meta) return;
    setObj(null);
    setErrors({});
    setDirty(new Set());
    setHistory(null);
    setLoadError("");
    api<ObjectData>(crudPath(label, isNew ? "new" : pk))
      .then((o) => {
        setObj(o);
        const v: Values = {};
        for (const f of meta.fields) if (f.name in o.values) v[f.name] = fromApi(f, o.values[f.name]);
        setValues(v);
      })
      .catch((e: Error) => setLoadError(e.message));
  }, [meta, label, pk, isNew, reload]);

  if (metaError || loadError) return <p className="error">{metaError || loadError}</p>;
  if (!meta || !obj) return <p className="muted">{t("app.loading")}</p>;

  const fieldsByName = Object.fromEntries(meta.fields.map((f) => [f.name, f]));
  const readonly = new Set(meta.form.readonly_fields);
  const canEdit = isNew ? meta.permissions.add : meta.permissions.change;

  const set = (name: string, value: unknown) => {
    setValues((v) => ({ ...v, [name]: value }));
    setDirty((d) => new Set(d).add(name));
  };

  async function save(e: FormEvent, then: "list" | "continue" | "another") {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    setMessage("");
    const names = meta!.form.fields.filter((n) => !readonly.has(n) && fieldsByName[n] && (isNew || dirty.has(n)));
    const body: Values = {};
    for (const n of names) {
      if (meta!.form.password_fields.includes(n) && !values[n]) continue;
      body[n] = toPayload(fieldsByName[n], values[n]);
    }
    try {
      const saved = isNew
        ? await api<ObjectData>(crudPath(label), { method: "POST", body })
        : await api<ObjectData>(crudPath(label, String(pk)), { method: "PATCH", body });
      const msg = t("form.saved", { label: saved.label });
      if (then === "list") navigate(`/${app}/${model}`, { state: { notice: msg } });
      else if (then === "another") {
        navigate(`/${app}/${model}/add`);
        setReload((r) => r + 1);
        setMessage(msg);
      } else {
        if (isNew) navigate(`/${app}/${model}/${saved.pk}`, { replace: true });
        setObj(saved);
        setDirty(new Set());
        setMessage(msg);
        setHistory(null);
      }
    } catch (err) {
      if (err instanceof ApiError && err.data.errors) setErrors(err.data.errors);
      else setErrors({ __all__: [err instanceof Error ? err.message : String(err)] });
    } finally {
      setBusy(false);
    }
  }

  async function askDelete() {
    setPlan(await api<DeletePlan>(crudPath(label, `${pk}/delete-plan`)));
  }

  async function doDelete() {
    setBusy(true);
    try {
      await api(crudPath(label, String(pk)), { method: "DELETE" });
      navigate(`/${app}/${model}`);
    } catch (err) {
      setPlan(null);
      setErrors({ __all__: [err instanceof Error ? err.message : String(err)] });
    } finally {
      setBusy(false);
    }
  }

  async function toggleHistory() {
    if (history) return setHistory(null);
    const r = await api<{ entries: HistoryEntry[] }>(crudPath(label, `${pk}/history`));
    setHistory(r.entries);
  }

  const general = errors.__all__?.filter(Boolean) ?? [];

  return (
    <div className="form-page">
      <nav className="breadcrumbs">
        <Link to="/">{t("nav.dashboard")}</Link> › <Link to={`/${app}/${model}`}>{cap(meta.verbose_name_plural)}</Link> ›{" "}
        <bdi>{isNew ? t("form.add", { model: meta.verbose_name }) : obj.label}</bdi>
      </nav>
      <div className="page-head">
        <h1>
          <bdi>{isNew ? t("form.add", { model: meta.verbose_name }) : obj.label}</bdi>
        </h1>
        {!isNew && (
          <button type="button" className="link" onClick={toggleHistory}>
            {history ? t("form.closeHistory") : t("form.history")}
          </button>
        )}
      </div>
      {message && <p className="notice">{message}</p>}
      {general.length > 0 && (
        <div className="error">
          {general.map((g) => (
            <p key={g}>{g}</p>
          ))}
        </div>
      )}
      {Object.keys(errors).length > 0 && !general.length && <p className="error">{t("form.fixErrors")}</p>}

      {history && (
        <section className="card history">
          {history.length === 0 ? (
            <p className="muted">{t("form.noHistory")}</p>
          ) : (
            <table className="grid">
              <tbody>
                {history.map((h, i) => (
                  <tr key={i}>
                    <td>{fmt.date(h.timestamp)}</td>
                    <td>
                      <bdi>{h.user ?? "—"}</bdi>
                    </td>
                    <td>{h.action_display}</td>
                    <td>
                      {fmt.list(h.changes.fields?.map((n) => cap(fieldsByName[n]?.verbose_name ?? n)) ?? [])}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      <form className="card object-form" onSubmit={(e) => save(e, "list")}>
        {meta.form.fields.map((name) => {
          const field = fieldsByName[name];
          if (!field) return null;
          const ro = !canEdit || readonly.has(name);
          return (
            <div key={name} className={`form-row${errors[name] ? " has-error" : ""}`}>
              <label htmlFor={`f-${name}`} className={field.required && !ro ? "required" : ""}>
                {cap(field.verbose_name)}
              </label>
              <div className="form-input">
                {ro ? (
                  <div className="readonly">
                    <bdi>{fmt.cell(obj.values[name], field.kind)}</bdi>
                  </div>
                ) : (
                  <FieldInput field={field} model={label} value={values[name]} onChange={(v) => set(name, v)} isNew={isNew} />
                )}
                {field.help_text && <small className="help">{field.help_text}</small>}
                {errors[name]?.map((m) => (
                  <small key={m} className="field-error">
                    {m}
                  </small>
                ))}
              </div>
            </div>
          );
        })}

        <div className="form-actions">
          {canEdit && (
            <>
              <button type="submit" className="primary" disabled={busy}>
                {t("form.save")}
              </button>
              <button type="button" disabled={busy} onClick={(e) => save(e, "continue")}>
                {t("form.saveContinue")}
              </button>
              {meta.permissions.add && (
                <button type="button" disabled={busy} onClick={(e) => save(e, "another")}>
                  {t("form.saveAnother")}
                </button>
              )}
            </>
          )}
          {!isNew && meta.permissions.delete && (
            <button type="button" className="danger push" disabled={busy} onClick={askDelete}>
              {t("common.delete")}
            </button>
          )}
        </div>
      </form>

      {plan && (
        <DeleteDialog
          plan={plan}
          title={t("form.deleteTitle", { label: obj.label })}
          busy={busy}
          onCancel={() => setPlan(null)}
          onConfirm={doDelete}
        />
      )}
    </div>
  );
}

type InputProps = {
  field: FieldMeta;
  model: string;
  value: unknown;
  onChange: (v: unknown) => void;
  isNew: boolean;
};

function FieldInput({ field, model, value, onChange, isNew }: InputProps) {
  const { t } = useI18n();
  const id = `f-${field.name}`;
  const str = value === null || value === undefined ? "" : String(value);

  if (field.choices) {
    return (
      <select id={id} value={str} onChange={(e) => onChange(e.target.value)}>
        {(field.blank || field.null || !str) && <option value="">---------</option>}
        {field.choices.map((c) => (
          <option key={String(c.value)} value={String(c.value)}>
            {c.label}
          </option>
        ))}
      </select>
    );
  }

  switch (field.kind) {
    case "boolean":
      if (field.null) {
        return (
          <select id={id} value={str} onChange={(e) => onChange(e.target.value === "" ? null : e.target.value === "true")}>
            <option value="">{t("common.unknown")}</option>
            <option value="true">{t("common.yes")}</option>
            <option value="false">{t("common.no")}</option>
          </select>
        );
      }
      return <input id={id} type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} />;
    case "text":
      return <textarea id={id} dir="auto" rows={4} value={str} onChange={(e) => onChange(e.target.value)} />;
    case "json":
      return <textarea id={id} dir="ltr" rows={6} className="mono" value={str} onChange={(e) => onChange(e.target.value)} />;
    case "integer":
    case "decimal":
    case "float":
      return <input id={id} dir="ltr" inputMode="decimal" value={str} onChange={(e) => onChange(e.target.value)} />;
    case "date":
      return <input id={id} type="date" value={str.slice(0, 10)} onChange={(e) => onChange(e.target.value)} />;
    case "datetime":
      return (
        <input
          id={id}
          type="datetime-local"
          value={str.includes("T") && /[zZ+]|-\d\d:\d\d$/.test(str) ? toLocalInput(str) : str}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case "time":
      return <input id={id} type="time" value={str.slice(0, 5)} onChange={(e) => onChange(e.target.value)} />;
    case "email":
      return <input id={id} type="email" dir="ltr" value={str} onChange={(e) => onChange(e.target.value)} />;
    case "url":
      return <input id={id} type="url" dir="ltr" value={str} onChange={(e) => onChange(e.target.value)} />;
    case "password":
      return (
        <input
          id={id}
          type="password"
          dir="ltr"
          autoComplete="new-password"
          placeholder={isNew ? "" : t("form.passwordPlaceholder")}
          value={str}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case "fk":
    case "o2o":
      return (
        <RelationSelect
          model={model}
          field={field.name}
          value={value && typeof value === "object" ? (value as Ref) : null}
          onChange={onChange}
        />
      );
    case "m2m": {
      const refs = Array.isArray(value) ? (value as Ref[]) : [];
      return (
        <div className="m2m">
          <ul className="chips">
            {refs.map((r) => (
              <li key={String(r.pk)}>
                <bdi>{r.label}</bdi>
                <button type="button" aria-label={t("common.remove")} onClick={() => onChange(refs.filter((x) => x.pk !== r.pk))}>
                  ×
                </button>
              </li>
            ))}
          </ul>
          <RelationSelect
            model={model}
            field={field.name}
            value={null}
            placeholder={t("form.addItem")}
            onChange={(r) => r && !refs.some((x) => x.pk === r.pk) && onChange([...refs, r])}
          />
        </div>
      );
    }
    default:
      return (
        <input
          id={id}
          dir="auto"
          maxLength={field.max_length ?? undefined}
          value={str}
          onChange={(e) => onChange(e.target.value)}
        />
      );
  }
}
