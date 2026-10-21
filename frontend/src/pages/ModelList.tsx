import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";

import { ApiError, api, crudPath, download, errorText } from "../api";
import DatePicker from "../components/DatePicker";
import DeleteDialog from "../components/DeleteDialog";
import FieldInput from "../components/FieldInput";
import { BooleanIcon, FileLink } from "../components/ReadonlyValue";
import RelationSelect from "../components/RelationSelect";
import { fromApi, toPayload, type Values } from "../forms";
import { useModelMeta, useShortcuts } from "../hooks";
import { useI18n } from "../i18n";
import type { Column, DeletePlan, FieldMeta, FilterSpec, FileValue, ListResponse, ListRow, Ref } from "../types";
import BulkEditDialog from "./list/BulkEditDialog";
import DateHierarchyBar from "./list/DateHierarchyBar";
import ImportDialog from "./list/ImportDialog";
import ViewsMenu from "./list/ViewsMenu";
import { isolate } from "../bidi";

const RESERVED = new Set(["q", "page", "ordering", "page_size", "columns", "view", "dh_year", "dh_month", "dh_day", "dh_calendar"]);

type Edits = Record<string, Values>; // row pk → changed list_editable values

export default function ModelList() {
  const { app, model } = useParams();
  const { t, cap } = useI18n();
  const label = `${app}.${model}`;
  const { meta, error: metaError } = useModelMeta(app, model);
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<ListResponse | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [acrossAll, setAcrossAll] = useState(false); // "select all N matching"
  const [action, setAction] = useState("");
  const [plan, setPlan] = useState<DeletePlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [edits, setEdits] = useState<Edits>({});
  const [rowErrors, setRowErrors] = useState<Record<string, Record<string, string[]>>>({});
  const [dialog, setDialog] = useState<"import" | "bulk" | null>(null);
  const location = useLocation();
  const [notice, setNotice] = useState((location.state as { notice?: string } | null)?.notice ?? "");
  const [search, setSearch] = useState(params.get("q") ?? "");
  const searchRef = useRef<HTMLInputElement>(null);

  const paramsKey = params.toString();
  const load = useCallback(() => {
    setError("");
    api<ListResponse>(crudPath(label), { params: Object.fromEntries(new URLSearchParams(paramsKey)) })
      .then((d) => {
        setData(d);
        setSelected(new Set());
        setAcrossAll(false);
        setEdits({});
        setRowErrors({});
      })
      .catch((e: Error) => setError(e.message));
  }, [label, paramsKey]);

  useEffect(load, [load]);
  useEffect(() => setSearch(new URLSearchParams(paramsKey).get("q") ?? ""), [paramsKey]);
  useEffect(() => {
    setNotice((location.state as { notice?: string } | null)?.notice ?? "");
    setAction("");
  }, [label, location.state]);

  const dirtyCount = Object.keys(edits).length;
  useShortcuts({
    "/": () => searchRef.current?.focus(),
    "mod+s": () => {
      if (dirtyCount) saveEdits();
    },
  });

  const update = (changes: Record<string, string | null>, keepPage = false) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    if (!keepPage) next.delete("page");
    setParams(next);
  };
  const replaceParams = (p: Record<string, string>) => setParams(new URLSearchParams(p));

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
  const listParams = useMemo(
    () => Object.fromEntries([...new URLSearchParams(paramsKey).entries()].filter(([k]) => k !== "page" && k !== "page_size")),
    [paramsKey],
  );

  if (metaError) return <p className="error">{metaError}</p>;
  if (!meta) return <p className="muted">{t("app.loading")}</p>;

  const fieldsByName: Record<string, FieldMeta> = Object.fromEntries(meta.fields.map((f) => [f.name, f]));
  const allSelected = !!data?.rows.length && data.rows.every((r) => selected.has(String(r.pk)));
  const actions = meta.list.actions.filter((a) => a.allowed);
  const canBulkEdit = meta.permissions.change && meta.list.bulk_edit_fields.length > 0;
  const selectable = actions.length > 0 || canBulkEdit;
  const canEditCells = meta.permissions.change && meta.list.editable.length > 0;
  const selectionCount = acrossAll ? (data?.count ?? 0) : selected.size;
  const selection = acrossAll ? { all: true as const, params: listParams } : { pks: [...selected] };

  async function runAction(e: FormEvent) {
    e.preventDefault();
    if (!action || !selectionCount) return;
    if (action === "delete_selected") {
      try {
        setPlan(await api<DeletePlan>(crudPath(label, "delete-plan"), { method: "POST", body: selection }));
      } catch (err) {
        setError(errorText(err));
      }
      return;
    }
    await execute(action);
  }

  async function execute(name: string) {
    setBusy(true);
    try {
      const r = await api<Record<string, unknown>>(crudPath(label, `actions/${name}`), { method: "POST", body: selection });
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

  function editValue(row: ListRow, name: string): unknown {
    const key = String(row.pk);
    if (edits[key] && name in edits[key]) return edits[key][name];
    return fromApi(fieldsByName[name], row.edit?.[name]);
  }

  function setEdit(row: ListRow, name: string, value: unknown) {
    const key = String(row.pk);
    setEdits((cur) => ({ ...cur, [key]: { ...(cur[key] ?? {}), [name]: value } }));
  }

  async function saveEdits() {
    setBusy(true);
    setRowErrors({});
    setError("");
    const rows = Object.entries(edits).map(([pk, values]) => ({
      pk,
      values: Object.fromEntries(Object.entries(values).map(([n, v]) => [n, toPayload(fieldsByName[n], v)])),
    }));
    try {
      const r = await api<{ updated: number; rows: ListRow[] }>(crudPath(label), { method: "PATCH", body: { rows } });
      setNotice(t("list.savedRows", { count: r.updated }));
      load();
    } catch (err) {
      if (err instanceof ApiError && err.data.row_errors) {
        setRowErrors(err.data.row_errors);
        setError(t("list.saveFailed"));
      } else setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function exportList(format: "csv" | "xlsx", onlySelected: boolean) {
    setError("");
    const p: Record<string, string> = { ...listParams, format };
    if (onlySelected && !acrossAll) p.pks = [...selected].join(",");
    try {
      const headers = await download(crudPath(label, "export"), p, `${model}.${format}`);
      if (headers.get("X-Gozar-Truncated")) setNotice(t("export.truncated"));
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <div className="list-page">
      <div className="page-head">
        <h1><bdi>{cap(meta.verbose_name_plural)}</bdi></h1>
        <span className="object-tools">
          {meta.permissions.import && (
            <button type="button" className="button" onClick={() => setDialog("import")}>
              {t("import.button")}
            </button>
          )}
          {meta.permissions.export && <ExportMenu selected={selectionCount} onExport={exportList} />}
          {meta.permissions.add && (
            <Link className="button primary" to={`/${app}/${model}/add`}>
              {t("list.add", { model: meta.verbose_name })}
            </Link>
          )}
        </span>
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
                  ref={searchRef}
                  dir="auto"
                  type="search"
                  placeholder={t("common.searchPlaceholder")}
                  aria-describedby={meta.list.search_help_text ? "search-help" : undefined}
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                <button type="submit">{t("common.search")}</button>
              </form>
            )}
            <ViewsMenu meta={meta} params={params} onApply={replaceParams} />
            {actions.length > 0 && (
              <form className="actions" onSubmit={runAction}>
                <select value={action} onChange={(e) => setAction(e.target.value)} aria-label={t("list.actionPlaceholder")}>
                  <option value="">{t("list.actionPlaceholder")}</option>
                  {actions.map((a) => (
                    <option key={a.name} value={a.name}>
                      {isolate(a.label)}
                    </option>
                  ))}
                </select>
                <button type="submit" disabled={!action || !selectionCount || busy}>
                  {t("list.run")}
                </button>
              </form>
            )}
            {canBulkEdit && (
              <button type="button" disabled={!selectionCount} onClick={() => setDialog("bulk")}>
                {t("bulk.button")}
              </button>
            )}
            {selectable && (
              <span className="muted">
                {acrossAll
                  ? t("list.allSelected", { count: data?.count ?? 0 })
                  : t("list.selected", { selected: selected.size, total: data?.rows.length ?? 0 })}
              </span>
            )}
          </div>
          {meta.list.search_help_text && (
            <small id="search-help" className="help">
              {meta.list.search_help_text}
            </small>
          )}
          {data?.date_hierarchy && <DateHierarchyBar dh={data.date_hierarchy} update={update} />}
          {selectable && allSelected && data && data.count > data.rows.length && (
            <p className="select-across">
              {acrossAll ? (
                <>
                  {t("list.allSelected", { count: data.count })}{" "}
                  <button type="button" className="link" onClick={() => (setAcrossAll(false), setSelected(new Set()))}>
                    {t("list.clearSelection")}
                  </button>
                </>
              ) : (
                <>
                  {t("list.pageSelected", { count: data.rows.length })}{" "}
                  <button type="button" className="link" onClick={() => setAcrossAll(true)}>
                    {t("list.selectAll", { count: data.count })}
                  </button>
                </>
              )}
            </p>
          )}

          {data && (
            <>
              <div className="table-wrap">
                <table className="grid">
                  <thead>
                    <tr>
                      {selectable && (
                        <th className="check">
                          <input
                            type="checkbox"
                            aria-label={t("list.selectPage")}
                            checked={allSelected}
                            onChange={() => {
                              setAcrossAll(false);
                              setSelected(allSelected ? new Set() : new Set(data.rows.map((r) => String(r.pk))));
                            }}
                          />
                        </th>
                      )}
                      {data.columns.map((c) => {
                        const dir = ordering[0] === c.name ? "▲" : ordering[0] === `-${c.name}` ? "▼" : "";
                        return (
                          <th key={c.name} className={c.boolean ? "center" : ""} aria-sort={dir === "▲" ? "ascending" : dir === "▼" ? "descending" : undefined}>
                            {c.sortable ? (
                              <button type="button" className="link" onClick={() => toggleSort(c.name)}>
                                <bdi>{c.label}</bdi> {dir}
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
                    {data.rows.map((row) => {
                      const key = String(row.pk);
                      const errs = rowErrors[key];
                      return (
                        <tr key={key} className={`${selected.has(key) || acrossAll ? "selected" : ""}${errs ? " has-error" : ""}${edits[key] ? " edited" : ""}`}>
                          {selectable && (
                            <td className="check">
                              <input
                                type="checkbox"
                                aria-label={t("list.selectRow", { label: row.label })}
                                checked={acrossAll || selected.has(key)}
                                onChange={() => {
                                  setAcrossAll(false);
                                  const next = new Set(selected);
                                  if (next.has(key)) next.delete(key);
                                  else next.add(key);
                                  setSelected(next);
                                }}
                              />
                            </td>
                          )}
                          {row.cells.map((cell, i) => {
                            const col = data.columns[i];
                            if (col.editable && canEditCells && row.edit && fieldsByName[col.name]) {
                              return (
                                <td key={col.name} className={`kind-${col.kind} editable-cell`}>
                                  <FieldInput
                                    id={`e-${key}-${col.name}`}
                                    field={fieldsByName[col.name]}
                                    widget={meta.form.widgets[col.name]?.type === "radio" ? { type: "select" } : meta.form.widgets[col.name]}
                                    model={label}
                                    value={editValue(row, col.name)}
                                    onChange={(v) => setEdit(row, col.name, v)}
                                    compact
                                  />
                                  {errs?.[col.name]?.map((m) => (
                                    <small key={m} className="field-error">
                                      {m}
                                    </small>
                                  ))}
                                </td>
                              );
                            }
                            return (
                              <td key={col.name} className={`kind-${col.kind}${col.boolean ? " center" : ""}`}>
                                <Cell col={col} cell={cell} row={row} app={app!} model={model!} />
                                {i === 0 && errs?.__all__?.map((m) => (
                                  <small key={m} className="field-error">
                                    {m}
                                  </small>
                                ))}
                              </td>
                            );
                          })}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                {!data.rows.length && <p className="empty">{t("list.empty")}</p>}
              </div>
              {dirtyCount > 0 && (
                <div className="edit-bar card">
                  <span>{t("list.unsaved", { count: dirtyCount })}</span>
                  <button type="button" className="primary" disabled={busy} onClick={saveEdits}>
                    {t("list.saveChanges")}
                  </button>
                  <button type="button" disabled={busy} onClick={() => (setEdits({}), setRowErrors({}))}>
                    {t("common.cancel")}
                  </button>
                  <small className="muted">{t("shortcuts.saveHint")}</small>
                </div>
              )}
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
          title={t("list.deleteTitle", { count: selectionCount, model: meta.verbose_name_plural })}
          busy={busy}
          onCancel={() => setPlan(null)}
          onConfirm={() => execute("delete_selected")}
        />
      )}
      {dialog === "import" && (
        <ImportDialog
          meta={meta}
          onClose={() => setDialog(null)}
          onImported={(count) => {
            setNotice(t("import.done", { count }));
            load();
          }}
        />
      )}
      {dialog === "bulk" && (
        <BulkEditDialog
          meta={meta}
          selection={selection}
          count={selectionCount}
          onClose={() => setDialog(null)}
          onDone={(count) => {
            setDialog(null);
            setNotice(t("bulk.done", { count }));
            load();
          }}
        />
      )}
    </div>
  );
}

function Cell({ col, cell, row, app, model }: { col: Column; cell: unknown; row: ListRow; app: string; model: string }) {
  const { fmt } = useI18n();
  if (cell === null || cell === undefined || cell === "") return <span className="muted">{col.empty_value ?? "—"}</span>;
  let content;
  if (col.boolean || typeof cell === "boolean") content = <BooleanIcon value={cell as boolean} />;
  else if (col.kind === "file" && typeof cell === "object") content = <FileLink value={cell as FileValue} image={col.image} />;
  else content = <bdi>{fmt.cell(cell, col.kind)}</bdi>;
  const ref = cell && typeof cell === "object" && "model" in (cell as object) ? (cell as Ref) : null;
  if (col.is_link) return <Link to={`/${app}/${model}/${row.pk}`}>{content}</Link>;
  if (ref?.model && !ref.restricted) return <Link to={`/${ref.model.replace(".", "/")}/${ref.pk}`}>{content}</Link>;
  return content;
}

function ExportMenu({ selected, onExport }: { selected: number; onExport: (format: "csv" | "xlsx", onlySelected: boolean) => void }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  const item = (format: "csv" | "xlsx", onlySelected: boolean, text: string) => (
    <li>
      <button type="button" className="menu-item" onClick={() => (onExport(format, onlySelected), setOpen(false))}>
        {text}
      </button>
    </li>
  );
  return (
    <div className="views-menu" ref={box}>
      <button type="button" className="button" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {t("export.button")} ▾
      </button>
      {open && (
        <div className="menu-popup">
          <ul>
            {item("csv", false, t("export.csv"))}
            {item("xlsx", false, t("export.xlsx"))}
            {selected > 0 && item("csv", true, t("export.selectedCsv", { count: selected }))}
            {selected > 0 && item("xlsx", true, t("export.selectedXlsx", { count: selected }))}
          </ul>
          <p className="small muted menu-footer">{t("export.hint")}</p>
        </div>
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
        {data.total !== null && data.count !== data.total && t("list.ofTotal", { total: data.total })}
      </span>
      {data.num_pages > 1 && (
        <span className="pages">
          <button type="button" disabled={data.page <= 1} onClick={() => onPage(data.page - 1)}>
            {t("list.previous")}
          </button>
          <span>{t("list.page", { page: data.page, pages: data.num_pages })}</span>
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
        <h4><bdi>{spec.label}</bdi></h4>
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
        <h4><bdi>{spec.label}</bdi></h4>
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
      <h4><bdi>{spec.label}</bdi></h4>
      <ul>
        {option(t("common.all"), !current && !isNull, {})}
        {choices.map((c) => option(c.label, current === String(c.value), { [name]: String(c.value) }))}
        {spec.null && option(t("filter.empty"), isNull === "true", { [nullKey]: "true" })}
      </ul>
    </div>
  );
}
