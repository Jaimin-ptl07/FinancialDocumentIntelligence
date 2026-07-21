"use client";

import { use } from "react";
import { ReportViewer } from "@/components/viewer/ReportViewer";

/** The route shell. It exists only to unwrap the async `params` promise; the
 *  screen itself takes a plain `docId` so it can be rendered — and tested —
 *  without one. */
export default function ReportViewerPage({
  params,
}: {
  params: Promise<{ docId: string }>;
}) {
  const { docId } = use(params);
  return <ReportViewer docId={docId} />;
}
