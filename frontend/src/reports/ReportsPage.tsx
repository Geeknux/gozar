import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, api } from "../api";
import { useI18n } from "../i18n";
import { type SavedReport, getSaved } from "../sharing";
import Builder from "./Builder";
import SavedActions from "./SavedActions";
import {
  type CatalogModel,
  type ChartType,
  compact,
  dataKey,
  type Filter,
  type Job,
  type Result,
  type Spec,
  type SpecError,
  specFromParam,
} from "./model";
import AskBox from "./AskBox";
import Results from "./Results";
import { JobCancelled, runSpec } from "./run";

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
  const [result, setResult] = useState<{ result: Result; spec: Spec; jobId?: string } | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [runError, setRunError] = useState("");
  const [running, setRunning] = useState(false);
  const [interpretation, setInterpretation] = useState("");
  const [copied, setCopied] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [saved, setSaved] = useState<SavedReport | null>(null);
  const [savedError, setSavedError] = useState("");
  const navigate = useNavigate();
  const seq = useRef(0);
  const bypassCache = useRef(false); // set by "Refresh": the next run skips the result cache

  useEffect(() => {
    api<{ semantic_models: CatalogModel[] }>("/reports/catalog")
      .then((c) => setCatalog(c.semantic_models))
      .catch((e: Error) => setLoadError(e.message));
  }, []);

  const savedId = params.get("saved");
  useEffect(() => {
    setSavedError("");
    if (!savedId) {
      setSaved(null);
      return;
    }
    getSaved(savedId)
      .then(setSaved)
      .catch((e: Error) => {
        setSaved(null);
        setSavedError(e.message);
      });
  }, [savedId]);

  const rawSpec = params.get("spec") ?? (saved && String(saved.id) === savedId ? JSON.stringify(saved.spec) : null);
  const spec = useMemo(() => {
    const parsed = specFromParam(rawSpec);
    if (parsed && catalog?.some((m) => m.name === parsed.semantic_model)) return parsed;
    const first = catalog?.[0];
    return first ? { semantic_model: first.name, measures: first.measures.slice(0, 1).map((m) => m.key) } : null;
  }, [rawSpec, catalog]);

  const setSpec = useCallback(
    (next: Spec) =>
      setParams((current) => {
        const out: Record<string, string> = { spec: JSON.stringify(compact(next)) };
        const id = current.get("saved");
        if (id) out.saved = id;
        return out;
      }, { replace: true }),
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
    const current = () => id === seq.current;
    const timer = setTimeout(async () => {
      setRunError("");
      setJob(null);
      if (!clean.measures.length) {
        setErrors([]);
        setResult(null);
        setInterpretation("");
        return;
      }
      try {
        const check = await api<{ valid: boolean; errors: SpecError[]; interpretation?: string }>("/reports/validate", {
          method: "POST",
          body: clean,
        });
        if (id !== seq.current) return;
        setErrors(remap(check.errors, filterIndex));
        if (!check.valid) {
          setInterpretation("");
          return;
        }
        setInterpretation(check.interpretation ?? "");
        setRunning(true);
        const bypass = bypassCache.current;
        bypassCache.current = false;
        const done = await runSpec(clean, { bypassCache: bypass, isCurrent: current, onJob: setJob });
        if (done) {
          setJob(null);
          setResult({ ...done, spec: clean });
        }
      } catch (err) {
        if (id !== seq.current) return;
        setJob(null);
        const specErrors = err instanceof ApiError ? (err.data as { errors?: unknown }).errors : undefined;
        if (err instanceof JobCancelled) setRunError(t("job.cancelled"));
        else if (Array.isArray(specErrors)) setErrors(remap(specErrors as SpecError[], filterIndex));
        else setRunError(err instanceof Error ? err.message : String(err));
      } finally {
        if (current()) setRunning(false);
      }
    }, 350);
    return () => clearTimeout(timer);
    // ``key`` captures everything in ``clean`` that affects the data
  }, [key, refresh]);

  if (loadError) return <p className="error">{loadError}</p>;
  if (!catalog || (savedId && !saved && !savedError)) return <p className="muted">{t("app.loading")}</p>;
  if (!catalog.length || !spec) return <p className="empty">{t("report.noModels")}</p>;

  const chart: ChartType = spec.chart ?? "table";
  const cancelJob = async () => {
    if (!job) return;
    seq.current++; // stop polling
    setRunning(false);
    try {
      setJob(await api<Job>(`/reports/jobs/${job.id}/cancel`, { method: "POST" }));
    } catch (err) {
      setRunError(err instanceof Error ? err.message : String(err));
    }
  };
  const share = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      window.prompt(t("report.share"), window.location.href);
    }
  };

  const openSaved = (report: SavedReport | null) =>
    navigate(report ? `/reports?saved=${report.id}` : "/reports", { replace: !report });

  return (
    <div className="reports-page">
      <div className="page-head">
        <div>
          <h1>
            {saved ? <bdi>{saved.name}</bdi> : t("report.title")}
            {saved && clean && dataKey(clean) !== dataKey(saved.spec) && <small className="muted"> · {t("saved.modified")}</small>}
          </h1>
          {saved && (
            <p className="muted small">
              {saved.description && <bdi>{saved.description} · </bdi>}
              {saved.is_owner ? t("saved.mine") : t("saved.sharedBy", { name: saved.owner.full_name || saved.owner.username })}
              {" · "}
              <Link to="/reports">{t("saved.newReport")}</Link>
            </p>
          )}
        </div>
        <div className="results-actions">
          {running && !job && <span className="muted">{t("report.running")}</span>}
          <button
            type="button"
            title={t("report.refreshHint")}
            onClick={() => {
              bypassCache.current = true;
              setRefresh((n) => n + 1);
            }}
          >
            {t("report.run")}
          </button>
          <button type="button" onClick={share}>
            {copied ? t("report.copied") : t("report.share")}
          </button>
          {clean && <SavedActions saved={saved} spec={clean} onSaved={(r) => (r.id === saved?.id ? setSaved(r) : openSaved(r))} onDeleted={() => openSaved(null)} />}
          <Link to="/reports/saved" className="button">
            {t("nav.savedReports")}
          </Link>
        </div>
      </div>
      {savedError && <p className="error">{savedError}</p>}
      <AskBox current={spec} onSpec={(next) => setSpec(next)} />
      <div className="reports-layout">
        <Builder catalog={catalog} spec={spec} onChange={setSpec} errors={errors} />
        <div className="reports-main">
          {!spec.measures.length && <p className="notice">{t("report.pickMeasure")}</p>}
          {errors.length > 0 && <p className="error">{t("report.invalid")}</p>}
          {runError && <p className="error">{runError}</p>}
          {interpretation && errors.length === 0 && (
            <p className="interpretation" dir="auto">
              <span className="muted">{t("ask.showing")}</span> {interpretation}
            </p>
          )}
          {job && (
            <div className="notice job-status" role="status">
              <span>
                {job.status === "cancelled"
                  ? t("job.cancelled")
                  : t(job.status === "running" ? "job.running" : "job.queued")}
              </span>
              {(job.status === "queued" || job.status === "running") && (
                <button type="button" className="button small" onClick={cancelJob}>
                  {t("job.cancel")}
                </button>
              )}
            </div>
          )}
          {result && (
            <Results
              result={result.result}
              spec={result.spec}
              jobId={result.jobId}
              chart={chart}
              onChart={(c) => setSpec({ ...spec, chart: c })}
            />
          )}
        </div>
      </div>
    </div>
  );
}
