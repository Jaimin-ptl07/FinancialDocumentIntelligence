"use client";

import { use } from "react";
import { DocumentCosting } from "@/components/costing/DocumentCosting";

/** The route shell. It exists only to unwrap the async `params` promise; the
 *  screen itself takes a plain `docId` so it can be rendered — and tested —
 *  without one. Mirrors app/documents/[docId]/page.tsx's own shell. */
export default function DocumentCostingPage({
  params,
}: {
  params: Promise<{ docId: string }>;
}) {
  const { docId } = use(params);
  return <DocumentCosting docId={docId} />;
}
