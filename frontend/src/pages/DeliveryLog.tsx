import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { useI18n } from "../i18n";
import type { Delivery } from "../schedules/api";
import Deliveries from "../schedules/Deliveries";
import { isolate } from "../bidi";

type Page = { entries: Delivery[]; count: number; page: number; num_pages: number; filters: { status: { value: string; label: string }[] } };

/** Every scheduled e-mail sent, skipped or failed (GET /audit/deliveries). */
export default function DeliveryLog() {
  const { t } = useI18n();
  const [data, setData] = useState<Page | null>(null);
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");

  useEffect(() => {
    const params: Record<string, string> = { page: String(page) };
    if (status) params.status = status;
    api<Page>("/audit/deliveries", { params })
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, [status, page]);

  return (
    <div className="audit-page">
      <div className="page-head">
        <h1>{t("deliveries.title")}</h1>
        {data && <span className="muted">{t("audit.count", { count: data.count })}</span>}
      </div>
      <p className="muted">
        {t("deliveries.intro")} <Link to="/audit?kind=email">{t("deliveries.runs")}</Link>
      </p>
      <div className="toolbar">
        <select value={status} aria-label={t("audit.status")} onChange={(e) => (setStatus(e.target.value), setPage(1))}>
          <option value="">{t("audit.anyStatus")}</option>
          {data?.filters.status.map((s) => (
            <option key={s.value} value={s.value}>
              {isolate(s.label)}
            </option>
          ))}
        </select>
      </div>
      {error && <p className="error">{error}</p>}
      {data && (
        <div className="table-wrap">
          <Deliveries items={data.entries} showSchedule />
        </div>
      )}
      {data && data.num_pages > 1 && (
        <div className="pagination">
          <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            {t("list.previous")}
          </button>
          <span>{t("list.page", { page: data.page, pages: data.num_pages })}</span>
          <button type="button" disabled={page >= data.num_pages} onClick={() => setPage(page + 1)}>
            {t("list.next")}
          </button>
        </div>
      )}
    </div>
  );
}
