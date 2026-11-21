"use client";

import { useCallback, useEffect, useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "./auth";
import {
  describeFailure,
  forgetJob,
  getJob,
  getJobResult,
  isTerminal,
  recallJob,
  rememberJob,
  subscribeJobs,
  type JobAccepted,
  type JobFailure,
  type JobStatus,
  type JobView,
} from "./jobs";

export type JobPhase = "idle" | "submitting" | JobStatus;

export interface UseJobOptions<Args extends unknown[]> {
  /** Which remembered job this screen is about (see `rememberJob`); null
   *  while the screen does not know yet, e.g. its filing is still loading. */
  slot: string | null;
  submit: (...args: Args) => Promise<JobAccepted>;
  /** Poll period in ms. The stage-run poll on /ingest uses the same 1s. */
  interval?: number;
}

export interface UseJob<T, Args extends unknown[]> {
  start: (...args: Args) => void;
  phase: JobPhase;
  /** True from the click until the job settles. */
  busy: boolean;
  /** The succeeded job's body -- what the route used to answer inline. While
   *  a newer job for the same slot runs (a forced re-read), or if it fails,
   *  the previous body stays: a re-read never blanks a good reading. */
  result: T | undefined;
  /** The current job failed, as reported by the worker. */
  failure: JobFailure | null;
  /** The one thing to tell the analyst, newest first: a refused submit
   *  (409, 429...), else the failed job with its correlation id, else a
   *  poll that could not reach the server. */
  problem: string | null;
  /** The remembered job for this slot (if any) is known, so `phase === "idle"`
   *  means there really is no job -- safe to auto-start one. */
  restored: boolean;
}

const noSubscription = () => () => {};

const jobKey = (jobId: string | null) => ["job", jobId] as const;

/** Submit an API job and follow it to its result, the way /ingest follows
 *  stage runs: poll until terminal, then stop on its own.
 *
 *  The job id is remembered per slot, so navigating away and back -- or a
 *  reload -- resumes the same job rather than losing it. A remembered id the
 *  server answers 404 for (expired, or another Invitee's) is forgotten. */
export function useJob<T, Args extends unknown[] = []>({
  slot,
  submit,
  interval = 1_000,
}: UseJobOptions<Args>): UseJob<T, Args> {
  const queryClient = useQueryClient();
  // The remembered id IS the state: one source of truth, shared by every
  // screen (and tab) about the same slot. The server snapshot is null --
  // there is no storage there -- and `mounted` keeps `restored` false
  // through hydration, which renders with that server snapshot.
  const jobId = useSyncExternalStore(
    subscribeJobs,
    () => (slot === null ? null : recallJob(slot)),
    () => null,
  );
  const mounted = useSyncExternalStore(noSubscription, () => true, () => false);

  const submission = useMutation({
    // The slot rides with the submit: if the subject changes while the POST
    // is out, the job is still filed under the one it was started for.
    mutationFn: ({ args }: { slot: string; args: Args }) => submit(...args),
    onSuccess: (accepted, { slot: startedFor }) => {
      // Seed the poll with the 202 itself, so "queued" shows immediately
      // rather than after the first GET.
      queryClient.setQueryData<JobView>(jobKey(accepted.job_id), {
        job_id: accepted.job_id,
        status: accepted.status,
        kind: null,
        correlation_id: accepted.correlation_id,
        error: null,
      });
      rememberJob(startedFor, accepted.job_id);
    },
  });

  const poll = useQuery({
    queryKey: jobKey(jobId),
    queryFn: () => getJob(jobId!),
    enabled: jobId !== null,
    // The seeded 202 must not count as fresh, or the first poll waits on
    // the provider's staleTime instead of the interval.
    staleTime: 0,
    refetchInterval: (query) => (isTerminal(query.state.data?.status) ? false : interval),
  });

  const status = poll.data?.status;
  const outcome = useQuery({
    queryKey: [...jobKey(jobId), "result", slot],
    queryFn: () => getJobResult<T>(jobId!),
    enabled: jobId !== null && status === "succeeded",
    // A succeeded job's result never changes.
    staleTime: Infinity,
    // Hold the previous job's body while this one is unsettled -- but only
    // for the same subject: another filing's figures are not a placeholder.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[3] === slot ? previous : undefined,
  });

  const lost = poll.error instanceof ApiError && poll.error.status === 404;
  useEffect(() => {
    if (lost && slot !== null && recallJob(slot) === jobId) forgetJob(slot);
  }, [lost, slot, jobId]);

  // A remembered job counts as restored once its status is known (or it is
  // known to be gone): until then "idle" would be a lie that invites a
  // second, duplicate submit.
  const restored =
    mounted && slot !== null && (jobId === null || status !== undefined || lost);

  const { mutate } = submission;
  // With no slot there is nowhere to remember the job, so nothing could
  // follow it: a screen that does not know its subject yet cannot start one.
  const start = useCallback(
    (...args: Args) => {
      if (slot !== null) mutate({ slot, args });
    },
    [mutate, slot],
  );

  const phase: JobPhase = submission.isPending
    ? "submitting"
    : jobId !== null && status
      ? status
      : "idle";

  // A new submit supersedes the old job's failure, refused or not.
  const failure: JobFailure | null =
    status === "failed" && poll.data && !submission.isPending && !submission.error
      ? { error: poll.data.error, correlation_id: poll.data.correlation_id }
      : null;
  const pollError = lost ? null : poll.error;

  return {
    start,
    phase,
    busy:
      // A remembered job whose status is not known yet -- unless asking
      // failed, which must not hold the button down until the server is back.
      (jobId !== null && status === undefined && !lost && !pollError) ||
      phase === "submitting" ||
      phase === "queued" ||
      phase === "running" ||
      (phase === "succeeded" && (outcome.isPending || outcome.isPlaceholderData)),
    result: outcome.data,
    failure,
    problem:
      submission.error?.message ??
      (failure ? describeFailure(failure) : null) ??
      pollError?.message ??
      outcome.error?.message ??
      null,
    restored,
  };
}
