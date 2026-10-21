import { Link } from "react-router-dom";

import { useI18n } from "../i18n";
import type { DeletePlan } from "../types";

type Props = {
  plan: DeletePlan;
  title: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

export default function DeleteDialog({ plan, title, busy, onConfirm, onCancel }: Props) {
  const { t, fmt, cap } = useI18n();
  return (
    <div className="overlay" role="dialog" aria-modal="true">
      <div className="card dialog">
        <h2>{title}</h2>
        {plan.deletable ? (
          <>
            <p>{t("delete.willDelete")}</p>
            <ul>
              {plan.summary.map((s) => (
                <li key={s.model}>
                  <bdi>{cap(s.verbose_name)}</bdi>: {fmt.number(s.count)}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            {plan.protected.length > 0 && (
              <>
                <p className="error">{t("delete.protected")}</p>
                <ul>
                  {plan.protected.map((p) => (
                    <li key={p.model}>
                      <bdi>{cap(p.verbose_name)}</bdi> ({fmt.number(p.count)}):{" "}
                      {p.sample.map((s, i) => (
                        <span key={String(s.pk)}>
                          {i > 0 && t("common.listSeparator")}
                          <Link to={`/${p.model.replace(".", "/")}/${s.pk}`}>
                            <bdi>{s.label}</bdi>
                          </Link>
                        </span>
                      ))}
                    </li>
                  ))}
                </ul>
              </>
            )}
            {(plan.out_of_scope ?? []).length > 0 && (
              <p className="error">
                {t("delete.outOfScope", {
                  models: fmt.list((plan.out_of_scope ?? []).map((o) => `${cap(o.verbose_name)} (${fmt.number(o.count)})`)),
                })}
              </p>
            )}
            {plan.perms_needed.length > 0 && (
              <p className="error">{t("delete.noPermission", { models: fmt.list(plan.perms_needed) })}</p>
            )}
          </>
        )}
        <div className="dialog-actions">
          {plan.deletable && (
            <button type="button" className="danger" disabled={busy} onClick={onConfirm}>
              {t("delete.confirm")}
            </button>
          )}
          <button type="button" onClick={onCancel} disabled={busy}>
            {t("common.cancel")}
          </button>
        </div>
      </div>
    </div>
  );
}
