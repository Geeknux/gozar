import { type FormEvent, Fragment, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { ApiError, api, crudPath, errorText, type InlineErrors } from "../api";
import DeleteDialog from "../components/DeleteDialog";
import FieldInput from "../components/FieldInput";
import InlineEditor, { inlinePayload, inlineState, type RowState } from "../components/InlineEditor";
import ReadonlyValue from "../components/ReadonlyValue";
import { type Errors, filePayload, fromApi, requestBody, slugify, toPayload, type Values } from "../forms";
import { useModelMeta, useShortcuts } from "../hooks";
import { useI18n } from "../i18n";
import type { DeletePlan, DiffEntry, FieldMeta, Fieldset, HistoryEntry, ObjectData } from "../types";

type Then = "list" | "continue" | "another" | "saveas";

const SOURCE_LABELS = {
  list: "history.source.list",
  bulk: "history.source.bulk",
  import: "history.source.import",
} as const;

export default function ObjectForm() {
  const { app, model, pk } = useParams();
  const { t, cap } = useI18n();
  const label = `${app}.${model}`;
  const isNew = !pk;
  const navigate = useNavigate();
  const { meta, error: metaError } = useModelMeta(app, model);
  const [obj, setObj] = useState<ObjectData | null>(null);
  const [values, setValues] = useState<Values>({});
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Errors>({});
  const [inlineRows, setInlineRows] = useState<Record<string, RowState[]>>({});
  const [inlineErrors, setInlineErrors] = useState<Record<string, InlineErrors>>({});
  const [errorKeys, setErrorKeys] = useState<Record<string, string[]>>({});
  const [message, setMessage] = useState("");
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<DeletePlan | null>(null);
  const [history, setHistory] = useState<HistoryEntry[] | null>(null);
  const [reload, setReload] = useState(0);
  const location = useLocation();
  const touchedSlugs = useRef<Set<string>>(new Set());

  useEffect(() => setMessage((location.state as { notice?: string } | null)?.notice ?? ""), [location.state]);
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    if (!meta) return;
    setObj(null);
    setErrors({});
    setInlineErrors({});
    setDirty(new Set());
    setHistory(null);
    setLoadError("");
    touchedSlugs.current = new Set();
    let stale = false; // a newer load (another pk, a reload) supersedes this one
    api<ObjectData>(crudPath(label, isNew ? "new" : pk))
      .then((o) => {
        if (stale) return;
        setObj(o);
        const v: Values = {};
        for (const f of meta.fields) if (f.name in o.values) v[f.name] = fromApi(f, o.values[f.name]);
        for (const f of meta.fields) if (f.form_only && !(f.name in v)) v[f.name] = f.initial ?? "";
        setValues(v);
        setInlineRows(
          Object.fromEntries(meta.form.inlines.map((m) => [m.name, inlineState(m, o.inlines?.[m.name]?.rows, true)])),
        );
      })
      .catch((e: Error) => !stale && setLoadError(e.message));
    return () => {
      stale = true;
    };
  }, [meta, label, pk, isNew, reload]);

  useShortcuts({ "mod+s": () => formRef.current?.requestSubmit(document.getElementById("save-continue") as HTMLButtonElement) });

  if (metaError || loadError) return <p className="error">{metaError || loadError}</p>;
  if (!meta || !obj) return <p className="muted">{t("app.loading")}</p>;

  const fieldsByName: Record<string, FieldMeta> = Object.fromEntries(meta.fields.map((f) => [f.name, f]));
  const readonly = new Set(meta.form.readonly_fields);
  const canEdit = isNew ? meta.permissions.add : meta.permissions.change;
  const addForm = isNew && meta.form.add.custom_form;
  const fieldsets: Fieldset[] = isNew ? meta.form.add.fieldsets : meta.form.fieldsets;
  const formFields = isNew ? meta.form.add.fields : meta.form.fields;
  const showInlines = !addForm && meta.form.inlines.length > 0;

  const set = (name: string, value: unknown) => {
    setValues((v) => {
      const next = { ...v, [name]: value };
      if (isNew) {
        for (const [target, sources] of Object.entries(meta!.form.prepopulated)) {
          if (sources.includes(name) && !touchedSlugs.current.has(target)) {
            next[target] = slugify(sources.map((s) => String(next[s] ?? "")).join(" "), !!fieldsByName[target]?.allow_unicode);
            setDirty((d) => new Set(d).add(target));
          }
        }
      }
      return next;
    });
    if (name in meta.form.prepopulated) touchedSlugs.current.add(name);
    setDirty((d) => new Set(d).add(name));
  };

  async function save(e: FormEvent, then: Then) {
    e.preventDefault();
    const saveAs = then === "saveas";
    setBusy(true);
    setErrors({});
    setInlineErrors({});
    setMessage("");
    const files = new Map<string, File>();
    const body: Values = {};
    const names = formFields.filter(
      (n) => !readonly.has(n) && fieldsByName[n] && (isNew || saveAs || dirty.has(n)),
    );
    for (const n of names) {
      const field = fieldsByName[n];
      if (meta!.form.password_fields.includes(n) && !values[n]) continue;
      if (field.kind === "file") filePayload(values[n], n, body, n, files, saveAs);
      else body[n] = toPayload(field, values[n]);
    }
    const keys: Record<string, string[]> = {};
    if (showInlines) {
      const payload: Record<string, Values[]> = {};
      for (const im of meta!.form.inlines) {
        const built = inlinePayload(im, inlineRows[im.name] ?? [], files, saveAs);
        if (built.rows.length) payload[im.name] = built.rows;
        keys[im.name] = built.keys;
      }
      if (Object.keys(payload).length) body.__inlines__ = payload;
    }
    if (saveAs) body.__save_as__ = pk;
    setErrorKeys(keys);
    try {
      const create = isNew || saveAs;
      const saved = create
        ? await api<ObjectData>(crudPath(label), { method: "POST", body: requestBody(body, files) })
        : await api<ObjectData>(crudPath(label, String(pk)), { method: "PATCH", body: requestBody(body, files) });
      const msg = addForm
        ? t("form.addedContinue", { label: saved.label })
        : t(saveAs ? "form.savedAsNew" : "form.saved", { label: saved.label });
      if (addForm || (saveAs && meta!.form.save_as_continue) || then === "continue") {
        if (create) navigate(`/${app}/${model}/${saved.pk}`, { replace: !saveAs, state: { notice: msg } });
        else {
          setReload((r) => r + 1);
          setMessage(msg);
        }
      } else if (then === "another") {
        navigate(`/${app}/${model}/add`);
        setReload((r) => r + 1);
        setMessage(msg);
      } else {
        navigate(`/${app}/${model}`, { state: { notice: msg } });
      }
    } catch (err) {
      if (err instanceof ApiError && (err.data.errors || err.data.inline_errors)) {
        setErrors(err.data.errors ?? {});
        setInlineErrors(err.data.inline_errors ?? {});
        if (!err.data.errors || !Object.keys(err.data.errors).length) {
          if (!err.data.inline_errors) setErrors({ __all__: [err.message] });
        }
      } else setErrors({ __all__: [errorText(err)] });
    } finally {
      setBusy(false);
    }
  }

  async function askDelete() {
    try {
      setPlan(await api<DeletePlan>(crudPath(label, `${pk}/delete-plan`)));
    } catch (err) {
      setErrors({ __all__: [errorText(err)] });
    }
  }

  async function doDelete() {
    setBusy(true);
    try {
      await api(crudPath(label, String(pk)), { method: "DELETE" });
      navigate(`/${app}/${model}`, { state: { notice: t("form.deleted", { label: obj!.label }) } });
    } catch (err) {
      setPlan(null);
      setErrors({ __all__: [errorText(err)] });
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
  const hasErrors = Object.keys(errors).length > 0 || Object.keys(inlineErrors).length > 0;
  const title = isNew ? t("form.add", { model: meta.verbose_name }) : obj.label;

  const buttons = (top: boolean) => (
    <div className={`form-actions${top ? " top" : ""}`}>
      {canEdit && (
        <>
          <button type="submit" className="primary" disabled={busy}>
            {t("form.save")}
          </button>
          {!addForm && (
            <button type="submit" id={top ? undefined : "save-continue"} disabled={busy} value="continue">
              {t("form.saveContinue")}
            </button>
          )}
          {meta.permissions.add && !addForm && (
            <button type="submit" disabled={busy} value="another">
              {t("form.saveAnother")}
            </button>
          )}
          {!isNew && meta.form.save_as && meta.permissions.add && (
            <button type="submit" disabled={busy} value="saveas">
              {t("form.saveAsNew")}
            </button>
          )}
        </>
      )}
      {!isNew && meta.permissions.delete && !top && (
        <button type="button" className="danger push" disabled={busy} onClick={askDelete}>
          {t("common.delete")}
        </button>
      )}
    </div>
  );

  const row = (names: string[]) => {
    const wide = names.length > 1;
    return (
      <div key={names.join("+")} className={`form-row${wide ? " multi" : ""}${names.some((n) => errors[n]) ? " has-error" : ""}`}>
        {names.map((name) => (
          <Fragment key={name}>{fieldRow(name, !wide)}</Fragment>
        ))}
      </div>
    );
  };

  // a render helper, not a component: a component defined here would remount (and
  // lose focus) on every keystroke
  function fieldRow(name: string, single: boolean) {
    const field = fieldsByName[name];
    if (!field) return null;
    const ro = !canEdit || readonly.has(name);
    const labelEl = (
      <label htmlFor={`f-${name}`} className={field.required && !ro ? "required" : ""}>
        <bdi>{cap(field.verbose_name)}</bdi>
      </label>
    );
    let input;
    if (!isNew && meta!.form.change_password && name === "password" && obj!.password_status) {
      const st = obj!.password_status;
      input = (
        <div className="readonly password-status">
          {st.usable ? t("password.status", { algorithm: st.algorithm ?? "?" }) : t("password.none")}{" "}
          {meta!.permissions.change && (
            <Link to={`/${app}/${model}/${pk}/password`} className="button small">
              {st.usable ? t("password.change") : t("password.set")}
            </Link>
          )}
        </div>
      );
    } else if (ro) {
      input = <ReadonlyValue field={field} value={obj!.values[name] ?? values[name]} />;
    } else {
      input = (
        <FieldInput
          id={`f-${name}`}
          field={field}
          widget={meta!.form.widgets[name]}
          model={label}
          value={values[name]}
          initial={obj!.values[name]}
          onChange={(v) => set(name, v)}
          isNew={isNew}
        />
      );
    }
    const body = (
      <div className="form-input">
        {input}
        {obj!.hidden_links?.[name] ? <small className="help">{t("form.hiddenLinks", { count: obj!.hidden_links[name] })}</small> : null}
        {field.help_text && <small className="help">{field.help_text}</small>}
        {name === "password1" && meta!.form.password_help.length > 0 && <PasswordHelp texts={meta!.form.password_help} />}
        {errors[name]?.map((m) => (
          <small key={m} className="field-error">
            {m}
          </small>
        ))}
      </div>
    );
    return single ? (
      <>
        {labelEl}
        {body}
      </>
    ) : (
      <div className="form-cell">
        {labelEl}
        {body}
      </div>
    );
  }

  return (
    <div className="form-page">
      <nav className="breadcrumbs">
        <Link to="/">{t("nav.dashboard")}</Link> › <Link to={`/${app}/${model}`}>{cap(meta.verbose_name_plural)}</Link> › <bdi>{title}</bdi>
      </nav>
      <div className="page-head">
        <h1>
          <bdi>{title}</bdi>
        </h1>
        <span className="object-tools">
          {obj.site_url && (
            <a href={obj.site_url} target="_blank" rel="noreferrer" className="button small">
              {t("form.viewOnSite")}
            </a>
          )}
          {!isNew && (
            <button type="button" className="button small" onClick={toggleHistory}>
              {history ? t("form.closeHistory") : t("form.history")}
            </button>
          )}
        </span>
      </div>
      {message && <p className="notice">{message}</p>}
      {general.length > 0 && (
        <div className="error">
          {general.map((g) => (
            <p key={g}>{g}</p>
          ))}
        </div>
      )}
      {hasErrors && !general.length && <p className="error">{t("form.fixErrors")}</p>}

      {history && <History entries={history} fields={fieldsByName} />}

      <form
        ref={formRef}
        className="object-form"
        onSubmit={(e) => {
          const submitter = (e.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
          save(e, ((submitter?.value as Then) || "list") as Then);
        }}
      >
        {meta.form.save_on_top && !isNew && buttons(true)}
        {fieldsets.map((fs, i) => (
          <FieldsetBox key={i} fieldset={fs} hasErrors={fs.rows.some((r) => r.some((n) => errors[n]))}>
            {fs.rows.map((names) => row(names))}
          </FieldsetBox>
        ))}
        {showInlines &&
          meta.form.inlines.map((im) => (
            <InlineEditor
              key={im.name}
              meta={im}
              model={label}
              rows={inlineRows[im.name] ?? []}
              hidden={obj.inlines?.[im.name]?.hidden ?? 0}
              readOnly={!canEdit}
              errors={inlineErrors[im.name]}
              errorKeys={errorKeys[im.name] ?? []}
              onChange={(rows) => setInlineRows((all) => ({ ...all, [im.name]: rows }))}
            />
          ))}
        <div className={`card form-footer${canEdit || (!isNew && meta.permissions.delete) ? "" : " empty"}`}>
          {buttons(false)}
          {canEdit && <small className="muted shortcut-hint">{t("shortcuts.saveHint")}</small>}
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

function FieldsetBox({ fieldset, hasErrors, children }: { fieldset: Fieldset; hasErrors: boolean; children: React.ReactNode }) {
  const body = (
    <>
      {fieldset.description && <p className="help fieldset-description"><bdi>{fieldset.description}</bdi></p>}
      {children}
    </>
  );
  if (fieldset.collapse) {
    return (
      <details className="card fieldset collapse" open={hasErrors}>
        <summary>
          <h2><bdi>{fieldset.title}</bdi></h2>
        </summary>
        {body}
      </details>
    );
  }
  return (
    <section className="card fieldset">
      {fieldset.title && <h2><bdi>{fieldset.title}</bdi></h2>}
      {body}
    </section>
  );
}

export function PasswordHelp({ texts }: { texts: string[] }) {
  return (
    <ul className="help password-help">
      {texts.map((text) => (
        <li key={text}>{text}</li>
      ))}
    </ul>
  );
}

function History({ entries, fields }: { entries: HistoryEntry[]; fields: Record<string, FieldMeta> }) {
  const { t, fmt, cap } = useI18n();
  if (!entries.length) return <section className="card history"><p className="muted">{t("form.noHistory")}</p></section>;
  return (
    <section className="card history">
      <h2>{t("form.history")}</h2>
      <ol className="history-list">
        {entries.map((h, i) => (
          <li key={i}>
            <div className="history-meta">
              <strong>{h.action_display}</strong> · <bdi>{h.user ?? "—"}</bdi> · <span className="muted">{fmt.date(h.timestamp)}</span>
              {h.source && <span className="badge">{t(SOURCE_LABELS[h.source])}</span>}
            </div>
            {h.diff.length > 0 ? (
              <DiffTable diff={h.diff} />
            ) : (
              h.changes.fields?.length ? (
                <p className="muted"><bdi>{fmt.list(h.changes.fields.map((n) => cap(fields[n]?.verbose_name ?? n)))}</bdi></p>
              ) : null
            )}
            {h.inlines?.map((inline) => (
              <div key={inline.name} className="history-inline">
                <h4><bdi>{cap(inline.verbose_name)}</bdi></h4>
                <ul>
                  {inline.added.map((a) => (
                    <li key={`a-${a}`}>
                      {t("history.added")} <bdi>{a}</bdi>
                    </li>
                  ))}
                  {inline.changed.map((c) => (
                    <li key={`c-${c.label}`}>
                      {t("history.changed")} <bdi>{c.label}</bdi>
                      {c.diff.length > 0 && <DiffTable diff={c.diff} />}
                    </li>
                  ))}
                  {inline.deleted.map((d) => (
                    <li key={`d-${d}`}>
                      {t("history.deleted")} <bdi>{d}</bdi>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </li>
        ))}
      </ol>
    </section>
  );
}

function DiffTable({ diff }: { diff: DiffEntry[] }) {
  const { t } = useI18n();
  return (
    <table className="grid diff">
      <thead>
        <tr>
          <th>{t("history.field")}</th>
          <th>{t("history.old")}</th>
          <th>{t("history.new")}</th>
        </tr>
      </thead>
      <tbody>
        {diff.map((d) => (
          <tr key={d.field}>
            <th scope="row"><bdi>{d.label}</bdi></th>
            <td className="old">
              <ReadonlyValue field={{ kind: d.kind } as FieldMeta} value={d.old} />
            </td>
            <td className="new">
              <ReadonlyValue field={{ kind: d.kind } as FieldMeta} value={d.new} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
