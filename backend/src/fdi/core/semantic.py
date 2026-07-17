"""Section classification types. Pure -- no SDK import, no I/O.

`SectionType` is the closed 8-way label set the semantic stage coerces an LLM's
free-text response into. `build_snippet` truncates a section's paragraphs into
the bounded text sent to the classifier.
"""

from enum import StrEnum


class SectionType(StrEnum):
    BALANCE_SHEET = "BALANCE_SHEET"
    INCOME_STATEMENT = "INCOME_STATEMENT"
    CASH_FLOW = "CASH_FLOW"
    NOTES = "NOTES"
    MDNA = "MDNA"
    AUDITORS_REPORT = "AUDITORS_REPORT"
    DIRECTORS_REPORT = "DIRECTORS_REPORT"
    OTHER = "OTHER"


def build_snippet(paragraphs: list[str], max_chars: int = 2000) -> str:
    """Join paragraphs with newlines and truncate to `max_chars`."""
    return "\n".join(paragraphs)[:max_chars]
