import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import { submitAsk, submitEval, submitExtract, submitFactsheet } from "./jobs";
import { useJob } from "./useJob";

const API = "http://localhost:8000";

function wrapperWith(client: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

const freshClient = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false } } });

/** The server's side of one job: POST answers 202 queued, then each
 *  GET /jobs/{id} walks one step along `statuses`, staying on the last. */
function serveJob(
  statuses: Array<{ status: string; error?: string | null }>,
  result: Record<string, unknown> = { doc_key: "ar.pdf", doc_id: "doc-1", module: null, field_count: 0, fields: [] },
) {
  const seen = { submits: 0, polls: 0 };
  server.use(
    http.post(`${API}/extract/document`, () => {
      seen.submits += 1;
      return HttpResponse.json(
        { job_id: "job-1", correlation_id: "corr-1", status: "queued" },
        { status: 202 },
      );
    }),
    http.get(`${API}/jobs/job-1`, () => {
      const step = statuses[Math.min(seen.polls, statuses.length - 1)];
      seen.polls += 1;
      return HttpResponse.json({
        job_id: "job-1",
        kind: "extract",
        correlation_id: "corr-1",
        status: step.status,
        error: step.error ?? null,
      });
    }),
    http.get(`${API}/jobs/job-1/result`, () => HttpResponse.json(result)),
  );
  return seen;
}

const extractJob = (slot: string | null = "extract:ar.pdf") => ({
  slot,
  submit: (force: boolean) => submitExtract("ar.pdf", undefined, force),
  interval: 10,
});

beforeEach(() => window.localStorage.clear());

