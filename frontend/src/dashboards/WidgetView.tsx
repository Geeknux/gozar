import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../api";
import { isolate } from "../bidi";
import { useI18n } from "../i18n";
import { compact, dataKey, type Job, type Result, type Spec } from "../reports/model";
import Results from "../reports/Results";
import { JobCancelled, runSpec } from "../reports/run";
import type { FilterOption, ResolvedWidget } from "./api";

type Props = { widget: ResolvedWidget; options: FilterOption[]; refresh: number };

/** One tile: runs its effective spec through /reports/run (cache and background jobs
 * included) as soon as it mounts, so every widget loads in parallel. */
export default function WidgetView({ widget, options, refresh }: Props) {
  const { t } = useI18n();
  const [result, setResult] = useState<{ result: Result; spec: Spec; jobId?: string } | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const seq = useRef(0);
  const lastRefresh = useRef(refresh);
  const spec = widget.spec;
  const key = spec ? dataKey(spec) : "";

  useEffect(() => {
    if (!spec) return;
    const id = ++seq.current;
    const current = () => id === seq.current;
    setError("");
    const bypassCache = refresh !== lastRefresh.current; // "Refresh" skips the result cache
    lastRefresh.current = refresh;
    runSpec(spec, { bypassCache, isCurrent: current, onJob: setJob })
      .then((done) => {
        if (done) setResult({ ...done, spec });
      })
      .catch((err) => {
        if (!current()) return;
        if (err instanceof JobCancelled) setError(t("job.cancelled"));
        else {
          const errors = err instanceof ApiError ? (err.data as { errors?: { message: string }[] }).errors : undefined;
          setError(Array.isArray(errors) ? errors.map((e) => e.message).join(" ") : err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (current()) setJob(null);
      });
    // key captures everything about the spec that affects the data
  }, [key, refresh]);

  const href = spec
    ? `/reports?${widget.saved_report ? `saved=${widget.saved_report.id}&` : ""}spec=${encodeURIComponent(JSON.stringify(compact(spec)))}`
    : "";
  const head = (
    <header className="widget-head">
      <h3 dir="auto" title={isolate(widget.title)}>
        {widget.title || t("dash.untitled")}
      </h3>
      {href && (
        <Link className="muted small" to={href} title={t("dash.openReport")}>
          {t("dash.open")}
        </Link>
      )}
    </header>
  );
  const label = (k: string) => (k === "period" ? t("dash.period") : (options.find((o) => o.key === k)?.label ?? k));

  return (
    <section className="widget card">
      {!(spec && result) && head}
      {widget.ignored.length > 0 && (
        <p className="muted small widget-note">{t("dash.notFiltered", { filters: widget.ignored.map(label).join(t("common.listSeparator")) })}</p>
      )}
      {widget.problem && <p className="notice muted"><bdi>{widget.problem}</bdi></p>}
      {error && <p className="error">{error}</p>}
      {spec && !result && !error && <p className="muted">{job ? t(job.status === "running" ? "job.running" : "job.queued") : t("report.running")}</p>}
      {spec && result && <Results result={result.result} spec={result.spec} jobId={result.jobId} chart={widget.chart} compact head={head} title={widget.title} />}
    </section>
  );
}
