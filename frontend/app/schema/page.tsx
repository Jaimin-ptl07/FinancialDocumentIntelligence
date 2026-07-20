"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  countFields,
  filterModules,
  ModuleTree,
  type FieldPath,
} from "@/components/schema/ModuleTree";
import { FieldEditor } from "@/components/schema/FieldEditor";
import { PageHeader } from "@/components/shell/PageHeader";
import { getSchema, putSchema } from "@/lib/api";
import type { ExtractionSchema, SchemaField } from "@/lib/types";

export default function SchemaScreen() {
  const queryClient = useQueryClient();
  /** The draft is the whole schema, held locally until saved. `null` means
   *  nothing has been touched — which is also what makes Save inert. */
  const [draft, setDraft] = useState<ExtractionSchema | null>(null);
  const [selected, setSelected] = useState<FieldPath | null>(null);
  const [query, setQuery] = useState("");

  const { data, error, isPending } = useQuery({
    queryKey: ["schema"],
    queryFn: getSchema,
  });

  // `GET /schema` wraps the document alongside its counts and provenance, so
  // the editable object is one level in.
  const schema = draft ?? data?.schema_document ?? null;
  const dirty = draft !== null;

  const save = useMutation({
    mutationFn: () => putSchema(schema!, data!.version),
    onSuccess: (saved) => {
      setDraft(null);
      queryClient.setQueryData(["schema"], saved);
    },
  });

  const field = useMemo<SchemaField | null>(() => {
    if (!schema || !selected) return null;
    return (
      schema.modules
        .find((m) => m.key === selected.moduleKey)
        ?.retrieval_units.find((u) => u.unit_id === selected.unitId)
        ?.fields.find((f) => f.field_name === selected.fieldName) ?? null
    );
  }, [schema, selected]);

  const total = schema ? countFields(schema.modules) : 0;
  const shown = schema ? countFields(filterModules(schema.modules, query)) : 0;

  const updateField = (next: SchemaField) => {
    if (!schema || !selected) return;
    setDraft({
      ...schema,
      modules: schema.modules.map((m) =>
        m.key !== selected.moduleKey
          ? m
          : {
              ...m,
              retrieval_units: m.retrieval_units.map((u) =>
                u.unit_id !== selected.unitId
                  ? u
                  : {
                      ...u,
                      fields: u.fields.map((f) =>
                        f.field_name === selected.fieldName ? next : f,
                      ),
                    },
              ),
            },
      ),
    });
  };

  return (
    <div className="flex flex-col" style={{ height: "100vh" }}>
      <PageHeader
        eyebrow="Configuration"
        title="Extraction schema"
        lede="What the system looks for in a filing, and how it is told to read it."
      >
        {data ? (
          <span
            className="data"
            style={{ fontSize: "var(--text-sm)", color: "var(--ink-soft)" }}
          >
            v{data.version}
            {data.source === "seed" ? " · as shipped" : " · edited"}
          </span>
        ) : null}
        <button
          type="button"
          className="btn btn-primary"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? "Saving" : "Save changes"}
        </button>
      </PageHeader>

      {error ? (
        <p
          role="alert"
          style={{ padding: "var(--sp-8)", color: "var(--ink)" }}
        >
          {(error as Error).message}
        </p>
      ) : isPending || !schema ? (
        <p className="eyebrow" style={{ padding: "var(--sp-8)" }}>
          Loading schema
        </p>
      ) : (
        <div className="flex flex-col md:flex-row flex-1 min-h-0">
          <div
            className="flex flex-col shrink-0 w-full md:w-80 max-h-[45vh] md:max-h-none"
            style={{
              background: "var(--leaf)",
              borderBottom: "var(--hairline)",
              borderRight: "var(--hairline)",
            }}
          >
            <div
              style={{
                padding: "var(--sp-3) var(--sp-4)",
                borderBottom: "1px solid var(--rule-soft)",
              }}
            >
              <label htmlFor="find" className="sr-only">
                Find a field
              </label>
              <input
                id="find"
                className="input"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Find a field"
              />
              <p
                role="status"
                className="eyebrow"
                style={{ marginTop: "var(--sp-2)" }}
              >
                <span className="data">{shown}</span> of{" "}
                <span className="data">{total}</span> fields
              </p>
            </div>

            <div className="flex-1 min-h-0 overflow-y-auto">
              <ModuleTree
                modules={schema.modules}
                query={query}
                selected={selected}
                onSelect={setSelected}
              />
            </div>
          </div>

          <div
            className="flex-1 min-w-0 overflow-y-auto"
            style={{ padding: "var(--sp-8)" }}
          >
            {field ? (
              <div
                className="sheet"
                style={{ padding: "var(--sp-6)", maxWidth: "56rem" }}
              >
                <FieldEditor field={field} onChange={updateField} />
              </div>
            ) : (
              <p style={{ color: "var(--ink-soft)" }}>
                Pick a field to edit how it is retrieved and read.
              </p>
            )}
            {save.error ? (
              <p
                role="alert"
                style={{
                  marginTop: "var(--sp-6)",
                  fontSize: "var(--text-sm)",
                  color: "var(--ink)",
                }}
              >
                {(save.error as Error).message}
              </p>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
