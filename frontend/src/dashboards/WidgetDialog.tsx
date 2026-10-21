import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api";
import { isolate } from "../bidi";
import Dialog from "../components/Dialog";
import TranslatedInput, { cleanTexts } from "../components/TranslatedInput";
import { type MessageKey, useI18n } from "../i18n";
import Builder from "../reports/Builder";
import { type CatalogModel, CHARTS, type ChartType, compact, type Spec, type SpecError } from "../reports/model";
import { useSession } from "../session";
import { listSaved, type SavedReport, type Texts } from "../sharing";
import type { Widget } from "./api";

type Props = {
  /** the widget to edit; a new one when absent */
  widget?: Widget;
  onDone: (w: Pick<Widget, "title" | "title_i18n" | "saved_report" | "spec" | "chart" | "inline">) => void;
  onClose: () => void;
};

/** Add or edit a widget: one of the saved reports I can open, or a report built here. */
export default function WidgetDialog({ widget, onDone, onClose }: Props) {
  const { t, lang } = useI18n();
  const { content } = useSession();
  const [source, setSource] = useState<"saved" | "inline">(widget && !widget.inline ? "saved" : widget ? "inline" : "saved");
  const [saved, setSaved] = useState<SavedReport[] | null>(null);
  const [savedId, setSavedId] = useState<number | null>(widget?.saved_report?.id ?? null);
  const [catalog, setCatalog] = useState<CatalogModel[] | null>(null);
  const [spec, setSpec] = useState<Spec | null>(widget?.inline ? widget.spec : null);
  const [titles, setTitles] = useState<Texts>(widget?.title_i18n ?? {});
  const [chart, setChart] = useState<ChartType>(widget?.chart ?? "table");
  const [errors, setErrors] = useState<SpecError[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    listSaved()
      .then((items) => {
        setSaved(items);
        if (savedId === null && items.length) setSavedId(items[0].id);
      })
      .catch((e: Error) => setError(e.message));
    api<{ semantic_models: CatalogModel[] }>("/reports/catalog")
      .then((c) => {
        setCatalog(c.semantic_models);
        setSpec((s) => s ?? (c.semantic_models[0] ? { semantic_model: c.semantic_models[0].name, measures: c.semantic_models[0].measures.slice(0, 1).map((m) => m.key) } : null));
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const chosen = saved?.find((r) => r.id === savedId);
  useEffect(() => {
    if (source === "saved" && chosen && !widget) setChart(chosen.spec.chart ?? "table");
  }, [source, chosen, widget]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    const title_i18n = cleanTexts(titles);
    // what the tile shows until the dashboard is saved and reloaded
    const title = title_i18n[lang] ?? title_i18n[content.defaultLanguage] ?? Object.values(title_i18n)[0] ?? "";
    if (source === "saved") {
      if (!chosen) return;
      onDone({ title: title || chosen.name, title_i18n, saved_report: { id: chosen.id, name: chosen.name }, spec: chosen.spec, chart, inline: false });
      onClose();
      return;
    }
    if (!spec) return;
    const clean = compact({ ...spec, chart });
    const check = await api<{ valid: boolean; errors: SpecError[] }>("/reports/validate", { method: "POST", body: clean });
    setErrors(check.errors);
    if (!check.valid) return;
    onDone({ title, title_i18n, saved_report: null, spec: clean, chart, inline: true });
    onClose();
  };

  return (
    <Dialog title={widget ? t("dash.editWidget") : t("dash.addWidget")} onClose={onClose} wide>
      <form className="dialog-form" onSubmit={submit}>
        <div className="segmented" role="group">
          <button type="button" className={source === "saved" ? "active" : ""} onClick={() => setSource("saved")}>
            {t("dash.fromSaved")}
          </button>
          <button type="button" className={source === "inline" ? "active" : ""} onClick={() => setSource("inline")}>
            {t("dash.newReport")}
          </button>
        </div>
        {source === "saved" && saved && saved.length === 0 && <p className="muted">{t("saved.empty")}</p>}
        {source === "saved" && saved && saved.length > 0 && (
          <label className="stack">
            {t("dash.savedReport")}
            <select value={savedId ?? ""} onChange={(e) => setSavedId(Number(e.target.value))}>
              {saved.map((r) => (
                <option key={r.id} value={r.id}>
                  {isolate(r.name)}
                </option>
              ))}
            </select>
          </label>
        )}
        {source === "inline" && catalog && spec && (
          <div className="widget-builder">
            <Builder catalog={catalog} spec={spec} onChange={setSpec} errors={errors} />
          </div>
        )}
        <TranslatedInput label={t("dash.widgetTitle")} value={titles} maxLength={200} onChange={setTitles} />
        {source === "saved" && chosen && <p className="muted small">{t("dash.titleFromReport", { name: chosen.name })}</p>}
        <div className="form-grid">
          <label className="stack">
            {t("dash.chart")}
            <select value={chart} onChange={(e) => setChart(e.target.value as ChartType)}>
              {CHARTS.map((c) => (
                <option key={c} value={c}>
                  {t(`chart.${c}` as MessageKey)}
                </option>
              ))}
            </select>
          </label>
        </div>
        {errors.length > 0 && <p className="error">{t("report.invalid")}</p>}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="submit" className="primary" disabled={source === "saved" ? !chosen : !spec}>
            {widget ? t("form.save") : t("dash.add")}
          </button>
          <button type="button" onClick={onClose}>
            {t("common.cancel")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
