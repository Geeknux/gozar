import { useEffect, useMemo, useState } from "react";

import { api, crudPath, errorText } from "../api";
import { useI18n } from "../i18n";
import type { Ref } from "../types";

type Perm = { pk: number; codename: string; label: string; action: string | null };
type ModelNode = { model: string; verbose_name: string; permissions: Perm[] };
type AppNode = { app_label: string; verbose_name: string; models: ModelNode[] };

type Props = {
  model: string; // the model owning the field (auth.user, auth.group)
  field: string;
  value: Ref[];
  onChange: (value: Ref[]) => void;
};

/**
 * Permissions as a readable tree: app → model → View / Add / Change / Delete (and
 * custom permissions), with "all" toggles per model and app and a filter box.
 */
export default function PermissionTree({ model, field, value, onChange }: Props) {
  const { t, fmt } = useI18n();
  const [apps, setApps] = useState<AppNode[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [onlyChosen, setOnlyChosen] = useState(false);

  useEffect(() => {
    api<{ apps: AppNode[] }>(crudPath(model, "permission-tree"), { params: { field } })
      .then((r) => setApps(r.apps))
      .catch((e) => setError(errorText(e)));
  }, [model, field]);

  const chosen = useMemo(() => new Set(value.map((v) => Number(v.pk))), [value]);
  const byPk = useMemo(() => {
    const out = new Map<number, Perm>();
    for (const a of apps ?? []) for (const m of a.models) for (const p of m.permissions) out.set(p.pk, p);
    return out;
  }, [apps]);

  if (error) return <p className="error">{error}</p>;
  if (!apps) return <p className="muted">{t("app.loading")}</p>;

  const setMany = (perms: Perm[], on: boolean) => {
    const next = new Set(chosen);
    for (const p of perms) {
      if (on) next.add(p.pk);
      else next.delete(p.pk);
    }
    onChange([...next].map((pk) => ({ pk, label: byPk.get(pk)?.codename ?? String(pk), model: "auth.permission" })));
  };

  const q = query.trim().toLocaleLowerCase();
  const matches = (text: string) => text.toLocaleLowerCase().includes(q);
  const visibleApps = apps
    .map((a) => ({
      ...a,
      models: a.models
        .map((m) => ({
          ...m,
          permissions: m.permissions.filter(
            (p) =>
              (!onlyChosen || chosen.has(p.pk)) &&
              (!q || matches(a.verbose_name) || matches(m.verbose_name) || matches(p.label) || matches(p.codename)),
          ),
        }))
        .filter((m) => m.permissions.length),
    }))
    .filter((a) => a.models.length);

  const state = (perms: Perm[]) => {
    const n = perms.filter((p) => chosen.has(p.pk)).length;
    return n === 0 ? "none" : n === perms.length ? "all" : "some";
  };

  return (
    <div className="perm-tree">
      <div className="inline-row perm-tools">
        <input dir="auto" type="search" placeholder={t("perm.filter")} value={query} onChange={(e) => setQuery(e.target.value)} />
        <label className="check-row">
          <input type="checkbox" checked={onlyChosen} onChange={(e) => setOnlyChosen(e.target.checked)} />
          {t("perm.onlyChosen")}
        </label>
        <span className="muted">{t("perm.count", { count: chosen.size })}</span>
      </div>
      <ul className="perm-apps">
        {visibleApps.map((a) => {
          const all = a.models.flatMap((m) => m.permissions);
          const st = state(all);
          const expanded = open.has(a.app_label) || !!q || onlyChosen;
          return (
            <li key={a.app_label}>
              <div className="perm-app">
                <TriCheckbox state={st} onChange={(on) => setMany(all, on)} label={a.verbose_name} />
                <button
                  type="button"
                  className="link"
                  aria-expanded={expanded}
                  onClick={() => {
                    const next = new Set(open);
                    if (next.has(a.app_label)) next.delete(a.app_label);
                    else next.add(a.app_label);
                    setOpen(next);
                  }}
                >
                  <strong><bdi>{a.verbose_name}</bdi></strong>{" "}
                  <span className="muted">
                    ({fmt.number(all.filter((p) => chosen.has(p.pk)).length)}/{fmt.number(all.length)})
                  </span>
                </button>
              </div>
              {expanded && (
                <table className="grid perm-models">
                  <tbody>
                    {a.models.map((m) => (
                      <tr key={m.model}>
                        <th scope="row">
                          <TriCheckbox state={state(m.permissions)} onChange={(on) => setMany(m.permissions, on)} label={m.verbose_name} />
                          <bdi>{m.verbose_name}</bdi>
                        </th>
                        <td>
                          <span className="perm-list">
                            {m.permissions.map((p) => (
                              <label key={p.pk} className="check-row" title={p.codename}>
                                <input type="checkbox" checked={chosen.has(p.pk)} onChange={(e) => setMany([p], e.target.checked)} />
                                <bdi>{p.label}</bdi>
                              </label>
                            ))}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </li>
          );
        })}
        {!visibleApps.length && <li className="muted">{t("relation.noResults")}</li>}
      </ul>
    </div>
  );
}

function TriCheckbox({ state, onChange, label }: { state: "none" | "some" | "all"; onChange: (on: boolean) => void; label: string }) {
  const { t } = useI18n();
  return (
    <input
      type="checkbox"
      aria-label={t("perm.all", { label })}
      checked={state === "all"}
      ref={(el) => {
        if (el) el.indeterminate = state === "some";
      }}
      onChange={() => onChange(state !== "all")}
    />
  );
}
