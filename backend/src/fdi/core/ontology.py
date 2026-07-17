import re
from collections.abc import Mapping
from enum import StrEnum


class CanonicalConcept(StrEnum):
    REVENUE = "REVENUE"
    COST_OF_GOODS_SOLD = "COST_OF_GOODS_SOLD"
    GROSS_PROFIT = "GROSS_PROFIT"
    OPERATING_EXPENSE = "OPERATING_EXPENSE"
    NET_PROFIT = "NET_PROFIT"
    TOTAL_ASSETS = "TOTAL_ASSETS"
    TOTAL_LIABILITIES = "TOTAL_LIABILITIES"
    EQUITY = "EQUITY"
    CASH = "CASH"
    UNKNOWN = "UNKNOWN"


# Indian-annual-report starter set. Keys are ALREADY in normalized form
# (lowercase, no punctuation, single-spaced) -- see _normalize_label.
_SYNONYMS: dict[str, CanonicalConcept] = {
    "revenue": CanonicalConcept.REVENUE,
    "sales": CanonicalConcept.REVENUE,
    "turnover": CanonicalConcept.REVENUE,
    "revenue from operations": CanonicalConcept.REVENUE,
    "income from operations": CanonicalConcept.REVENUE,
    "total revenue": CanonicalConcept.REVENUE,
    "cost of goods sold": CanonicalConcept.COST_OF_GOODS_SOLD,
    "cogs": CanonicalConcept.COST_OF_GOODS_SOLD,
    "cost of materials consumed": CanonicalConcept.COST_OF_GOODS_SOLD,
    "gross profit": CanonicalConcept.GROSS_PROFIT,
    "operating expenses": CanonicalConcept.OPERATING_EXPENSE,
    "operating expense": CanonicalConcept.OPERATING_EXPENSE,
    "total expenses": CanonicalConcept.OPERATING_EXPENSE,
    "net profit": CanonicalConcept.NET_PROFIT,
    "profit after tax": CanonicalConcept.NET_PROFIT,
    "pat": CanonicalConcept.NET_PROFIT,
    "profit for the year": CanonicalConcept.NET_PROFIT,
    "net income": CanonicalConcept.NET_PROFIT,
    "total assets": CanonicalConcept.TOTAL_ASSETS,
    "total liabilities": CanonicalConcept.TOTAL_LIABILITIES,
    "equity": CanonicalConcept.EQUITY,
    "total equity": CanonicalConcept.EQUITY,
    "shareholders equity": CanonicalConcept.EQUITY,
    "shareholders funds": CanonicalConcept.EQUITY,
    "cash": CanonicalConcept.CASH,
    "cash and cash equivalents": CanonicalConcept.CASH,
    "cash and bank balances": CanonicalConcept.CASH,
}


_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _normalize_label(label: str) -> str:
    lowered = label.lower()
    stripped_punct = _PUNCT_RE.sub("", lowered)
    return _WS_RE.sub(" ", stripped_punct).strip()


def map_label(label: str) -> CanonicalConcept:
    """Normalize `label` and look it up in the synonym dictionary.

    Returns CanonicalConcept.UNKNOWN for anything not recognized -- an
    expected, non-error outcome, not a failure mode.
    """
    normalized = _normalize_label(label)
    return _SYNONYMS.get(normalized, CanonicalConcept.UNKNOWN)


def map_document(ast: Mapping[str, object]) -> dict[str, object]:
    """Map every section heading and every paragraph in `ast` through
    map_label, keeping only the non-UNKNOWN results.

    SIMPLIFICATION (documented, spec §2.6 / §6): a paragraph is mapped as a
    WHOLE string. A real OCR line item like "Revenue  12,345" will not match
    the dictionary (the trailing number breaks the exact lookup) -- only
    headings and paragraphs that are themselves bare labels map today.
    Numeric-value extraction + real line-item parsing is a follow-on.
    """
    mappings: list[dict[str, object]] = []
    sections_raw = ast.get("sections")
    if isinstance(sections_raw, list):
        for section in sections_raw:
            if not isinstance(section, dict):
                continue
            heading = section.get("heading")
            if isinstance(heading, str):
                concept = map_label(heading)
                if concept is not CanonicalConcept.UNKNOWN:
                    mappings.append({"label": heading, "concept": concept.value})
            paragraphs_raw = section.get("paragraphs")
            if isinstance(paragraphs_raw, list):
                for paragraph in paragraphs_raw:
                    if not isinstance(paragraph, str):
                        continue
                    concept = map_label(paragraph)
                    if concept is not CanonicalConcept.UNKNOWN:
                        mappings.append({"label": paragraph, "concept": concept.value})
    return {"schema_version": 1, "mappings": mappings}
