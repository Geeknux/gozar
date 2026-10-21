import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, errorText } from "../api";
import { useI18n } from "../i18n";
import type { RecentAction } from "../types";

const ICONS: Record<RecentAction["action"], string> = { add: "+", change: "✎", delete: "×" };

/** admin's "Recent actions": the user's own latest edits (from the change log). */
export default function RecentActions({ limit = 10 }: { limit?: number }) {
  const { t, fmt } = useI18n();
  const [entries, setEntries] = useState<RecentAction[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<{ entries: RecentAction[] }>("/meta/recent-actions", { params: { limit: String(limit) } })
      .then((r) => setEntries(r.entries))
      .catch((e) => setError(errorText(e)));
  }, [limit]);

  return (
    <section className="card recent-actions">
      <h2>{t("recent.title")}</h2>
      {error && <p className="error">{error}</p>}
      {entries === null && !error && <p className="muted">{t("app.loading")}</p>}
      {entries && !entries.length && <p className="muted">{t("recent.none")}</p>}
      {entries && entries.length > 0 && (
        <ul>
          {entries.map((e, i) => (
            <li key={i} className={`recent-${e.action}`}>
              <span className="recent-icon" aria-label={e.action_display} title={e.action_display}>
                {ICONS[e.action]}
              </span>
              <span className="recent-body">
                {e.exists ? (
                  <Link to={`/${e.model.replace(".", "/")}/${e.object_pk}`}>
                    <bdi>{e.object_repr}</bdi>
                  </Link>
                ) : (
                  <bdi className="deleted">{e.object_repr}</bdi>
                )}
                <small className="muted">
                  <bdi>{e.verbose_name}</bdi> · {fmt.date(e.timestamp)}
                </small>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
