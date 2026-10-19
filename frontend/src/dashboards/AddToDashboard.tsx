import { type FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import Dialog from "../components/Dialog";
import { useI18n } from "../i18n";
import type { Spec } from "../reports/model";
import { errorText } from "../sharing";
import { type DashboardSummary, createDashboard, getDashboard, listDashboards, nextRow, updateDashboard, widgetIn } from "./api";

type Props = {
  /** a saved report (unchanged) becomes a saved-report widget, otherwise an inline spec */
  savedId: number | null;
  spec: Spec;
  title: string;
  onClose: () => void;
};

/** "Add to dashboard": append this report to one of my dashboards, or a new one. */
export default function AddToDashboard({ savedId, spec, title, onClose }: Props) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [boards, setBoards] = useState<DashboardSummary[] | null>(null);
  const [target, setTarget] = useState<string>("new");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    listDashboards()
      .then((r) => {
        const mine = r.dashboards.filter((b) => b.can_edit);
        setBoards(mine);
        if (mine.length) setTarget(String(mine[0].id));
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    const chart = spec.chart ?? "table";
    const widget = {
      title: savedId ? "" : title,
      saved_report: savedId,
      spec: savedId ? null : spec,
      chart,
      x: 0,
      w: chart === "kpi" ? 12 : 6,
      h: chart === "kpi" ? 2 : 4,
    };
    try {
      let id: number;
      if (target === "new") {
        const board = await createDashboard({
          name: name || title,
          description: "",
          sharing: { visibility: "private", users: [], groups: [] },
          widgets: [{ ...widget, y: 0 }],
        });
        id = board.id;
      } else {
        const board = await getDashboard(target);
        const widgets = board.widgets.map(widgetIn);
        await updateDashboard(board.id, { widgets: [...widgets, { ...widget, y: nextRow(board.widgets) }] });
        id = board.id;
      }
      onClose();
      navigate(`/dashboards/${id}`);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title={t("dash.addToDashboard")} onClose={onClose}>
      <form className="dialog-form" onSubmit={submit}>
        {!boards && !error && <p className="muted">{t("app.loading")}</p>}
        {boards && (
          <label className="stack">
            {t("dash.dashboard")}
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              {boards.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
              <option value="new">{t("dash.newDashboard")}</option>
            </select>
          </label>
        )}
        {target === "new" && (
          <label className="stack">
            {t("saved.name")}
            <input dir="auto" value={name} placeholder={title} onChange={(e) => setName(e.target.value)} />
          </label>
        )}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="submit" className="primary" disabled={busy || !boards}>
            {t("dash.add")}
          </button>
          <button type="button" onClick={onClose}>
            {t("common.cancel")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
