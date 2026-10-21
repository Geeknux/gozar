import { type FormEvent, useEffect, useRef, useState } from "react";

import { ApiError, api, errorText } from "../../api";
import Dialog from "../../components/Dialog";
import { useI18n } from "../../i18n";
import type { ModelMeta, SavedView } from "../../types";

type Props = {
  meta: ModelMeta;
  params: URLSearchParams;
  onApply: (params: Record<string, string>) => void;
};

const SKIP = new Set(["page", "view"]);

/** Saved list views: the current search, filters, ordering and columns under a
 * name, per user; one may be the default that opens with the list. */
export default function ViewsMenu({ meta, params, onApply }: Props) {
  const { t } = useI18n();
  const [views, setViews] = useState<SavedView[]>([]);
  const [editing, setEditing] = useState<SavedView | "new" | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const appliedDefault = useRef(false);
  const box = useRef<HTMLDivElement>(null);

  const load = () =>
    api<{ views: SavedView[] }>("/list-views", { params: { model: meta.label } })
      .then((r) => {
        setViews(r.views);
        return r.views;
      })
      .catch((e) => {
        setError(errorText(e));
        return [] as SavedView[];
      });

  useEffect(() => {
    appliedDefault.current = false;
    load().then((vs) => {
      // the default view opens with a bare list (not when the URL already has a query)
      const bare = [...params.keys()].every((k) => SKIP.has(k));
      const def = vs.find((v) => v.is_default);
      if (def && bare && !appliedDefault.current) {
        appliedDefault.current = true;
        onApply(asParams(def));
      }
    });
  }, [meta.label]);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const current = Object.fromEntries([...params.entries()].filter(([k]) => !SKIP.has(k) && k !== "columns"));
  const currentKey = JSON.stringify(sorted({ ...current, columns: params.get("columns") ?? "" }));
  const active = views.find((v) => JSON.stringify(sorted({ ...v.params, columns: v.columns.join(",") })) === currentKey);

  return (
    <div className="views-menu" ref={box}>
      <button type="button" className="button" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <bdi>{active ? active.name : t("views.title")}</bdi> ▾
      </button>
      {open && (
        <div className="menu-popup">
          {error && <p className="error">{error}</p>}
          <ul>
            {views.map((v) => (
              <li key={v.id} className={active?.id === v.id ? "active" : ""}>
                <button
                  type="button"
                  className="menu-item"
                  onClick={() => {
                    onApply(asParams(v));
                    setOpen(false);
                  }}
                >
                  <bdi>{v.name}</bdi>
                  {v.is_default && <span className="badge">{t("views.default")}</span>}
                </button>
                <button type="button" className="link small" onClick={() => (setEditing(v), setOpen(false))}>
                  {t("views.edit")}
                </button>
              </li>
            ))}
            {!views.length && <li className="muted small">{t("views.none")}</li>}
          </ul>
          <div className="menu-footer">
            <button type="button" className="link" onClick={() => (setEditing("new"), setOpen(false))}>
              {t("views.saveCurrent")}
            </button>
            {[...params.keys()].some((k) => !SKIP.has(k)) && (
              <button type="button" className="link" onClick={() => (onApply({}), setOpen(false))}>
                {t("views.reset")}
              </button>
            )}
          </div>
        </div>
      )}
      {editing && (
        <ViewDialog
          meta={meta}
          view={editing === "new" ? null : editing}
          current={current}
          currentColumns={(params.get("columns") ?? "").split(",").filter(Boolean)}
          onClose={() => setEditing(null)}
          onSaved={(v) => {
            setEditing(null);
            load();
            if (v) onApply(asParams(v));
          }}
        />
      )}
    </div>
  );
}

function sorted(obj: Record<string, string>) {
  return Object.fromEntries(Object.entries(obj).sort(([a], [b]) => a.localeCompare(b)));
}

export function asParams(v: SavedView): Record<string, string> {
  return { ...v.params, ...(v.columns.length ? { columns: v.columns.join(",") } : {}) };
}

