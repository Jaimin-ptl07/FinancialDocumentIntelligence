"use client";

import { useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type JobView = {
  job_id: string;
  status: string;
  artifact_key: string | null;
  artifact_url: string | null;
  error: string | null;
};

export default function Home() {
  const [job, setJob] = useState<JobView | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      const accepted = await fetch(`${API}/ping-job`, { method: "POST" });
      const { job_id } = await accepted.json();

      for (let i = 0; i < 60; i++) {
        const view: JobView = await (await fetch(`${API}/jobs/${job_id}`)).json();
        setJob(view);
        if (view.status === "done" || view.status === "failed") return;
        await new Promise((r) => setTimeout(r, 1000));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-2xl p-8 font-mono">
      <h1 className="text-2xl font-bold">Financial Document Intelligence</h1>
      <p className="mt-1 text-sm opacity-60">SP-1 walking skeleton</p>

      <button
        onClick={submit}
        disabled={busy}
        className="mt-6 rounded bg-black px-4 py-2 text-white disabled:opacity-40 dark:bg-white dark:text-black"
      >
        {busy ? "Running…" : "Submit ping job"}
      </button>

      {job && (
        <pre className="mt-6 overflow-x-auto rounded border p-4 text-sm">
          {JSON.stringify(job, null, 2)}
        </pre>
      )}
    </main>
  );
}
