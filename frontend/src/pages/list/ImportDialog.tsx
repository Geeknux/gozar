import { useState } from "react";

import { api, crudPath, download, errorText } from "../../api";
import Dialog from "../../components/Dialog";
import { useI18n } from "../../i18n";
import type { ModelMeta } from "../../types";

type ImportRow = {
  row: number;
  action: "create" | "update" | "skip" | "error";
  pk?: string | null;
  label?: string;
  errors?: Record<string, string[]>;
};

type Report = {
  dry_run: boolean;
  committed: boolean;
  totals: Record<ImportRow["action"], number>;
  rows: ImportRow[];
  row_count: number;
  columns: { field: string; label: string }[];
  ignored_columns: string[];
};

type Props = { meta: ModelMeta; onClose: () => void; onImported: (count: number) => void };

/** Import CSV/XLSX: a dry run first (every row validated, nothing saved), then
 * the real import — all or nothing. */
export default function ImportDialog({ meta, onClose, onImported }: Props) {
  const { t, fmt, cap } = useI18n();
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const fieldLabels = Object.fromEntries(meta.fields.map((f) => [f.name, cap(f.verbose_name)]));

  async function run(dryRun: boolean) {
    if (!file) return;
    setBusy(true);
    setError("");
    const data = new FormData();
    data.set("file", file, file.name);
    try {
      const r = await api<Report>(crudPath(meta.label, `import?dry_run=${dryRun}`), { method: "POST", body: data });
      setReport(r);
      if (r.committed) onImported(r.totals.create + r.totals.update);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const template = (format: string) =>
    download(crudPath(meta.label, "import-template"), { format }, `${meta.model_name}-import.${format}`).catch((e) => setError(errorText(e)));

  const actionLabel: Record<ImportRow["action"], string> = {
    create: t("import.create"),
    update: t("import.update"),
    skip: t("import.skip"),
    error: t("import.error"),
  };

  return (
    <Dialog title={t("import.title", { model: meta.verbose_name_plural })} onClose={onClose} wide>
      <div className="dialog-form import-dialog">
        <p className="help">{t("import.intro")}</p>
        <p className="small">
          {t("import.template")}{" "}
          <button type="button" className="link" onClick={() => template("csv")}>
            CSV
          </button>{" "}
          ·{" "}
          <button type="button" className="link" onClick={() => template("xlsx")}>
            XLSX
          </button>
        </p>
        <input
          type="file"
          accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setReport(null);
          }}
        />
        {error && <p className="error">{error}</p>}
        {report && (
          <div className="import-report">
            {report.committed ? (
              <p className="notice">{t("import.done", { count: report.totals.create + report.totals.update })}</p>
            ) : report.totals.error ? (
              <p className="error">{t("import.hasErrors", { count: report.totals.error })}</p>
            ) : (
              <p className="notice">{t("import.ready")}</p>
            )}
            <p className="small">
              {(["create", "update", "skip", "error"] as const).map((a) => (
                <span key={a} className={`badge import-${a}`}>
                  {actionLabel[a]}: {fmt.number(report.totals[a])}
                </span>
              ))}
            </p>
            <p className="small muted">
              {t("import.columns")} {fmt.list(report.columns.map((c) => c.label))}
              {report.ignored_columns.length > 0 && (
                <>
                  {" · "}
                  {t("import.ignored")} {fmt.list(report.ignored_columns)}
                </>
              )}
            </p>
            <div className="table-wrap">
              <table className="grid import-rows">
                <thead>
                  <tr>
                    <th>{t("import.row")}</th>
                    <th>{t("import.result")}</th>
                    <th>{t("import.details")}</th>
                  </tr>
                </thead>
                <tbody>
                  {report.rows.map((r) => (
                    <tr key={r.row} className={r.action === "error" ? "has-error" : ""}>
                      <td>{fmt.number(r.row)}</td>
                      <td>
                        <span className={`badge import-${r.action}`}>{actionLabel[r.action]}</span>
                      </td>
                      <td className="wrap">
                        {r.label && <bdi>{r.label}</bdi>}
                        {r.errors &&
                          Object.entries(r.errors).map(([field, msgs]) => (
                            <div key={field} className="field-error">
                              {field !== "__all__" && <strong>{fieldLabels[field] ?? field}: </strong>}
                              {msgs.join(" ")}
                            </div>
                          ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {report.rows.length < report.row_count && <p className="small muted">{t("import.more", { count: report.row_count - report.rows.length })}</p>}
          </div>
        )}
        <div className="dialog-actions">
          <button type="button" className={report?.dry_run && !report.totals.error ? "" : "primary"} disabled={!file || busy} onClick={() => run(true)}>
            {t("import.check")}
          </button>
          <button
            type="button"
            className={report?.dry_run && !report.totals.error ? "primary" : ""}
            disabled={!file || busy || !report?.dry_run || report.totals.error > 0}
            onClick={() => run(false)}
          >
            {t("import.run")}
          </button>
          <button type="button" onClick={onClose}>
            {report?.committed ? t("common.close") : t("common.cancel")}
          </button>
        </div>
      </div>
    </Dialog>
  );
}
