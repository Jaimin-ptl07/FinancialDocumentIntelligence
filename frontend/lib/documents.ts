import type { DocumentView } from "./types";

/** Whether a stored source PDF exists for this row.
 *
 *  Not every `document` row is a filing. `POST /ping-job` and the pipeline's
 *  own test paths create rows with no checksum, no filename, and no bytes —
 *  the backend answers `GET /documents/{id}/file` for those with
 *  `404 no source file for document ... (no checksum)`. Checksum is the
 *  signal because it is what the file is stored under. */
export function hasSourceFile(doc: DocumentView | null | undefined): boolean {
  return Boolean(doc?.checksum);
}

/** What to call a filing in a list or a title bar. Falls back through the
 *  names a row might actually carry, and says plainly when it carries none
 *  rather than presenting a bare UUID as if it were a document title. */
export function filingLabel(doc: DocumentView | null | undefined): string {
  if (!doc) return "Filing";
  return doc.filename ?? doc.doc_key ?? "Untitled record";
}
