/** The browser half of T5's API jobs.
 *
 *  `/ask`, `/extract/document`, `/factsheet/document` and `/eval` no longer
 *  answer with their body: they answer 202 with a job id, the work runs on a
 *  worker, `GET /jobs/{id}` walks queued -> running -> succeeded | failed, and
 *  `GET /jobs/{id}/result` serves the body the route used to return inline --
 *  re-validated server-side through the same view, so the shapes in types.ts
 *  are unchanged. Instant refusals (503/404/409, and 429 once quotas land)
 *  still come back from the submit itself, as an ApiError. */

import { request } from "./api";

export type JobKind = "ask" | "extract" | "factsheet" | "eval";
export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export interface JobAccepted {
  job_id: string;
  correlation_id: string;
  status: JobStatus;
}

export interface JobView {
  job_id: string;
  status: JobStatus;
  kind: JobKind | null;
  correlation_id: string | null;
  error: string | null;
}

/** What a failed job leaves for the analyst to report. */
export type JobFailure = Pick<JobView, "error" | "correlation_id">;

export const isTerminal = (status: JobStatus | undefined) =>
  status === "succeeded" || status === "failed";

const send = <T>(method: string, path: string, body?: unknown) =>
  request<T>(
    path,
    body === undefined
      ? { method }
      : {
          method,
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
  );

export const getJob = (jobId: string) => send<JobView>("GET", `/jobs/${jobId}`);

export const getJobResult = <T>(jobId: string) =>
  send<T>("GET", `/jobs/${jobId}/result`);

// ---------------------------------------------------------------- submitters

export interface AskRequest {
  question: string;
  max_rounds?: number | null;
}

export const submitAsk = (body: AskRequest) =>
  send<JobAccepted>("POST", "/ask", body);

/** Result: ExtractDocumentView. */
export const submitExtract = (docKey: string, module?: string, force?: boolean) =>
  send<JobAccepted>("POST", "/extract/document", {
    doc_key: docKey,
    module: module ?? null,
    force: force ?? false,
  });

/** Result: the JSON Fact bundle, or -- `format: "xbrl"` -- the instance
 *  document as a string. The format is fixed at submit. */
export const submitFactsheet = (
  docKey: string,
  options: { format?: "json" | "xbrl"; force?: boolean } = {},
) =>
  send<JobAccepted>(
    "POST",
    `/factsheet/document?format=${options.format ?? "json"}`,
    { doc_key: docKey, module: null, force: options.force ?? false },
  );

export interface EvalRequest {
  candidate: string;
  dataset: string;
  dataset_version: number;
  metric?: string | null;
}

/** Result: `{run_id, aggregate}`. */
export const submitEval = (body: EvalRequest) =>
  send<JobAccepted>("POST", "/eval", body);

// ------------------------------------------------------- surviving navigation

/** A running job outlives the screen that started it. Its id is remembered
 *  per *slot* -- a flow plus whatever it was about, e.g. `extract:<doc_key>` --
 *  so leaving the page, or reloading it, picks the same job back up instead of
 *  starting (and paying for) another.
 *
 *  localStorage, not the access token's in-memory home: a job id is not a
 *  credential -- the jobs endpoints are owner-scoped, so another Invitee on
 *  the same browser reads a 404 for it, and the hook forgets it. Where
 *  storage is unavailable (private mode, blocked site data) ids live in
 *  memory: they survive navigation, not a reload. */
const storageKey = (slot: string) => `fdi.job.${slot}`;
const inMemory = new Map<string, string>();
const listeners = new Set<() => void>();

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function notify() {
  listeners.forEach((listener) => listener());
}

export function rememberJob(slot: string, jobId: string) {
  try {
    const store = storage();
    if (store) store.setItem(storageKey(slot), jobId);
    else inMemory.set(slot, jobId);
  } catch {
    inMemory.set(slot, jobId); // quota, or a store that refuses writes
  }
  notify();
}

export function recallJob(slot: string): string | null {
  try {
    const store = storage();
    if (store) return store.getItem(storageKey(slot)) ?? inMemory.get(slot) ?? null;
  } catch {
    /* fall through to memory */
  }
  return inMemory.get(slot) ?? null;
}

export function forgetJob(slot: string) {
  inMemory.delete(slot);
  try {
    storage()?.removeItem(storageKey(slot));
  } catch {
    /* nothing to forget */
  }
  notify();
}

/** For useSyncExternalStore: this tab's writes, and other tabs' via the
 *  `storage` event. */
export function subscribeJobs(listener: () => void): () => void {
  listeners.add(listener);
  window.addEventListener("storage", listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", listener);
  };
}

/** What an analyst reports when a job fails: the reason and the correlation
 *  id that names every log line the worker wrote for it. */
export function describeFailure(job: JobFailure): string {
  const reason = job.error ?? "the job reported no detail";
  return job.correlation_id
    ? `${reason} (correlation id ${job.correlation_id})`
    : reason;
}
