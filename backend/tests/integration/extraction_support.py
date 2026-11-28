"""Shared fixtures for the S3 extraction tests: indexed Reports through the
real stage chain (fake OCR / semantic / embedding / vector store -- no Azure
anywhere), and a fake model that answers whichever Fields a prompt asks for
and records them, so a test can assert WHICH Fields reached the model."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping

from fdi.core.extractor import ExtractResult
from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.basis import BasisStage
from fdi.pipeline.stages.chunks import ChunksStage
from fdi.pipeline.stages.embedding import EmbeddingStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.index import IndexStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.pipeline.stages.structure import StructureStage
from fdi.pipeline.vectorstore import FakeVectorStore

_FIELD_PATH = re.compile(r'field_path="([^"]+)"')

CHAIN = ("ocr", "normalize", "ast", "semantic", "structure", "graph", "provenance",
         "basis", "chunks", "embedding", "index")


class AnsweringClient:
    """Answers every field_path in a batched prompt: `values[path]` when
    given, else 1.0. `fail(paths)` -> True makes that call raise (a model
    outage). `calls` records the Field paths each call asked for."""

    model_id = "fake-answering"

    def __init__(
        self,
        values: Mapping[str, object] | None = None,
        *,
        fail: Callable[[list[str]], bool] | None = None,
        confidence: float = 0.9,
    ) -> None:
        self.values = dict(values or {})
        self.fail = fail
        self.confidence = confidence
        self.calls: list[list[str]] = []

    @property
    def fields_asked(self) -> list[str]:
        return [p for call in self.calls for p in call]

    def extract(self, *, system: str, user: str) -> ExtractResult:
        paths = _FIELD_PATH.findall(user)
        self.calls.append(paths)
        if self.fail is not None and self.fail(paths):
            raise RuntimeError("model unavailable")
        return ExtractResult(data={
            p: {"value": self.values.get(p, 1.0), "confidence": self.confidence,
                "source": {"page": 1, "snippet": "Total revenue 1,000",
                           "evidence_ids": []}}
            for p in paths
        })


async def indexed_report(
    session_factory, store, *, embedder: FakeEmbeddingClient,
    vector_store: FakeVectorStore, doc_key: str | None = None,
) -> str:
    """One Report through the whole chain up to `index`: a statement-like
    table and a narrative paragraph, embedded into the shared fakes."""
    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), ChunksStage(), StructureStage(), BasisStage(),
                  EmbeddingStage(), IndexStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    ocr = FakeOcrClient({
        "content": "ACME LTD",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "sectionHeading", "content": "Statement of Profit and Loss",
             "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
            {"content": f"ACME Ltd reports its results {marker}.",
             "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}]},
        ],
        "tables": [{
            "rowCount": 3, "columnCount": 2, "spans": [{"offset": 0, "length": 40}],
            "boundingRegions": [{"pageNumber": 1, "polygon": []}],
            "cells": [
                {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                 "content": "Particulars"},
                {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": "FY25"},
                {"rowIndex": 1, "columnIndex": 0, "content": "Revenue from operations"},
                {"rowIndex": 1, "columnIndex": 1, "content": "1,000"},
                {"rowIndex": 2, "columnIndex": 0, "content": "Total expenses"},
                {"rowIndex": 2, "columnIndex": 1, "content": "800"},
            ],
        }],
    })
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="r.pdf",
        doc_key=doc_key or f"acme-{marker}", content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    services = {
        "ocr": {"ocr": ocr},
        "semantic": {"semantic": FakeSemanticClient({"Statement of Profit and Loss": "MDNA"})},
        "embedding": {"embedder": embedder},
        "index": {"vector_store": vector_store},
    }
    for name in CHAIN:
        await run_stage(
            doc_id=doc.id, stage_name=name, run_id=str(uuid.uuid4()), correlation_id="c1",
            session_factory=session_factory, store=store, registry=reg, resolver=resolver,
            services=services.get(name, {}),
        )
    return doc.id