describe("useJob", () => {
  it("reflects the queued job the moment the submit is accepted", async () => {
    serveJob([{ status: "queued" }]);
    const { result } = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });

    act(() => result.current.start(false));

    await waitFor(() => expect(result.current.phase).toBe("queued"));
    expect(result.current.busy).toBe(true);
  });

  it("polls through running to succeeded and hands back the route's body", async () => {
    const body = {
      doc_key: "ar.pdf",
      doc_id: "doc-1",
      module: null,
      field_count: 1,
      fields: [{ field_name: "Total assets", value: 10, confidence: 0.9, source: {} }],
    };
    serveJob([{ status: "queued" }, { status: "running" }, { status: "succeeded" }], body);
    const { result } = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });

    act(() => result.current.start(false));

    await waitFor(() => expect(result.current.result).toEqual(body));
    expect(result.current.phase).toBe("succeeded");
    expect(result.current.busy).toBe(false);
  });

  it("surfaces a failed job's error with the correlation id to report it by", async () => {
    serveJob([{ status: "running" }, { status: "failed", error: "Azure OpenAI timed out" }]);
    const { result } = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });

    act(() => result.current.start(false));

    await waitFor(() => expect(result.current.phase).toBe("failed"));
    expect(result.current.failure).toEqual({
      error: "Azure OpenAI timed out",
      correlation_id: "corr-1",
    });
    expect(result.current.result).toBeUndefined();
    expect(result.current.busy).toBe(false);
  });

  it("surfaces a refused submit without inventing a job", async () => {
    server.use(
      http.post(`${API}/extract/document`, () =>
        HttpResponse.json(
          { detail: "this document's pipeline has not finished indexing yet" },
          { status: 409 },
        ),
      ),
    );
    const { result } = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });

    act(() => result.current.start(false));

    await waitFor(() => expect(result.current.problem).toMatch(/not finished indexing/));
    expect(result.current.phase).toBe("idle");
  });

  it("picks a running job back up after the screen is left and reopened", async () => {
    const seen = serveJob([{ status: "running" }]);
    const first = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });
    act(() => first.result.current.start(false));
    await waitFor(() => expect(first.result.current.phase).toBe("running"));
    first.unmount();

    // A fresh query cache: the reopened screen knows only what survived it.
    const second = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });

    await waitFor(() => expect(second.result.current.phase).toBe("running"));
    expect(seen.submits).toBe(1);
  });

  it("keeps each subject's job to itself", async () => {
    serveJob([{ status: "running" }]);
    const first = renderHook(() => useJob(extractJob("extract:ar.pdf")), {
      wrapper: wrapperWith(freshClient()),
    });
    act(() => first.result.current.start(false));
    await waitFor(() => expect(first.result.current.phase).toBe("running"));
    first.unmount();

    const other = renderHook(() => useJob(extractJob("extract:other.pdf")), {
      wrapper: wrapperWith(freshClient()),
    });

    await waitFor(() => expect(other.result.current.restored).toBe(true));
    expect(other.result.current.phase).toBe("idle");
  });

  it("forgets a remembered job the server no longer knows", async () => {
    window.localStorage.setItem("fdi.job.extract:ar.pdf", "job-gone");
    server.use(
      http.get(`${API}/jobs/job-gone`, () =>
        HttpResponse.json({ detail: "job job-gone not found" }, { status: 404 }),
      ),
    );
    const { result } = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });

    await waitFor(() =>
      expect(window.localStorage.getItem("fdi.job.extract:ar.pdf")).toBeNull(),
    );
    expect(result.current.phase).toBe("idle");
    expect(result.current.problem).toBeNull();
  });

  it.each([
    {
      kind: "ask",
      route: "/ask",
      submit: () => submitAsk({ question: "What were FY25 total assets?" }),
      body: { question: "What were FY25 total assets?" },
    },
    {
      kind: "eval",
      route: "/eval",
      submit: () => submitEval({ candidate: "baseline", dataset: "tcs", dataset_version: 2 }),
      body: { candidate: "baseline", dataset: "tcs", dataset_version: 2 },
    },
  ])("follows an $kind job from its submit to its result", async ({ route, submit, body }) => {
    const sent: unknown[] = [];
    server.use(
      http.post(`${API}${route}`, async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(
          { job_id: "job-2", correlation_id: "corr-2", status: "queued" },
          { status: 202 },
        );
      }),
      http.get(`${API}/jobs/job-2`, () =>
        HttpResponse.json({ job_id: "job-2", status: "succeeded", correlation_id: "corr-2" }),
      ),
      http.get(`${API}/jobs/job-2/result`, () => HttpResponse.json({ answer: "done" })),
    );
    const { result } = renderHook(() => useJob({ slot: "s", submit, interval: 10 }), {
      wrapper: wrapperWith(freshClient()),
    });

    act(() => result.current.start());

    await waitFor(() => expect(result.current.result).toEqual({ answer: "done" }));
    expect(sent).toEqual([body]);
  });

  it("hands back an XBRL factsheet as the instance document's text", async () => {
    const xml = '<?xml version="1.0"?><xbrli:xbrl/>';
    let format: string | null = null;
    server.use(
      http.post(`${API}/factsheet/document`, ({ request }) => {
        format = new URL(request.url).searchParams.get("format");
        return HttpResponse.json(
          { job_id: "job-3", correlation_id: "corr-3", status: "queued" },
          { status: 202 },
        );
      }),
      http.get(`${API}/jobs/job-3`, () =>
        HttpResponse.json({ job_id: "job-3", status: "succeeded", correlation_id: "corr-3" }),
      ),
      http.get(`${API}/jobs/job-3/result`, () =>
        new HttpResponse(xml, { headers: { "content-type": "application/xml" } }),
      ),
    );
    const { result } = renderHook(
      () =>
        useJob<string>({
          slot: "factsheet:ar.pdf:xbrl",
          submit: () => submitFactsheet("ar.pdf", { format: "xbrl" }),
          interval: 10,
        }),
      { wrapper: wrapperWith(freshClient()) },
    );

    act(() => result.current.start());

    await waitFor(() => expect(result.current.result).toBe(xml));
    expect(format).toBe("xbrl");
  });

  it("keeps showing the last reading while a forced re-read runs", async () => {
    const first = { doc_key: "ar.pdf", fields: ["old"] };
    serveJob([{ status: "succeeded" }], first);
    const { result } = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });
    act(() => result.current.start(false));
    await waitFor(() => expect(result.current.result).toEqual(first));

    server.use(
      http.post(`${API}/extract/document`, () =>
        HttpResponse.json(
          { job_id: "job-9", correlation_id: "corr-9", status: "queued" },
          { status: 202 },
        ),
      ),
      http.get(`${API}/jobs/job-9`, () =>
        HttpResponse.json({ job_id: "job-9", status: "running", correlation_id: "corr-9" }),
      ),
    );
    act(() => result.current.start(true));

    await waitFor(() => expect(result.current.phase).toBe("running"));
    expect(result.current.result).toEqual(first);
  });

  it("reports a refused resubmit over the previous job's failure", async () => {
    serveJob([{ status: "failed", error: "Azure OpenAI timed out" }]);
    const { result } = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });
    act(() => result.current.start(false));
    await waitFor(() => expect(result.current.phase).toBe("failed"));

    server.use(
      http.post(`${API}/extract/document`, () =>
        HttpResponse.json({ detail: "daily quota reached" }, { status: 429 }),
      ),
    );
    act(() => result.current.start(false));

    await waitFor(() => expect(result.current.problem).toMatch(/daily quota reached/));
    expect(result.current.failure).toBeNull();
  });

  it("states a failed job's error and correlation id as the problem", async () => {
    serveJob([{ status: "failed", error: "Azure OpenAI timed out" }]);
    const { result } = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });
    act(() => result.current.start(false));

    await waitFor(() =>
      expect(result.current.problem).toBe("Azure OpenAI timed out (correlation id corr-1)"),
    );
  });

  it("does not stay busy when the remembered job cannot be checked", async () => {
    window.localStorage.setItem("fdi.job.extract:ar.pdf", "job-1");
    server.use(
      http.get(`${API}/jobs/job-1`, () =>
        HttpResponse.json({ detail: "database unavailable" }, { status: 503 }),
      ),
    );
    const { result } = renderHook(() => useJob(extractJob()), {
      wrapper: wrapperWith(freshClient()),
    });

    await waitFor(() => expect(result.current.problem).toMatch(/database unavailable/));
    expect(result.current.busy).toBe(false);
  });
});
