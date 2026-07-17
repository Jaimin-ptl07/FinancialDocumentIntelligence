"use client";
import { useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Doc = { id: string; checksum: string | null; version: number | null };
type Stage = { name: string; status: string; error: string | null };
type Ast = { title: string | null; sections: { heading: string | null; paragraphs: string[] }[] };

export default function Home() {
  const [doc, setDoc] = useState<Doc | null>(null);
  const [stages, setStages] = useState<Stage[]>([]);
  const [ast, setAst] = useState<Ast | null>(null);
  const [busy, setBusy] = useState(false);

  async function upload(file: File) {
    const form = new FormData();
    form.append("file", file);
    const r = await fetch(`${API}/documents`, { method: "POST", body: form });
    setDoc(await r.json()); setStages([]); setAst(null);
  }

  async function runOcr() {
    if (!doc) return;
    setBusy(true);
    try {
      await fetch(`${API}/documents/${doc.id}/ocr`, { method: "POST" });
      for (let i = 0; i < 90; i++) {
        const s: { stages: Stage[] } = await (await fetch(`${API}/documents/${doc.id}/stages`)).json();
        setStages(s.stages);
        const astStage = s.stages.find((x) => x.name === "ast");
        if (astStage?.status === "done") {
          setAst(await (await fetch(`${API}/documents/${doc.id}/artifacts/ast`)).json());
          return;
        }
        if (s.stages.some((x) => x.status === "failed")) return;
        await new Promise((r) => setTimeout(r, 1000));
      }
    } finally { setBusy(false); }
  }

  return (
    <main className="mx-auto max-w-2xl p-8 font-mono">
      <h1 className="text-2xl font-bold">Financial Document Intelligence</h1>
      <p className="mt-1 text-sm opacity-60">Upload a PDF → OCR → normalize → AST</p>

      <input type="file" accept="application/pdf" className="mt-6 block"
             onChange={(e) => { const f = e.target.files?.[0]; if (f) void upload(f); }} />

      {doc && (
        <div className="mt-4 text-sm">
          <div>document <code>{doc.id}</code> · v{doc.version}</div>
          <button onClick={() => void runOcr()} disabled={busy}
                  className="mt-3 rounded bg-black px-4 py-2 text-white disabled:opacity-40 dark:bg-white dark:text-black">
            {busy ? "Running…" : "Run OCR pipeline"}
          </button>
        </div>
      )}

      {stages.length > 0 && (
        <ul className="mt-6 text-sm">
          {stages.map((s) => (
            <li key={s.name}>{s.name}: <b>{s.status}</b>{s.error ? ` — ${s.error}` : ""}</li>
          ))}
        </ul>
      )}

      {ast && (
        <section className="mt-6">
          <h2 className="text-lg font-bold">{ast.title ?? "(untitled)"}</h2>
          {ast.sections.map((sec, i) => (
            <div key={i} className="mt-3">
              <h3 className="font-semibold">{sec.heading ?? "(preamble)"}</h3>
              {sec.paragraphs.map((p, j) => <p key={j} className="mt-1 text-sm">{p}</p>)}
            </div>
          ))}
        </section>
      )}
    </main>
  );
}
