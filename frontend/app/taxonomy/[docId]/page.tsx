"use client";

import { use } from "react";
import { TaxonomyViewer } from "@/components/taxonomy/TaxonomyViewer";

/** The route shell. It exists only to unwrap the async `params` promise; the
 *  screen itself takes a plain `docId` so it can be rendered — and tested —
 *  without one. Mirrors app/documents/[docId]/page.tsx. */
export default function TaxonomyDocumentPage({
  params,
}: {
  params: Promise<{ docId: string }>;
}) {
  const { docId } = use(params);
  return <TaxonomyViewer docId={docId} />;
}
