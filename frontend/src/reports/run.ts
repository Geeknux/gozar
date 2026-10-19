/** Running a report: POST /reports/run, and when it becomes a background job (202),
 * poll it until it finishes. Shared by the Reports page and the Ask panel. */
import { api } from "../api";
import type { Job, Result, Spec } from "./model";

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const POLL_MS = [500, 1000, 1000, 2000, 2000, 3000]; // then every 3 s

export class JobCancelled extends Error {}

type Options = {
  bypassCache?: boolean;
  /** false once the caller no longer wants this run (the spec changed): stop polling */
  isCurrent: () => boolean;
  onJob?: (job: Job) => void;
};

/** The result (and the job that produced it, if any); ``null`` when superseded. */
export async function runSpec(spec: Spec, { bypassCache, isCurrent, onJob }: Options): Promise<{ result: Result; jobId?: string } | null> {
  const params = bypassCache ? { cache: "false" } : undefined;
  const res = await api<Result | { job: Job }>("/reports/run", { method: "POST", body: spec, params });
  if (!isCurrent()) return null;
  if (!("job" in res)) return { result: res };
  let state = res.job;
  onJob?.(state);
  for (let i = 0; state.status === "queued" || state.status === "running"; i++) {
    await wait(POLL_MS[Math.min(i, POLL_MS.length - 1)]);
    if (!isCurrent()) return null;
    state = await api<Job>(`/reports/jobs/${state.id}`);
    if (!isCurrent()) return null;
    onJob?.(state);
  }
  if (state.status === "cancelled") throw new JobCancelled();
  // done → the result; failed → the same error a synchronous run would raise
  const final = await api<Result>(`/reports/jobs/${state.id}/result`);
  return isCurrent() ? { result: final, jobId: state.id } : null;
}
