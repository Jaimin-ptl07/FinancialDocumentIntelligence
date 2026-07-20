import type {
  AnalyticsView,
  DocumentListView,
  DocumentView,
  ExtractDocumentView,
  ExtractionSchema,
  StagesView,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** Every failure surfaces the method, path, status, and the backend's own
 *  `detail` string. An error an analyst can act on beats a stack trace. */
async function request<T>(
  path: string,
  init?: RequestInit & { method?: string },
): Promise<T> {
  const method = init?.method ?? "GET";
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* body was not JSON; the status text stands */
    }
    throw new Error(`${method} ${path} failed (${response.status}): ${detail}`);
  }
  return (await response.json()) as T;
}

export const listDocuments = (limit = 50, offset = 0) =>
  request<DocumentListView>(`/documents?limit=${limit}&offset=${offset}`);

export const getDocument = (docId: string) =>
  request<DocumentView>(`/documents/${docId}`);

export const getStages = (docId: string) =>
  request<StagesView>(`/documents/${docId}/stages`);

export const getArtifact = (docId: string, stage: string) =>
  request<unknown>(`/documents/${docId}/artifacts/${stage}`);

export const startStage = (docId: string, stage: string) =>
  request<{ doc_id: string; stage: string }>(`/documents/${docId}/${stage}`, {
    method: "POST",
  });

export const extractDocument = (docKey: string, module?: string) =>
  request<ExtractDocumentView>("/extract/document", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ doc_key: docKey, module: module ?? null }),
  });

export const getSchema = () => request<ExtractionSchema>("/schema");

export const putSchema = (schema: ExtractionSchema, baseVersion: string) =>
  request<ExtractionSchema>("/schema", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ base_version: baseVersion, schema }),
  });

export const getAnalytics = () => request<AnalyticsView>("/analytics");

export async function uploadDocument(
  file: File,
  docKey?: string,
): Promise<DocumentView> {
  const form = new FormData();
  form.append("file", file);
  if (docKey) form.append("doc_key", docKey);
  return request<DocumentView>("/documents", { method: "POST", body: form });
}

/** The PDF is proxied through the API rather than served from a presigned
 *  MinIO URL, so the browser only ever talks to one origin and MinIO needs no
 *  CORS configuration of its own. */
export const pdfUrl = (docId: string) => `${API_BASE}/documents/${docId}/file`;
