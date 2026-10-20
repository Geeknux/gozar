import { type FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError, api, crudPath, errorText } from "../api";
import FieldInput from "../components/FieldInput";
import type { Errors, Values } from "../forms";
import { useModelMeta, useShortcuts } from "../hooks";
import { useI18n } from "../i18n";
import type { FieldMeta, ObjectData } from "../types";
import { PasswordHelp } from "./ObjectForm";

/** admin's "Change password" page: two password fields (or disabling password login). */
export default function PasswordChange() {
  const { app, model, pk } = useParams();
  const { t, cap } = useI18n();
  const label = `${app}.${model}`;
  const navigate = useNavigate();
  const { meta, error: metaError } = useModelMeta(app, model);
  const [form, setForm] = useState<{ fields: FieldMeta[]; help: string[] } | null>(null);
  const [obj, setObj] = useState<ObjectData | null>(null);
  const [values, setValues] = useState<Values>({ usable_password: "true" });
  const [errors, setErrors] = useState<Errors>({});
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Promise.all([
      api<{ fields: FieldMeta[]; help: string[] }>(crudPath(label, "password-form")),
      api<ObjectData>(crudPath(label, String(pk))),
    ])
      .then(([f, o]) => {
        setForm(f);
        setObj(o);
      })
      .catch((e) => setLoadError(errorText(e)));
  }, [label, pk]);

  async function submit(e?: FormEvent) {
    e?.preventDefault();
    setBusy(true);
    setErrors({});
    try {
      const body = values.usable_password === "false" ? { usable_password: "false" } : values;
      const saved = await api<ObjectData>(crudPath(label, `${pk}/password`), { method: "POST", body });
      navigate(`/${app}/${model}/${pk}`, { state: { notice: t("password.changed", { label: saved.label }) } });
    } catch (err) {
      if (err instanceof ApiError && err.data.errors) setErrors(err.data.errors);
      else setErrors({ __all__: [errorText(err)] });
    } finally {
      setBusy(false);
    }
  }

  useShortcuts({ "mod+s": () => submit() });

  if (metaError || loadError) return <p className="error">{metaError || loadError}</p>;
  if (!meta || !form || !obj) return <p className="muted">{t("app.loading")}</p>;
  const disabled = values.usable_password === "false";

  return (
    <div className="form-page">
      <nav className="breadcrumbs">
        <Link to="/">{t("nav.dashboard")}</Link> › <Link to={`/${app}/${model}`}>{cap(meta.verbose_name_plural)}</Link> ›{" "}
        <Link to={`/${app}/${model}/${pk}`}>
          <bdi>{obj.label}</bdi>
        </Link>{" "}
        › {t("password.change")}
      </nav>
      <div className="page-head">
        <h1>
          {t("password.title")} <bdi>{obj.label}</bdi>
        </h1>
      </div>
      {errors.__all__?.map((m) => (
        <p key={m} className="error">
          {m}
        </p>
      ))}
      <form className="object-form" onSubmit={submit}>
        <section className="card fieldset">
          <p className="help fieldset-description">{t("password.intro", { label: obj.label })}</p>
          {form.fields.map((f) => {
            if (disabled && f.kind === "password") return null;
            return (
              <div key={f.name} className={`form-row${errors[f.name] ? " has-error" : ""}`}>
                <label htmlFor={`p-${f.name}`} className={f.required ? "required" : ""}>
                  {cap(f.verbose_name)}
                </label>
                <div className="form-input">
                  <FieldInput
                    id={`p-${f.name}`}
                    field={f}
                    widget={f.choices ? { type: "radio" } : undefined}
                    model={label}
                    value={values[f.name] ?? ""}
                    onChange={(v) => setValues((cur) => ({ ...cur, [f.name]: v }))}
                    isNew
                  />
                  {f.help_text && <small className="help">{f.help_text}</small>}
                  {f.name === "password1" && <PasswordHelp texts={form.help} />}
                  {errors[f.name]?.map((m) => (
                    <small key={m} className="field-error">
                      {m}
                    </small>
                  ))}
                </div>
              </div>
            );
          })}
        </section>
        <div className="card form-footer">
          <div className="form-actions">
            <button type="submit" className="primary" disabled={busy}>
              {t("password.change")}
            </button>
            <Link to={`/${app}/${model}/${pk}`} className="button">
              {t("common.cancel")}
            </Link>
          </div>
        </div>
      </form>
    </div>
  );
}
