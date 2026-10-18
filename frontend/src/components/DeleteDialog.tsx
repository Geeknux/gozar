import { Link } from "react-router-dom";

import type { DeletePlan } from "../types";
import { formatNumber } from "../format";

type Props = {
  plan: DeletePlan;
  title: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

export default function DeleteDialog({ plan, title, busy, onConfirm, onCancel }: Props) {
  return (
    <div className="overlay" role="dialog" aria-modal="true">
      <div className="card dialog">
        <h2>{title}</h2>
        {plan.deletable ? (
          <>
            <p>این موارد حذف خواهند شد:</p>
            <ul>
              {plan.summary.map((s) => (
                <li key={s.model}>
                  {s.verbose_name}: {formatNumber(s.count)}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            {plan.protected.length > 0 && (
              <>
                <p className="error">به‌خاطر وابستگی این رکوردها، حذف ممکن نیست:</p>
                <ul>
                  {plan.protected.map((p) => (
                    <li key={p.model}>
                      {p.verbose_name} ({formatNumber(p.count)}):{" "}
                      {p.sample.map((s, i) => (
                        <span key={String(s.pk)}>
                          {i > 0 && "، "}
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
            {plan.perms_needed.length > 0 && (
              <p className="error">
                برای حذف این موارد مجوز ندارید: {plan.perms_needed.join("، ")}
              </p>
            )}
          </>
        )}
        <div className="dialog-actions">
          {plan.deletable && (
            <button type="button" className="danger" disabled={busy} onClick={onConfirm}>
              بله، حذف شود
            </button>
          )}
          <button type="button" onClick={onCancel} disabled={busy}>
            انصراف
          </button>
        </div>
      </div>
    </div>
  );
}
