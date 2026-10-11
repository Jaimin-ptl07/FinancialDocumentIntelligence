import type {
  AnalyticsView,
  CorpusCostView,
  DocumentCostView,
  DocumentListView,
  DocumentUsageEventsView,
  DocumentView,
  ExtractDocumentView,
  ExtractionSchema,
  SchemaValidationView,
  SchemaView,
  StagesView,
} from "./types";

import { API_BASE } from "./apiBase";
import { ApiError, authorizedFetch, detailOf } from "./auth";

export { API_BASE, ApiError };

/** Every failure surfaces the method, path, status, and the backend's own
 *  `detail` string. An error an analyst can act on beats a stack trace.
 *  Every call rides the session (lib/auth): bearer attached, one silent
 *  refresh on a 401. */
async function request<T>(
  path: string,
  init?: RequestInit & { method?: string },
): Promise<T> {
  const method = init?.method ?? "GET";
  const response = await authorizedFetch(path, init);
  if (!response.ok) {
    throw new ApiError(
      `${method} ${path} failed (${response.status}): ${await detailOf(response)}`,
      response.status,
    );
  }
  return (await response.json()) as T;
}

export const listDocuments = (limit = 50, offset = 0) =>
  request<DocumentListView>(`/documents?limit=${limit}&offset=${offset}`);

export const getDocument = (docId: string) =>
  request<DocumentView>(`/documents/${docId}`);

/** 204 No Content on success -- nothing to parse, so this bypasses
 *  `request<T>` (which always awaits `response.json()`) and does its own
 *  fetch + error handling instead. */
export async function deleteDocument(docId: string): Promise<void> {
  const response = await authorizedFetch(`/documents/${docId}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    throw new ApiError(
      `DELETE /documents/${docId} failed (${response.status}): ${await detailOf(response)}`,
      response.status,
    );
  }
}

export const getStages = (docId: string) =>
  request<StagesView>(`/documents/${docId}/stages`);

export const getDocumentCost = (docId: string) =>
  request<DocumentCostView>(`/documents/${docId}/cost`);

export const getDocumentCostEvents = (docId: string) =>
  request<DocumentUsageEventsView>(`/documents/${docId}/cost/events`);

export const getArtifact = (docId: string, stage: string) =>
  request<unknown>(`/documents/${docId}/artifacts/${stage}`);

export const startStage = (docId: string, stage: string) =>
  request<{ doc_id: string; stage: string }>(`/documents/${docId}/${stage}`, {
    method: "POST",
  });

export const extractDocument = (docKey: string, module?: string, force?: boolean) =>
  request<ExtractDocumentView>("/extract/document", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ doc_key: docKey, module: module ?? null, force: force ?? false }),
  });

export const getSchema = () => request<SchemaView>("/schema");

/** `base_version` must match the version currently stored or the write is
 *  refused 409 — two tabs editing the same schema must not clobber each other.
 *  The new version is stamped server-side by bumping the patch component, so
 *  nothing here chooses one. */
export const putSchema = (
  schemaDocument: ExtractionSchema,
  baseVersion: string,
) =>
  request<SchemaView>("/schema", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      base_version: baseVersion,
      schema_document: schemaDocument,
    }),
  });

/** Dry-runs the PUT gate without touching storage, so the editor can validate
 *  as the analyst types. */
export const validateSchema = (schemaDocument: ExtractionSchema) =>
  request<SchemaValidationView>("/schema/validate", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ schema_document: schemaDocument }),
  });

export const getAnalytics = () => request<AnalyticsView>("/analytics");

export const getCosting = () => request<CorpusCostView>("/costing");

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
