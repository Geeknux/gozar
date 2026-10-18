import { Link } from "react-router-dom";

import { useI18n } from "../i18n";
import type { AppEntry } from "../types";

export default function Home({ apps }: { apps: AppEntry[] }) {
  const { t, cap } = useI18n();
  if (!apps.length) return <p className="empty">{t("home.noAccess")}</p>;
  return (
    <div className="home">
      <h1>{t("nav.dashboard")}</h1>
      {apps.map((app) => (
        <section key={app.app_label} className="card">
          <h2>{cap(app.verbose_name)}</h2>
          <table className="grid">
            <tbody>
              {app.models.map((m) => (
                <tr key={m.label}>
                  <td>
                    <Link to={`/${m.label.replace(".", "/")}`}>{cap(m.verbose_name_plural)}</Link>
                  </td>
                  <td className="actions-cell">
                    {m.permissions.add && (
                      <Link to={`/${m.label.replace(".", "/")}/add`} className="button small">
                        {t("home.add")}
                      </Link>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}
