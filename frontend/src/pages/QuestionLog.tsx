import { type FormEvent, Fragment, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../api";
import { useI18n } from "../i18n";

type Entry = {
  id: number;
  timestamp: string;
  user: string | null;
  question: string;
  previous_spec: Record<string, unknown> | null;
  status: "ok" | "clarification" | "invalid" | "denied" | "failed";
  status_display: string;
  spec: Record<string, unknown> | null;
  interpretation: string;
  clarification: string;
  provider: string;
  model: string;
  attempts: number;
  cache_hit: boolean;
  duration_ms: number | null;
  error_code: string;
  error: string;
  has_transcript: boolean;
};

type Page = {
  entries: Entry[];
  count: number;
  page: number;
  num_pages: number;
  filters: { status: { value: string; label: string }[] };
};

/** Natural-language questions (GET /audit/questions), for bi_admin.view_nllog. */
export default function QuestionLog() {
  const { t, fmt } = useI18n();
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<Page | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [search, setSearch] = useState(params.get("q") ?? "");
  const key = params.toString();

  useEffect(() => {
    setError("");
    api<Page>("/audit/questions", { params: Object.fromEntries(new URLSearchParams(key)) })
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, [key]);

  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (!("page" in changes)) next.delete("page");
    setParams(next);
  };
  const submitSearch = (e: FormEvent) => {
    e.preventDefault();
    update({ q: search.trim() });
  };
  const filtered = ["status", "user", "q"].some((f) => params.get(f));

  return (
    <div className="audit-page">
      <div className="page-head">
        <h1>{t("questions.title")}</h1>
        {data && <span className="muted">{t("questions.count", { count: data.count })}</span>}
      </div>
      <p className="muted">{t("questions.intro")}</p>
      <div className="toolbar audit-filters">
        <form onSubmit={submitSearch} className="search">
          <input
            type="search"
            dir="auto"
            value={search}
            placeholder={t("questions.searchPlaceholder")}
            aria-label={t("common.search")}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button type="submit">{t("common.search")}</button>
        </form>
        <select aria-label={t("audit.status")} value={params.get("status") ?? ""} onChange={(e) => update({ status: e.target.value })}>
          <option value="">{t("audit.anyStatus")}</option>
          {data?.filters.status.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        {params.get("user") && (
          <span className="chip">
            {t("audit.user")}: <bdi>{params.get("user")}</bdi>
          </span>
        )}
        {filtered && (
          <button type="button" className="link" onClick={() => setParams(new URLSearchParams())}>
            {t("list.clearFilters")}
          </button>
        )}
      </div>
      {error && <p className="error">{error}</p>}
      {!data && !error && <p className="muted">{t("app.loading")}</p>}
      {data && !data.entries.length && <p className="empty">{t("questions.empty")}</p>}
      {data && data.entries.length > 0 && (
        <div className="table-wrap">
          <table className="grid audit-table">
            <thead>
              <tr>
                <th>{t("audit.time")}</th>
                <th>{t("audit.user")}</th>
                <th>{t("questions.question")}</th>
                <th>{t("audit.status")}</th>
                <th>{t("questions.provider")}</th>
                <th className="num">{t("questions.attempts")}</th>
                <th className="num">{t("audit.duration")}</th>
                <th>{t("audit.cache")}</th>
              </tr>
            </thead>
            <tbody>
              {data.entries.map((e) => (
                <Fragment key={e.id}>
                  <tr className={open === e.id ? "selected" : ""}>
                    <td>
                      <button type="button" className="link" aria-expanded={open === e.id} onClick={() => setOpen(open === e.id ? null : e.id)}>
                        {fmt.date(e.timestamp)}
                      </button>
                    </td>
                    <td>
                      {e.user ? (
                        <button type="button" className="link" onClick={() => update({ user: e.user ?? "" })}>
                          <bdi>{e.user}</bdi>
                        </button>
                      ) : (
                        <span className="muted">{t("audit.system")}</span>
                      )}
                    </td>
                    <td dir="auto">{e.question}</td>
                    <td>
                      <span className={`badge status-${e.status}`}>{e.status_display}</span>
                      {e.error_code && <span className="mono muted"> {e.error_code}</span>}
                    </td>
                    <td className="mono">{e.model ? `${e.provider} · ${e.model}` : e.provider || "—"}</td>
                    <td className="num">{fmt.number(e.attempts)}</td>
                    <td className="num">{e.duration_ms === null ? "—" : t("audit.ms", { ms: e.duration_ms })}</td>
                    <td>{e.cache_hit ? t("audit.cacheHit") : ""}</td>
                  </tr>
                  {open === e.id && (
                    <tr className="audit-detail">
                      <td colSpan={8}>
                        <dl>
                          {(e.interpretation || e.clarification) && (
                            <>
                              <dt>{t("questions.answer")}</dt>
                              <dd dir="auto">{e.interpretation || e.clarification}</dd>
                            </>
                          )}
                          {e.spec && (
                            <>
                              <dt>{t("audit.spec")}</dt>
                              <dd>
                                <pre dir="ltr">{JSON.stringify(e.spec, null, 2)}</pre>
                                {e.status === "ok" && (
                                  <Link to={`/reports?spec=${encodeURIComponent(JSON.stringify(e.spec))}`}>{t("audit.openReport")}</Link>
                                )}
                              </dd>
                            </>
                          )}
                          {e.previous_spec && (
                            <>
                              <dt>{t("questions.previous")}</dt>
                              <dd>
                                <pre dir="ltr">{JSON.stringify(e.previous_spec, null, 2)}</pre>
                              </dd>
                            </>
                          )}
                          {e.error && (
                            <>
                              <dt>{t("audit.error")}</dt>
                              <dd>
                                <bdi>{e.error}</bdi>
                              </dd>
                            </>
                          )}
                          {e.has_transcript && <dd className="muted">{t("questions.transcript")}</dd>}
                        </dl>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && data.num_pages > 1 && (
        <div className="pagination">
          <span className="muted">{t("list.page", { page: data.page, pages: data.num_pages })}</span>
          <span className="pages">
            <button type="button" disabled={data.page <= 1} onClick={() => update({ page: String(data.page - 1) })}>
              {t("list.previous")}
            </button>
            <button type="button" disabled={data.page >= data.num_pages} onClick={() => update({ page: String(data.page + 1) })}>
              {t("list.next")}
            </button>
          </span>
        </div>
      )}
    </div>
  );
}