type DialogProps = {
  meta: ModelMeta;
  view: SavedView | null;
  current: Record<string, string>;
  currentColumns: string[];
  onClose: () => void;
  onSaved: (view: SavedView | null) => void;
};

function ViewDialog({ meta, view, current, currentColumns, onClose, onSaved }: DialogProps) {
  const { t } = useI18n();
  const defaults = meta.list.columns.map((c) => c.name);
  const [name, setName] = useState(view?.name ?? "");
  const [isDefault, setIsDefault] = useState(view?.is_default ?? false);
  const [useCurrent, setUseCurrent] = useState(!view);
  const [columns, setColumns] = useState<string[]>(view?.columns.length ? view.columns : currentColumns.length ? currentColumns : defaults);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const labels = Object.fromEntries(meta.list.available_columns.map((c) => [c.name, c.label]));
  const all = [...columns, ...meta.list.available_columns.map((c) => c.name).filter((n) => !columns.includes(n))];

  const move = (name: string, delta: number) => {
    const i = columns.indexOf(name);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= columns.length) return;
    const next = [...columns];
    [next[i], next[j]] = [next[j], next[i]];
    setColumns(next);
  };

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    const sameAsDefault = columns.join(",") === defaults.join(",");
    const body: Record<string, unknown> = { name, is_default: isDefault, columns: sameAsDefault ? [] : columns };
    if (useCurrent) body.params = current;
    try {
      const saved = view
        ? await api<SavedView>(`/list-views/${view.id}`, { method: "PATCH", body })
        : await api<SavedView>("/list-views", { method: "POST", body: { ...body, model: meta.label } });
      onSaved(saved);
    } catch (err) {
      if (err instanceof ApiError && err.data.errors) setErrors(err.data.errors);
      else setErrors({ __all__: [errorText(err)] });
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!view) return;
    setBusy(true);
    try {
      await api(`/list-views/${view.id}`, { method: "DELETE" });
      onSaved(null);
    } catch (err) {
      setErrors({ __all__: [errorText(err)] });
      setBusy(false);
    }
  }

  return (
    <Dialog title={view ? t("views.editTitle") : t("views.saveTitle")} onClose={onClose}>
      <form className="dialog-form" onSubmit={submit}>
        {errors.__all__?.map((m) => (
          <p key={m} className="error">
            {m}
          </p>
        ))}
        <label className="stack">
          {t("views.name")}
          <input dir="auto" autoFocus required value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />
          {errors.name?.map((m) => (
            <small key={m} className="field-error">
              {m}
            </small>
          ))}
        </label>
        {view && (
          <label className="check-row">
            <input type="checkbox" checked={useCurrent} onChange={(e) => setUseCurrent(e.target.checked)} />
            {t("views.replaceFilters")}
          </label>
        )}
        <label className="check-row">
          <input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} />
          {t("views.makeDefault")}
        </label>
        <fieldset className="share-fields">
          <legend>{t("views.columns")}</legend>
          <ul className="column-chooser">
            {all.map((n) => {
              const on = columns.includes(n);
              return (
                <li key={n}>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() => setColumns(on ? columns.filter((c) => c !== n) : [...columns, n])}
                    />
                    <bdi>{labels[n] ?? n}</bdi>
                  </label>
                  {on && (
                    <span className="move">
                      <button type="button" className="link" aria-label={t("views.up")} onClick={() => move(n, -1)}>
                        ↑
                      </button>
                      <button type="button" className="link" aria-label={t("views.down")} onClick={() => move(n, 1)}>
                        ↓
                      </button>
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
          {errors.columns?.map((m) => (
            <small key={m} className="field-error">
              {m}
            </small>
          ))}
        </fieldset>
        <div className="dialog-actions">
          <button type="submit" className="primary" disabled={busy || !name.trim() || !columns.length}>
            {t("form.save")}
          </button>
          <button type="button" onClick={onClose}>
            {t("common.cancel")}
          </button>
          {view && (
            <button type="button" className="button danger push" disabled={busy} onClick={remove}>
              {t("common.delete")}
            </button>
          )}
        </div>
      </form>
    </Dialog>
  );
}
