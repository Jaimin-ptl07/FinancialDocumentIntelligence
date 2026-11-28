"""Fixture Reports for the Financial fact service tests (S5): each Report's
`ocr`, `fog_objects`, `fog_mapping` and `fog_reason` artifacts are generated
from one compact spec of printed face rows, so a test states what a Report
PRINTS and asserts what an Invitee gets."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.numbers import parse_amount
from fdi.platform.db.models import Document
from fdi.platform.db.repository import CompanyRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore

ST = "st:consolidated:profit_and_loss"
BS = "st:consolidated:balance_sheet"

# A tiny standard schema: enough Concepts and one derivation.
SCHEMA: dict[str, object] = {
    "version": "test-1",
    "modules": [
        {"module": "Profit and Loss", "retrieval_units": [{"fields": [
            {"tag": "RFOGRS", "field_name": "Gross Revenue from Operations"},
            {"tag": "TOTOTIN", "field_name": "Other income",
             "derivation": {"method": "sum", "inputs": ["INTOI", "DIVOI"]}},
            {"tag": "INTOI", "field_name": "Interest income"},
            {"tag": "DIVOI", "field_name": "Dividend income"},
            {"tag": "TOTRNOTINR", "field_name": "Total income"},
            {"tag": "PATR", "field_name": "Profit (loss) After Tax"},
            {"tag": "PBTR", "field_name": "Profit (loss) before tax"},
            {"tag": "EOI", "field_name": "Exceptional items"},
        ]}]},
        {"module": "Assets", "retrieval_units": [{"fields": [
            {"tag": "TOTCAR", "field_name": "Total Current Assets"},
        ]}]},
        {"module": "Liabilities", "retrieval_units": [{"fields": [
            {"tag": "TOTCLR", "field_name": "Total Current Liabilities"},
        ]}]},
    ],
}


@dataclass(frozen=True)
class Row:
    label: str
    tag: str | None
    current: str
    prior: str = ""


@dataclass
class ReportSpec:
    doc_key: str
    fiscal_year: str
    period_end: date
    rows: Sequence[Row]
    version: int = 1
    balance_sheet: Sequence[Row] = ()
    not_answered: Sequence[str] = ()
    processed: bool = True
    failed_checks: Sequence[str] = ()


@dataclass
class Seeded:
    company_id: str
    docs: dict[str, str] = field(default_factory=dict)   # label -> doc_id


def _header(end: date) -> list[str]:
    prev = end.replace(year=end.year - 1)
    return ["", "Note", f"Year ended {end:%B} {end.day}, {end.year}",
            f"Year ended {prev:%B} {prev.day}, {prev.year}"]


def _artifacts(doc_id: str, spec: ReportSpec) -> dict[str, Mapping[str, object]]:
    lead = "Consolidated Statement of Profit and Loss\n(₹ crore)\n"
    tables: list[dict[str, object]] = []
    content = lead
    items: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    statements: list[dict[str, object]] = []
    for index, (parent, statement, rows) in enumerate(
        ((ST, "profit_and_loss", spec.rows), (BS, "balance_sheet", spec.balance_sheet))
    ):
        if not rows:
            continue
        offset = len(content)
        grid = [_header(spec.period_end)] + [[r.label, "", r.current, r.prior] for r in rows]
        tables.append({
            "spans": [{"offset": offset, "length": 30}],
            "cells": [{"row_index": ri, "column_index": ci, "content": text}
                      for ri, row in enumerate(grid) for ci, text in enumerate(row)],
        })
        content += "x" * 30 + "\nBalance Sheet\n(₹ crore)\n"
        statements.append({"id": parent, "statement": statement, "basis": "consolidated",
                           "page": 4 + index, "table_index": len(tables) - 1})
        for i, r in enumerate(rows):
            li = f"li:{parent}:r{i}"
            items.append({
                "id": li, "parent": parent, "origin": "face", "label": r.label,
                "caption": "", "ref": "", "current": parse_amount(r.current),
                "prior": parse_amount(r.prior), "page": 4 + index,
                "table_index": len(tables) - 1, "row_index": i, "source_row": i + 1,
                "kind": "currency",
            })
            if r.tag:
                edges.append({"type": "MAPS_TO", "source": li, "target": r.tag})
    proof = [{"line_item": e["source"], "method": "lexical", "score": 1.0} for e in edges]
    tags = [e["target"] for e in edges]
    checks = [{"rule_id": f"check:{t}", "kind": "reconciliation", "severity": "error",
               "passed": t not in spec.failed_checks, "tags": [t], "expected": "",
               "actual": 1.0, "delta": 0.0} for t in tags]
    facts = {e["target"]: next(i["current"] for i in items if i["id"] == e["source"])
             for e in edges}
    return {
        "ocr": {"content": content + "end", "tables": tables},
        "fog_objects": {"schema_version": 6, "doc_id": doc_id, "bases": {"consolidated": {
            "statements": statements, "notes": [], "line_items": items, "edges": []}}},
        "fog_mapping": {"bases": {"consolidated": {"edges": edges, "proof": proof}}},
        "fog_reason": {"schema_version": 4, "doc_id": doc_id, "bases": {"consolidated": {
            "facts": {t: v for t, v in facts.items() if v is not None}, "checks": checks,
            "confidence": {}, "coverage": {"not_answered": list(spec.not_answered)},
        }}},
    }


async def put_artifact(s: AsyncSession, store: ArtifactStore, doc_id: str, stage: str,
                       body: Mapping[str, object]) -> None:
    repo = RegistryRepository(s)
    run_id = str(uuid.uuid4())
    key = f"test/{doc_id}/{stage}/{uuid.uuid4().hex}.json"
    await store.put_json(key, body)
    await repo.start_run(run_id, doc_id, stage, 1, uuid.uuid4().hex, None)
    await repo.finish_run(run_id, uuid.uuid4().hex, key)


async def seed_company(
    s: AsyncSession, store: ArtifactStore, specs: Mapping[str, ReportSpec], *,
    name: str = "ACME",
) -> Seeded:
    tag = uuid.uuid4().hex[:8]
    company, _ = await CompanyRepository(s).get_or_create_by_cin(
        cin=f"L{tag.upper():0<20}"[:21], name=f"{name} {tag}")
    seeded = Seeded(company.id)
    for label, spec in specs.items():
        doc_id = str(uuid.uuid4())
        s.add(Document(id=doc_id, doc_key=f"{spec.doc_key}-{tag}", version=spec.version,
                       company_id=company.id, fiscal_year=spec.fiscal_year,
                       period_end=spec.period_end, link_status="linked"))
        await s.flush()
        seeded.docs[label] = doc_id
        if spec.processed:
            for stage, body in _artifacts(doc_id, spec).items():
                await put_artifact(s, store, doc_id, stage, body)
    await s.commit()
    return seeded


def acme_specs() -> dict[str, ReportSpec]:
    """FY2025-26 (two versions), FY2024-25, and FY2022-23 filed unprocessed.

    FY2025-26's comparative column RESTATES FY2024-25's revenue (2,55,000 vs
    the 2,55,324 FY2024-25 printed) and agrees on profit after tax."""
    v2_rows = [
        Row("Revenue from operations", "RFOGRS", "2,67,021", "2,55,000"),
        Row("Other income", "TOTOTIN", "4,402", "3,962"),
        Row("Profit for the year", "PATR", "49,454", "48,797"),
        Row("Exceptional items", "EOI", "-", "-"),
    ]
    return {
        "2026v1": ReportSpec("acme-2026", "FY2025-26", date(2026, 3, 31), version=1, rows=[
            Row("Revenue from operations", "RFOGRS", "2,60,000", "2,55,324")]),
        "2026": ReportSpec("acme-2026", "FY2025-26", date(2026, 3, 31), version=2,
                           rows=v2_rows, not_answered=["PBTR"],
                           balance_sheet=[Row("Total current assets", "TOTCAR", "1,35,705",
                                              "1,23,011"),
                                          Row("Total current liabilities", "TOTCLR",
                                              "60,914", "53,001")]),
        "2025": ReportSpec("acme-2025", "FY2024-25", date(2025, 3, 31), rows=[
            Row("Revenue from operations", "RFOGRS", "2,55,324", "2,40,893"),
            Row("Other income", "TOTOTIN", "3,962", "3,500"),
            Row("Profit for the year", "PATR", "48,797", "46,099"),
        ], failed_checks=["PATR"]),
        "2023": ReportSpec("acme-2023", "FY2022-23", date(2023, 3, 31), rows=[],
                           processed=False),
    }
