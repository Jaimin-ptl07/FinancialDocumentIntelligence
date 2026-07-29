"use client";

import { useMemo, useState } from "react";
import type { TaxonomyTreeNode } from "@/lib/types";
import { formatAmount, formatConfidence, inkOpacity, isDualPeriod } from "@/lib/format";
import { RefMark } from "@/components/data/RefMark";

const INDENT_PX = 20;
// Nodes at this depth or deeper start collapsed -- a statement's tree is
// ~100 tags deep across up to 4 levels; showing everything at once buries
// the top-line figures a banking user actually starts from.
const DEFAULT_EXPAND_DEPTH = 2;

function allParentPaths(nodes: TaxonomyTreeNode[], out: Set<string>): void {
  for (const node of nodes) {
    if (node.children.length > 0) out.add(node.path);
    allParentPaths(node.children, out);
  }
}

function defaultCollapsed(nodes: TaxonomyTreeNode[], depth: number, out: Set<string>): void {
  for (const node of nodes) {
    if (node.children.length > 0 && depth >= DEFAULT_EXPAND_DEPTH) out.add(node.path);
    defaultCollapsed(node.children, depth + 1, out);
  }
}

function matchesQuery(node: TaxonomyTreeNode, needle: string): boolean {
  return (
    node.field_name.toLowerCase().includes(needle) ||
    (node.tag ?? "").toLowerCase().includes(needle)
  );
}

/** Same shape as ModuleTree's filterModules: a node survives if it matches
 *  itself or has a matching descendant, so the table shows the shape of the
 *  answer rather than a column of empty branches. */
function filterTree(nodes: TaxonomyTreeNode[], needle: string): TaxonomyTreeNode[] {
  if (!needle) return nodes;
  const out: TaxonomyTreeNode[] = [];
  for (const node of nodes) {
    const children = filterTree(node.children, needle);
    if (matchesQuery(node, needle) || children.length > 0) {
      out.push({ ...node, children });
    }
  }
  return out;
}

function numericCurrent(raw: unknown): number | null {
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  if (isDualPeriod(raw) && typeof raw.current === "number") return raw.current;
  return null;
}

function numericPrior(raw: unknown): number | null {
  if (isDualPeriod(raw) && typeof raw.prior === "number") return raw.prior;
  return null;
}

function deltaPercent(current: number | null, prior: number | null): string {
  if (current === null || prior === null || prior === 0) return "—";
  return `${(((current - prior) / Math.abs(prior)) * 100).toFixed(1)}%`;
}

const thStyle = {
  padding: "var(--sp-2) var(--sp-4)",
  fontSize: "var(--text-2xs)",
} as const;

