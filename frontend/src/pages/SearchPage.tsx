import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, errorText } from "../api";
import { useI18n } from "../i18n";

type Group = {
  model: string;
  verbose_name: string;
  verbose_name_plural: string;
  app_verbose_name: string;
  count: number;
  more: boolean;
  results: { pk: string | number; label: string }[];
};

/** Global search: every model the user may view, with its own search rules and
 * row-level scope; each group links to its filtered list. */
export default function SearchPage() {
  const { t, fmt } = useI18n();
  const [params] = useSearchParams();
  const q = params.get("q") ?? "";
  const [groups, setGroups] = useState<Group[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setGroups(null);
    setError("");
    if (q.trim().length < 2) {
      setGroups([]);
      return;
    }
    api<{ groups: Group[] }>("/search", { params: { q } })
      .then((r) => setGroups(r.groups))
      .catch((e) => setError(errorText(e)));
  }, [q]);

  return (
    <div className="search-page">
      <div className="page-head">
        <h1>{q ? t("search.titleFor", { q }) : t("search.title")}</h1>
      </div>
      {error && <p className="error">{error}</p>}
      {groups === null && !error && <p className="muted">{t("app.loading")}</p>}
      {groups && q.trim().length < 2 && <p className="muted">{t("search.tooShort")}</p>}
      {groups && q.trim().length >= 2 && !groups.length && <p className="empty">{t("search.none")}</p>}
      <div className="search-groups">
        {groups?.map((g) => {
          const path = `/${g.model.replace(".", "/")}`;
          return (
            <section key={g.model} className="card">
              <div className="inline-head">
                <h2>
                  <Link to={`${path}?q=${encodeURIComponent(q)}`}><bdi>{g.verbose_name_plural}</bdi></Link>
                </h2>
                <span className="muted">
                  <bdi>{g.app_verbose_name}</bdi> · {g.more ? t("search.manyHits", { count: g.count }) : t("list.count", { count: g.count })}
                </span>
              </div>
              <ul className="search-hits">
                {g.results.map((r) => (
                  <li key={String(r.pk)}>
                    <Link to={`${path}/${r.pk}`}>
                      <bdi>{r.label}</bdi>
                    </Link>
                  </li>
                ))}
              </ul>
              {g.count > g.results.length && (
                <Link className="small" to={`${path}?q=${encodeURIComponent(q)}`}>
                  {t("search.showAll", { count: fmt.number(g.count) })}
                </Link>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
