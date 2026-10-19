import { useState } from "react";

import SharedDialog from "../components/SharedDialog";
import AddToDashboard from "../dashboards/AddToDashboard";
import { useI18n } from "../i18n";
import ScheduleDialog from "../schedules/ScheduleDialog";
import { useSession } from "../session";
import { createSaved, deleteSaved, duplicateSaved, errorText, type SavedReport, sharingIn, updateSaved } from "../sharing";
import { compact, dataKey, type Spec } from "./model";

type Props = {
  saved: SavedReport | null;
  spec: Spec;
  onSaved: (report: SavedReport) => void;
  onDeleted?: () => void;
  /** a smaller set for the Ask panel: only "Save" / "Save as" */
  minimal?: boolean;
};

type Open = null | "saveAs" | "edit" | "schedule" | "dashboard";

/** Save / save as / rename & share / duplicate / delete / schedule / add to dashboard. */
export default function SavedActions({ saved, spec, onSaved, onDeleted, minimal }: Props) {
  const { t } = useI18n();
  const session = useSession();
  const [open, setOpen] = useState<Open>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const changed = saved !== null && (dataKey(spec) !== dataKey(saved.spec) || (spec.chart ?? "table") !== (saved.spec.chart ?? "table"));

  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {saved?.can_edit && (
        <button type="button" className="primary" disabled={busy || !changed} onClick={() => act(async () => onSaved(await updateSaved(saved.id, { spec: compact(spec) })))}>
          {t("saved.save")}
        </button>
      )}
      <button type="button" onClick={() => setOpen("saveAs")}>
        {saved ? t("saved.saveAs") : t("saved.saveReport")}
      </button>
      {!minimal && saved?.can_edit && (
        <button type="button" onClick={() => setOpen("edit")}>
          {t("saved.renameShare")}
        </button>
      )}
      {!minimal && saved && (
        <button type="button" disabled={busy} onClick={() => act(async () => onSaved(await duplicateSaved(saved.id)))}>
          {t("saved.duplicate")}
        </button>
      )}
      {!minimal && saved && (
        <button type="button" onClick={() => setOpen("schedule")}>
          {t("schedule.emailIt")}
        </button>
      )}
      {!minimal && (
        <button type="button" onClick={() => setOpen("dashboard")}>
          {t("dash.addToDashboard")}
        </button>
      )}
      {!minimal && saved?.can_edit && (
        <button
          type="button"
          className="danger"
          disabled={busy}
          onClick={() =>
            window.confirm(t("saved.confirmDelete", { name: saved.name })) &&
            act(async () => {
              await deleteSaved(saved.id);
              onDeleted?.();
            })
          }
        >
          {t("common.delete")}
        </button>
      )}
      {error && <span className="error">{error}</span>}
      {open === "saveAs" && (
        <SharedDialog
          title={t("saved.saveTitle")}
          submitLabel={t("form.save")}
          initial={saved ? { name: t("saved.copyName", { name: saved.name }), description: saved.description } : undefined}
          onSubmit={async (v) => onSaved(await createSaved({ ...v, sharing: sharingIn(v.sharing), spec: compact(spec) }))}
          onClose={() => setOpen(null)}
        />
      )}
      {open === "edit" && saved && (
        <SharedDialog
          title={t("saved.editTitle")}
          submitLabel={t("form.save")}
          initial={{ name: saved.name, description: saved.description, sharing: saved.sharing }}
          onSubmit={async (v) => onSaved(await updateSaved(saved.id, { ...v, sharing: sharingIn(v.sharing) }))}
          onClose={() => setOpen(null)}
        />
      )}
      {open === "schedule" && saved && (
        <ScheduleDialog
          target={{ kind: "saved_report", id: saved.id, name: saved.name }}
          me={session.user}
          defaultTimezone={session.timezone}
          onSaved={() => undefined}
          onClose={() => setOpen(null)}
        />
      )}
      {open === "dashboard" && (
        <AddToDashboard
          savedId={saved && !changed ? saved.id : null}
          spec={compact(spec)}
          title={saved?.name ?? t("report.title")}
          onClose={() => setOpen(null)}
        />
      )}
    </>
  );
}
