import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { useI18n } from "../i18n";
import type { Column as ListColumn, Ref } from "../types";
import type { Cell, Spec } from "./model";

type Page = {
  model: string;
  verbose_name_plural: string;
  linkable: boolean;
  columns: ListColumn[];
  rows: { pk: string | number; label: string; cells: unknown[] }[];
  count: number;
  page: number;
  num_pages: number;
};

type Props = { spec: Spec; cell: Cell; measure: string | null; title: string; onClose: () => void };

/** The records behind a clicked report cell (POST /reports/drilldown), linked to CRUD. */
export default function Drilldown({ spec, cell, measure, title, onClose }: Props) {
  const { t, fmt, cap } = useI18n();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Page | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setError("");
    api<Page>("/reports/drilldown", { method: "POST", body: { spec, cell, measure, page } })
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, [spec, cell, measure, page]);

  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);

  const href = (pk: string | number) => (data ? `/${data.model.replace(".", "/")}/${pk}` : "#");

  return (
    <div className="overlay" role="dialog" aria-modal="true" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="card dialog drilldown">
        <div className="page-head">
          <div>
            <h2>{data ? cap(data.verbose_name_plural) : t("drill.title")}</h2>
            <p className="muted">
              <bdi>{title}</bdi>
              {data && <> · {t("drill.count", { count: data.count })}</>}
            </p>
          </div>
          <button type="button" onClick={onClose}>
            {t("drill.close")}
          </button>
        </div>
        {error && <p className="error">{error}</p>}
        {!data && !error && <p className="muted">{t("app.loading")}</p>}
        {data && (
          <>
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    {data.columns.map((c) => (
                      <th key={c.name}>{c.label}</th>
                    ))}
                    {!data.columns.length && <th>{cap(data.verbose_name_plural)}</th>}
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((row) => (
                    <tr key={String(row.pk)}>
                      {data.columns.length ? (
                        row.cells.map((value, i) => {
                          const col = data.columns[i];
                          const text = fmt.cell(value, col.kind);
                          const ref = value && typeof value === "object" && "model" in value ? (value as Ref) : null;
                          return (
                            <td key={col.name} className={`kind-${col.kind}`}>
                              {i === 0 && data.linkable ? (
                                <Link to={href(row.pk)} onClick={onClose}>
                                  <bdi>{text}</bdi>
                                </Link>
                              ) : ref?.model ? (
                                <Link to={`/${ref.model.replace(".", "/")}/${ref.pk}`} onClick={onClose}>
                                  <bdi>{text}</bdi>
                                </Link>
                              ) : (
                                <bdi>{text}</bdi>
                              )}
                            </td>
                          );
                        })
                      ) : (
                        <td>
                          <bdi>{row.label}</bdi>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
              {!data.rows.length && <p className="empty">{t("list.empty")}</p>}
            </div>
            {data.num_pages > 1 && (
              <div className="pagination">
                <span />
                <span className="pages">
                  <button type="button" disabled={data.page <= 1} onClick={() => setPage(data.page - 1)}>
                    {t("list.previous")}
                  </button>
                  <span>{t("list.page", { page: data.page, pages: data.num_pages })}</span>
                  <button type="button" disabled={data.page >= data.num_pages} onClick={() => setPage(data.page + 1)}>
                    {t("list.next")}
                  </button>
                </span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
