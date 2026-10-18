import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ApiError, api } from "../api";
import { useI18n } from "../i18n";
import Builder from "./Builder";
import {
  type CatalogModel,
  type ChartType,
  compact,
  dataKey,
  type Filter,
  type Result,
  type Spec,
  type SpecError,
  specFromParam,
} from "./model";
import Results from "./Results";

const incomplete = (f: Filter) =>
  f.op !== "is_null" &&
  (f.value === "" ||
    f.value === null ||
    f.value === undefined ||
    (Array.isArray(f.value) && (f.value.length === 0 || f.value.some((v) => v === ""))));

/** Drop half-filled filters before sending; remember where the rest came from. */
function sanitize(spec: Spec): { spec: Spec; filterIndex: number[] } {
  const filterIndex: number[] = [];
  const filters = (spec.filters ?? []).filter((f, i) => !incomplete(f) && filterIndex.push(i));
  return { spec: compact({ ...spec, filters }), filterIndex };
}

const remap = (errors: SpecError[], filterIndex: number[]) =>
  errors.map((e) => ({ ...e, path: e.path.replace(/^filters\[(\d+)\]/, (_, i) => `filters[${filterIndex[Number(i)] ?? i}]`) }));

/** Reports: builder on one side, live results on the other; the spec lives in the URL. */
export default function ReportsPage() {
  const { t } = useI18n();
  const [params, setParams] = useSearchParams();
  const [catalog, setCatalog] = useState<CatalogModel[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [errors, setErrors] = useState<SpecError[]>([]);
  const [result, setResult] = useState<{ result: Result; spec: Spec } | null>(null);
  const [runError, setRunError] = useState("");
  const [running, setRunning] = useState(false);
  const [copied, setCopied] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const seq = useRef(0);

  useEffect(() => {
    api<{ semantic_models: CatalogModel[] }>("/reports/catalog")
      .then((c) => setCatalog(c.semantic_models))
      .catch((e: Error) => setLoadError(e.message));
  }, []);

  const rawSpec = params.get("spec");
  const spec = useMemo(() => {
    const parsed = specFromParam(rawSpec);
    if (parsed && catalog?.some((m) => m.name === parsed.semantic_model)) return parsed;
    const first = catalog?.[0];
    return first ? { semantic_model: first.name, measures: first.measures.slice(0, 1).map((m) => m.key) } : null;
  }, [rawSpec, catalog]);

  const setSpec = useCallback(
    (next: Spec) => setParams({ spec: JSON.stringify(compact(next)) }, { replace: true }),
    [setParams],
  );

  const { spec: clean, filterIndex } = useMemo(
    () => (spec ? sanitize(spec) : { spec: null, filterIndex: [] }),
    [spec],
  );
  const key = clean ? dataKey(clean) : "";

  // Validate, then run, whenever the data-relevant part of the spec changes.
  useEffect(() => {
    if (!clean) return;
    const id = ++seq.current;
    const timer = setTimeout(async () => {
      setRunError("");
      if (!clean.measures.length) {
        setErrors([]);
        setResult(null);
        return;
      }
      try {
        const check = await api<{ valid: boolean; errors: SpecError[] }>("/reports/validate", {
          method: "POST",
          body: clean,
        });
        if (id !== seq.current) return;
        setErrors(remap(check.errors, filterIndex));
        if (!check.valid) return;
        setRunning(true);
        const res = await api<Result>("/reports/run", { method: "POST", body: clean });
        if (id === seq.current) setResult({ result: res, spec: clean });
      } catch (err) {
        if (id !== seq.current) return;
        const specErrors = err instanceof ApiError ? (err.data as { errors?: unknown }).errors : undefined;
        if (Array.isArray(specErrors)) setErrors(remap(specErrors as SpecError[], filterIndex));
        else setRunError(err instanceof Error ? err.message : String(err));
      } finally {
        if (id === seq.current) setRunning(false);
      }
    }, 350);
    return () => clearTimeout(timer);
    // ``key`` captures everything in ``clean`` that affects the data
  }, [key, refresh]);

  if (loadError) return <p className="error">{loadError}</p>;
  if (!catalog) return <p className="muted">{t("app.loading")}</p>;
  if (!catalog.length || !spec) return <p className="empty">{t("report.noModels")}</p>;

  const chart: ChartType = spec.chart ?? "table";
  const share = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      window.prompt(t("report.share"), window.location.href);
    }
  };

  return (
    <div className="reports-page">
      <div className="page-head">
        <h1>{t("report.title")}</h1>
        <div className="results-actions">
          {running && <span className="muted">{t("report.running")}</span>}
          <button type="button" onClick={() => setRefresh((n) => n + 1)}>
            {t("report.run")}
          </button>
          <button type="button" onClick={share}>
            {copied ? t("report.copied") : t("report.share")}
          </button>
        </div>
      </div>
      <div className="reports-layout">
        <Builder catalog={catalog} spec={spec} onChange={setSpec} errors={errors} />
        <div className="reports-main">
          {!spec.measures.length && <p className="notice">{t("report.pickMeasure")}</p>}
          {errors.length > 0 && <p className="error">{t("report.invalid")}</p>}
          {runError && <p className="error">{runError}</p>}
          {result && (
            <Results
              result={result.result}
              spec={result.spec}
              chart={chart}
              onChart={(c) => setSpec({ ...spec, chart: c })}
            />
          )}
        </div>
      </div>
    </div>
  );
}
