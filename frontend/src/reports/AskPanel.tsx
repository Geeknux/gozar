import { lazy, Suspense, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../api";
import { useI18n } from "../i18n";
import AskBox from "./AskBox";
import { compact, type ChartType, type Job, type Result, type Spec } from "./model";
import { JobCancelled, runSpec } from "./run";

// ECharts (inside Results) only loads once there is something to show.
const Results = lazy(() => import("./Results"));

/** The home page's Ask panel: question → interpretation → report and chart, with
 * follow-ups and a link to adjust the spec in the report builder. */
export default function AskPanel() {
  const { t } = useI18n();
  const [spec, setSpec] = useState<Spec | null>(null);
  const [result, setResult] = useState<{ result: Result; spec: Spec; jobId?: string } | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const seq = useRef(0);

  async function show(next: Spec) {
    const id = ++seq.current;
    const current = () => id === seq.current;
    setSpec(next);
    setError("");
    setRunning(true);
    try {
      const done = await runSpec(next, { isCurrent: current, onJob: setJob });
      if (done) setResult({ ...done, spec: next });
    } catch (err) {
      if (!current()) return;
      if (err instanceof JobCancelled) setError(t("job.cancelled"));
      else if (err instanceof ApiError) setError(err.message);
      else setError(String(err));
    } finally {
      if (current()) {
        setRunning(false);
        setJob(null);
      }
    }
  }

  const chart: ChartType = spec?.chart ?? "table";
  const builderLink = spec ? `/reports?spec=${encodeURIComponent(JSON.stringify(compact(spec)))}` : "";

  return (
    <div className="ask-panel">
      <AskBox current={spec} onSpec={(next) => void show(next)} autoFocus />
      {(running || job) && <p className="muted">{job ? t(job.status === "running" ? "job.running" : "job.queued") : t("report.running")}</p>}
      {error && <p className="error">{error}</p>}
      {result && spec && (
        <section className="card ask-result">
          <div className="page-head">
            <h2>{t("ask.result")}</h2>
            <Link to={builderLink} className="button">
              {t("ask.openInBuilder")}
            </Link>
          </div>
          <Suspense fallback={<p className="muted">{t("app.loading")}</p>}>
            <Results
              result={result.result}
              spec={result.spec}
              jobId={result.jobId}
              chart={chart}
              onChart={(c) => setSpec({ ...spec, chart: c })}
            />
          </Suspense>
        </section>
      )}
    </div>
  );
}