export function StatementTable({
  tree,
  docId,
}: {
  tree: TaxonomyTreeNode[];
  docId: string;
}) {
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    defaultCollapsed(tree, 0, initial);
    return initial;
  });

  const needle = query.trim().toLowerCase();
  const visible = useMemo(() => filterTree(tree, needle), [tree, needle]);

  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const expandAll = () => setCollapsed(new Set());
  const collapseAll = () => {
    const all = new Set<string>();
    allParentPaths(tree, all);
    setCollapsed(all);
  };

  const rows: { node: TaxonomyTreeNode; depth: number }[] = [];
  const collect = (nodes: TaxonomyTreeNode[], depth: number) => {
    for (const node of nodes) {
      rows.push({ node, depth });
      if (node.children.length > 0 && !collapsed.has(node.path)) {
        collect(node.children, depth + 1);
      }
    }
  };
  collect(visible, 0);

  return (
    <div className="sheet-flat">
      <div
        className="flex items-center"
        style={{
          gap: "var(--sp-3)",
          padding: "var(--sp-3) var(--sp-4)",
          borderBottom: "var(--hairline)",
        }}
      >
        <input
          className="input"
          placeholder="Find a line item or tag"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ maxWidth: "24rem" }}
        />
        <button type="button" className="btn" onClick={expandAll}>
          Expand all
        </button>
        <button type="button" className="btn" onClick={collapseAll}>
          Collapse all
        </button>
      </div>

      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ borderBottom: "1px solid var(--rule)" }}>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "left" }}>Particulars</th>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "left" }}>Tag</th>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "right" }}>Current</th>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "right" }}>Prior</th>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "right" }}>Δ%</th>
            <th className="eyebrow" style={{ ...thStyle, textAlign: "right" }}>Confidence</th>
            <th className="eyebrow" style={thStyle}>
              <span className="sr-only">Evidence</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={7} style={{ padding: "var(--sp-6)", color: "var(--ink-soft)" }}>
                No line item answers to that name or tag.
              </td>
            </tr>
          ) : (
            rows.map(({ node, depth }) => (
              <StatementRow
                key={node.path}
                node={node}
                depth={depth}
                docId={docId}
                collapsed={collapsed.has(node.path)}
                onToggle={() => toggle(node.path)}
              />
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function StatementRow({
  node,
  depth,
  docId,
  collapsed,
  onToggle,
}: {
  node: TaxonomyTreeNode;
  depth: number;
  docId: string;
  collapsed: boolean;
  onToggle: () => void;
}) {
  const current = numericCurrent(node.value);
  const prior = numericPrior(node.value);
  const hasChildren = node.children.length > 0;
  const isDerived = node.origin === "derived";
  const isTagged = node.tag !== null;
  const missing = isTagged && node.value === null;
  const cell = { padding: "var(--sp-2) var(--sp-4)" } as const;

  return (
    <tr style={{ borderTop: "1px solid var(--rule-soft)" }}>
      <td style={{ ...cell, paddingLeft: `calc(var(--sp-4) + ${depth * INDENT_PX}px)` }}>
        <span className="flex items-baseline" style={{ gap: "var(--sp-2)" }}>
          {hasChildren ? (
            <button
              type="button"
              onClick={onToggle}
              aria-expanded={!collapsed}
              aria-label={`${collapsed ? "Expand" : "Collapse"} ${node.field_name}`}
              className="data"
              style={{ width: "1.2em", flexShrink: 0, color: "var(--ink-faint)", cursor: "pointer" }}
            >
              {collapsed ? "▸" : "▾"}
            </button>
          ) : (
            <span aria-hidden="true" style={{ width: "1.2em", flexShrink: 0 }} />
          )}
          <span style={{ fontWeight: hasChildren ? 600 : 400, color: "var(--ink-soft)" }}>
            {node.field_name}
          </span>
          {isDerived ? (
            <span
              aria-hidden="true"
              className="data"
              title={`Derived (${node.field_name})`}
              style={{ fontSize: "var(--text-2xs)", color: "var(--ink-faint)" }}
            >
              Σ
            </span>
          ) : null}
        </span>
      </td>
      <td className="data" style={{ ...cell, fontSize: "var(--text-xs)", color: "var(--ink-faint)" }}>
        {node.tag ?? ""}
      </td>
      <td
        className="data"
        style={{
          ...cell,
          textAlign: "right",
          opacity: node.confidence !== null ? inkOpacity(node.confidence) : 1,
        }}
      >
        {!isTagged ? "" : missing ? "—" : formatAmount(node.value)}
      </td>
      <td className="data" style={{ ...cell, textAlign: "right", color: "var(--ink-faint)" }}>
        {!isTagged ? "" : prior !== null ? formatAmount(prior) : "—"}
      </td>
      <td className="data" style={{ ...cell, textAlign: "right", color: "var(--ink-faint)" }}>
        {!isTagged ? "" : deltaPercent(current, prior)}
      </td>
      <td className="data" style={{ ...cell, textAlign: "right", fontSize: "var(--text-2xs)" }}>
        {!isTagged || node.confidence === null
          ? ""
          : node.band === "failed"
            ? "!"
            : node.band === "high"
              ? "✓"
              : formatConfidence(node.confidence)}
      </td>
      <td style={cell}>
        {node.origin === "extracted" && node.page !== null && node.tag !== null ? (
          <RefMark
            page={node.page}
            label={`Open evidence for ${node.field_name} on page ${node.page}`}
            onClick={() =>
              window.open(`/documents/${docId}?tag=${encodeURIComponent(node.tag!)}`, "_blank")
            }
          />
        ) : null}
      </td>
    </tr>
  );
}
