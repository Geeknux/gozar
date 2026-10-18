import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";

import { ApiError, api, crudPath } from "../api";
import DatePicker from "../components/DatePicker";
import DeleteDialog from "../components/DeleteDialog";
import RelationSelect from "../components/RelationSelect";
import { useModelMeta } from "../hooks";
import { useI18n } from "../i18n";
import type { DeletePlan, FilterSpec, ListResponse, Ref } from "../types";

const RESERVED = new Set(["q", "page", "ordering", "page_size"]);

export default function ModelList() {
  const { app, model } = useParams();
  const { t, fmt, cap } = useI18n();
  const label = `${app}.${model}`;
  const { meta, error: metaError } = useModelMeta(app, model);
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<ListResponse | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [action, setAction] = useState("");
  const [plan, setPlan] = useState<DeletePlan | null>(null);
  const [busy, setBusy] = useState(false);
  const location = useLocation();
  const [notice, setNotice] = useState((location.state as { notice?: string } | null)?.notice ?? "");
  const [search, setSearch] = useState(params.get("q") ?? "");

  const paramsKey = params.toString();
  const load = useCallback(() => {
    setError("");
    api<ListResponse>(crudPath(label), { params: Object.fromEntries(new URLSearchParams(paramsKey)) })
      .then((d) => {
        setData(d);
        setSelected(new Set());
      })
      .catch((e: Error) => setError(e.message));
  }, [label, paramsKey]);

  useEffect(load, [load]);
  useEffect(() => setSearch(new URLSearchParams(paramsKey).get("q") ?? ""), [paramsKey]);
  useEffect(() => {
    setNotice((location.state as { notice?: string } | null)?.notice ?? "");
    setAction("");
  }, [label, location.state]);

  const update = (changes: Record<string, string | null>, keepPage = false) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    if (!keepPage) next.delete("page");
    setParams(next);
  };

  const ordering = (params.get("ordering") ?? "").split(",").filter(Boolean);
  const toggleSort = (name: string) => {
    const current = ordering[0];
    const next = current === name ? `-${name}` : current === `-${name}` ? "" : name;
    update({ ordering: next });
  };

  const activeFilters = useMemo(
    () => [...new URLSearchParams(paramsKey).entries()].filter(([k]) => !RESERVED.has(k)),
    [paramsKey],
  );

  if (metaError) return <p className="error">{metaError}</p>;
  if (!meta) return <p className="muted">{t("app.loading")}</p>;

  const allSelected = !!data?.rows.length && data.rows.every((r) => selected.has(String(r.pk)));
  const actions = meta.list.actions.filter((a) => a.allowed);

  async function runAction(e: FormEvent) {
    e.preventDefault();
    if (!action || !selected.size) return;
    const pks = [...selected];
    if (action === "delete_selected") {
      setPlan(await api<DeletePlan>(crudPath(label, "delete-plan"), { method: "POST", body: { pks } }));
      return;
    }
    await execute(action, pks);
  }

  async function execute(name: string, pks: string[]) {
    setBusy(true);
    try {
      const r = await api<Record<string, unknown>>(crudPath(label, `actions/${name}`), {
        method: "POST",
        body: { pks },
      });
      const count = (r.deleted ?? r.updated) as number | undefined;
      setNotice(count !== undefined ? t("list.doneCount", { count }) : t("list.done"));
      setPlan(null);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="list-page">
      <div className="page-head">
        <h1>{cap(meta.verbose_name_plural)}</h1>
        {meta.permissions.add && (
          <Link className="button primary" to={`/${app}/${model}/add`}>
            {t("list.add", { model: meta.verbose_name })}
          </Link>
        )}
      </div>
      {notice && <p className="notice">{notice}</p>}
      {error && <p className="error">{error}</p>}

      <div className="list-layout">
        <div className="list-main">
          <div className="toolbar">
            {meta.list.search_fields.length > 0 && (
              <form
                className="search"
                onSubmit={(e) => {
                  e.preventDefault();
                  update({ q: search.trim() });
                }}
              >
                <input
                  dir="auto"
                  type="search"
                  placeholder={t("common.searchPlaceholder")}
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                <button type="submit">{t("common.search")}</button>
              </form>
            )}
            {actions.length > 0 && (
              <form className="actions" onSubmit={runAction}>
                <select value={action} onChange={(e) => setAction(e.target.value)}>
                  <option value="">{t("list.actionPlaceholder")}</option>
                  {actions.map((a) => (
                    <option key={a.name} value={a.name}>
                      {a.label}
                    </option>
                  ))}
                </select>
                <button type="submit" disabled={!action || !selected.size || busy}>
                  {t("list.run")}
                </button>
                <span className="muted">
                  {t("list.selected", { selected: selected.size, total: data?.rows.length ?? 0 })}
                </span>
              </form>
            )}
          </div>

          {data && (
            <>
              <div className="table-wrap">
                <table className="grid">
                  <thead>
                    <tr>
                      {actions.length > 0 && (
                        <th className="check">
                          <input
                            type="checkbox"
                            checked={allSelected}
                            onChange={() =>
                              setSelected(allSelected ? new Set() : new Set(data.rows.map((r) => String(r.pk))))
                            }
                          />
                        </th>
                      )}
                      {data.columns.map((c) => {
                        const dir = ordering[0] === c.name ? "▲" : ordering[0] === `-${c.name}` ? "▼" : "";
                        return (
                          <th key={c.name}>
                            {c.sortable ? (
                              <button type="button" className="link" onClick={() => toggleSort(c.name)}>
                                {c.label} {dir}
                              </button>
                            ) : (
                              c.label
                            )}
                          </th>
                        );
                      })}
                    </tr>
                  </thead>
                  <tbody>
                    {data.rows.map((row) => (
                      <tr key={String(row.pk)} className={selected.has(String(row.pk)) ? "selected" : ""}>
                        {actions.length > 0 && (
                          <td className="check">
                            <input
                              type="checkbox"
                              checked={selected.has(String(row.pk))}
                              onChange={() => {
                                const next = new Set(selected);
                                const k = String(row.pk);
                                if (next.has(k)) next.delete(k);
                                else next.add(k);
                                setSelected(next);
                              }}
                            />
                          </td>
                        )}
                        {row.cells.map((cell, i) => {
                          const col = data.columns[i];
                          const text = fmt.cell(cell, col.kind);
                          const ref = cell && typeof cell === "object" && "model" in (cell as object) ? (cell as Ref) : null;
                          return (
                            <td key={col.name} className={`kind-${col.kind}`}>
                              {col.is_link ? (
                                <Link to={`/${app}/${model}/${row.pk}`}>
                                  <bdi>{text}</bdi>
                                </Link>
                              ) : ref?.model ? (
                                <Link to={`/${ref.model.replace(".", "/")}/${ref.pk}`}>
                                  <bdi>{text}</bdi>
                                </Link>
                              ) : (
                                <bdi>{text}</bdi>
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!data.rows.length && <p className="empty">{t("list.empty")}</p>}
              </div>
              <Pagination data={data} onPage={(p) => update({ page: String(p) }, true)} />
            </>
          )}
        </div>

        {meta.list.filters.length > 0 && (
          <aside className="filters card">
            <h3>{t("list.filters")}</h3>
            {activeFilters.length > 0 && (
              <button
                type="button"
                className="link"
                onClick={() => update(Object.fromEntries(activeFilters.map(([k]) => [k, null])))}
              >
                {t("list.clearFilters")}
              </button>
            )}
            {meta.list.filters.map((f) => (
              <FilterBox key={f.name} spec={f} model={label} params={params} update={update} />
            ))}
          </aside>
        )}
      </div>

      {plan && (
        <DeleteDialog
          plan={plan}
          title={t("list.deleteTitle", { count: selected.size, model: meta.verbose_name_plural })}
          busy={busy}
          onCancel={() => setPlan(null)}
          onConfirm={() => execute("delete_selected", [...selected])}
        />
      )}
    </div>
  );
}

function Pagination({ data, onPage }: { data: ListResponse; onPage: (p: number) => void }) {
  const { t } = useI18n();
  return (
    <div className="pagination">
      <span className="muted">
        {t("list.count", { count: data.count })}
        {data.count !== data.total && t("list.ofTotal", { total: data.total })}
      </span>
      {data.num_pages > 1 && (
        <span className="pages">
          <button type="button" disabled={data.page <= 1} onClick={() => onPage(data.page - 1)}>
            {t("list.previous")}
          </button>
          <span>
            {t("list.page", { page: data.page, pages: data.num_pages })}
          </span>
          <button type="button" disabled={data.page >= data.num_pages} onClick={() => onPage(data.page + 1)}>
            {t("list.next")}
          </button>
        </span>
      )}
    </div>
  );
}

type FilterProps = {
  spec: FilterSpec;
  model: string;
  params: URLSearchParams;
  update: (changes: Record<string, string | null>) => void;
};

function FilterBox({ spec, model, params, update }: FilterProps) {
  const { t, fmt } = useI18n();
  const name = spec.name;
  const nullKey = `${name}__isnull`;
  const current = params.get(name);
  const isNull = params.get(nullKey);
  const clearAll = { [name]: null, [nullKey]: null };

  const option = (text: string, active: boolean, changes: Record<string, string | null>) => (
    <li key={text}>
      <button type="button" className={active ? "link active" : "link"} onClick={() => update({ ...clearAll, ...changes })}>
        <bdi>{text}</bdi>
      </button>
    </li>
  );

  if (spec.kind === "date" || spec.kind === "datetime") {
    const suffix = spec.kind === "datetime" ? "__date" : "";
    const from = `${name}${suffix}__gte`;
    const to = `${name}${suffix}__lte`;
    return (
      <div className="filter">
        <h4>{spec.label}</h4>
        <label>
          {t("filter.from")}
          <DatePicker value={params.get(from) ?? ""} onChange={(v) => update({ [from]: v })} />
        </label>
        <label>
          {t("filter.to")}
          <DatePicker value={params.get(to) ?? ""} onChange={(v) => update({ [to]: v })} />
        </label>
        {spec.null && (
          <ul>
            {option(t("filter.noValue"), isNull === "true", { [nullKey]: "true", [from]: null, [to]: null })}
            {option(t("filter.hasValue"), isNull === "false", { [nullKey]: "false" })}
          </ul>
        )}
      </div>
    );
  }

  if (spec.kind === "relation" && !spec.choices) {
    return (
      <div className="filter">
        <h4>{spec.label}</h4>
        <RelationSelect
          model={model}
          field={name}
          value={current ? { pk: current, label: `#${fmt.digits(current)}` } : null}
          onChange={(r) => update({ ...clearAll, [name]: r ? String(r.pk) : null })}
          placeholder={t("common.all")}
        />
      </div>
    );
  }

  const choices =
    spec.choices ??
    (spec.kind === "boolean"
      ? [
          { value: "true", label: t("common.yes") },
          { value: "false", label: t("common.no") },
        ]
      : []);
  return (
    <div className="filter">
      <h4>{spec.label}</h4>
      <ul>
        {option(t("common.all"), !current && !isNull, {})}
        {choices.map((c) => option(c.label, current === String(c.value), { [name]: String(c.value) }))}
        {spec.null && option(t("filter.empty"), isNull === "true", { [nullKey]: "true" })}
      </ul>
    </div>
  );
}
