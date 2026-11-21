"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "@/lib/api";

/** Retry a call the SERVER might answer differently next time, and never one
 *  it has already answered definitively.
 *
 *  A 4xx is a settled answer. `GET /documents/{id}/artifacts/fog_reason`
 *  returns 404 when that stage has not run for that filing -- the normal state
 *  of any filing mid-ingest, and one no retry can change. Retrying it doubled
 *  every artifact request (measured: 25 identical 404s in one session for a
 *  filing whose FOG was never built) and delayed the "not built yet" message by
 *  a backoff, so the screen sat empty while waiting for an answer already
 *  given.
 *
 *  5xx and network failures keep their retry: those a server genuinely may
 *  answer differently a moment later. */
export function retryUnlessAnswered(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return false;
  }
  return failureCount < 1;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { staleTime: 5_000, retry: retryUnlessAnswered } },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
