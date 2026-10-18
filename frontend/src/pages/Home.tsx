import { Link } from "react-router-dom";

import type { AppEntry } from "../types";

export default function Home({ apps }: { apps: AppEntry[] }) {
  if (!apps.length) return <p className="empty">به هیچ مدلی دسترسی ندارید.</p>;
  return (
    <div className="home">
      <h1>پیشخوان</h1>
      {apps.map((app) => (
        <section key={app.app_label} className="card">
          <h2>{app.verbose_name}</h2>
          <table className="grid">
            <tbody>
              {app.models.map((m) => (
                <tr key={m.label}>
                  <td>
                    <Link to={`/${m.label.replace(".", "/")}`}>{m.verbose_name_plural}</Link>
                  </td>
                  <td className="actions-cell">
                    {m.permissions.add && (
                      <Link to={`/${m.label.replace(".", "/")}/add`} className="button small">
                        + افزودن
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
