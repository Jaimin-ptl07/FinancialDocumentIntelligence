"""Issuer identification: which Company issued a Report, and by which CIN.

A listed Indian company must print its Corporate Identity Number (CIN) in its
annual report, so the CIN is the Company's identity (ADR-0003). The hard part
is that Reports also print OTHER companies' CINs -- subsidiaries in the
Directors' report, a subsidiary's secretarial audit, the registrar's own CIN
-- and the issuer's CIN is often rarer and later than those. Measured on the
corpus: RIL prints its own CIN once (p34) and once more on the back cover
(p147) while its subsidiaries' CINs appear five times; TCS's first appears on
p104.

So candidates are ranked by CONTEXT, never by how often or how early a CIN
appears:

* the company name printed nearest before the CIN is the issuer's name;
* a "CIN"/"Corporate Identity Number" label sits right before it;
* a registered-office block is near it;
* subsidiary / amalgamation / "hereinafter called the Company" wording near it
  counts against.

A CIN scores its BEST occurrence; occurrences are never summed, which would
reintroduce frequency through the back door.

The issuer's NAME is the one company name, among those printed beside a CIN,
that the Report mentions most. That is a frequency over names, deliberately:
a Report is about its issuer, so the issuer's name dominates its own text
(RIL: "Reliance Industries Limited" 40 mentions vs. 7 for Reliance Retail
Ventures), while the CIN is a one-line legal formality that need not.

Pure, no I/O; total -- never raises, whatever shape `ast`/`provenance` are.
"""

import re
from collections import Counter
from collections.abc import Mapping
from datetime import date, timedelta

SCHEMA_VERSION = 2

# 21 characters: listing status, industry code, state, year, ownership, number.
# OCR sometimes splits the groups with a space, so whitespace is tolerated
# between groups and removed on normalisation.
_CIN_RE = re.compile(
    r"\b([LU])\s?(\d{5})\s?([A-Z]{2})\s?(\d{4})\s?([A-Z]{3})\s?(\d{6})\b"
)
_LABEL_RE = re.compile(
    r"(?:\bCIN\b|corporate\s+identit(?:y|ification)\s+(?:number|no))[\s.:\-–]*"
    r"(?:no\.?|number)?[\s.:\-–]*$",
    re.IGNORECASE,
)
_SUFFIX_RE = re.compile(r"\b(?:Limited|LIMITED|Ltd|LTD)\b\.?")
_OFFICE_RE = re.compile(r"registered\s+office", re.IGNORECASE)
_AGAINST_RE = re.compile(
    r"subsidiar|associate\s+compan|amalgamat|hereinafter|ceased\s+to\s+be",
    re.IGNORECASE,
)
# Words that precede a company name on a line without being part of it.
_LEADING_FILLER = frozenset(
    {"to", "the", "members", "m/s", "m/s.", "messrs", "messrs.", "company", "information"}
)

_BEFORE = 240
_AFTER = 120
# How much of the paragraph around a CIN is kept as its evidence.
_EVIDENCE_BEFORE = 160
_EVIDENCE_AFTER = 80
_NAME_FIT = 3
_NAME_MISMATCH = -2
_LABEL = 1
_OFFICE = 1
_AGAINST = -3
# The winner must be backed by the issuer's name and clear the runner-up by a
# whole signal; anything closer is a guess an Invitee should settle.
_MIN_WINNING_SCORE = 3
_MIN_MARGIN = 2


def _normalize_name(name: str) -> str:
    lowered = re.sub(r"\s+", " ", name.strip().lower())
    lowered = re.sub(r"\bltd\b\.?", "limited", lowered)
    return lowered


def _attached_name(before: str) -> str | None:
    """The company name printed nearest before a CIN: the run of capitalised
    words ending in Limited/Ltd closest to the end of `before`, with leading
    filler ("To, The Members", "M/s") dropped."""
    matches = list(_SUFFIX_RE.finditer(before))
    for suffix in reversed(matches):
        head = before[: suffix.start()]
        # A name never spans a line, comma, parenthesis or colon.
        head = re.split(r"[\n,(;:]", head)[-1]
        tokens = head.split()
        words: list[str] = []
        for token in reversed(tokens):
            if token[:1].isupper() or token[:1].isdigit() or token == "&":
                words.append(token)
            else:
                break
        words.reverse()
        while words and words[0].lower() in _LEADING_FILLER:
            words.pop(0)
        if words and any(w.lower() != "private" for w in words):
            return " ".join([*words, suffix.group().rstrip(".")])
    return None


def _node_pages(provenance: Mapping[str, object]) -> dict[str, int]:
    out: dict[str, int] = {}
    entries = provenance.get("node_provenance")
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        node_id = entry.get("node_id")
        page = entry.get("page")
        if isinstance(node_id, str) and isinstance(page, int) and not isinstance(page, bool):
            out[node_id] = page
    return out


def _blocks(ast: Mapping[str, object]) -> list[list[tuple[str, str]]]:
    """Each section as its (node_id, text) blocks: heading first, then paragraphs."""
    sections = ast.get("sections")
    if not isinstance(sections, list):
        return []
    out: list[list[tuple[str, str]]] = []
    for i, section in enumerate(sections):
        if not isinstance(section, Mapping):
            continue
        blocks: list[tuple[str, str]] = []
        heading = section.get("heading")
        if isinstance(heading, str):
            blocks.append((f"s{i}", heading))
        paragraphs = section.get("paragraphs")
        if isinstance(paragraphs, list):
            for j, para in enumerate(paragraphs):
                if isinstance(para, str):
                    blocks.append((f"s{i}p{j}", para))
        out.append(blocks)
    return out


def _occurrences(ast: Mapping[str, object], pages: Mapping[str, int]) -> list[dict[str, object]]:
    """Every CIN occurrence with its surrounding text. Context runs across the
    blocks of one section (a back cover prints the name, then exchange codes,
    then the CIN as separate lines), never across sections."""
    found: list[dict[str, object]] = []
    for blocks in _blocks(ast):
        joined = ""
        starts: list[tuple[int, str]] = []
        for node_id, text in blocks:
            starts.append((len(joined), node_id))
            joined += text + "\n"
        for match in _CIN_RE.finditer(joined):
            block_start, node_id = next(
                (start, n) for start, n in reversed(starts) if start <= match.start()
            )
            before = joined[max(0, match.start() - _BEFORE) : match.start()]
            after = joined[match.end() : match.end() + _AFTER]
            block_text = next(t for n, t in blocks if n == node_id)
            found.append({
                "cin": "".join(match.groups()),
                "node_id": node_id,
                "page": pages.get(node_id),
                "text": _passage(
                    block_text, match.start() - block_start, match.end() - block_start
                ),
                "before": before,
                "after": after,
                "name": _attached_name(before),
            })
    return found


def _passage(block: str, start: int, end: int) -> str:
    """The matched CIN with the words around it, cut from its paragraph at
    word boundaries -- evidence a person can read, not a page of prose."""
    lo = max(0, start - _EVIDENCE_BEFORE)
    hi = min(len(block), end + _EVIDENCE_AFTER)
    if lo > 0:
        lo = block.find(" ", lo) + 1 or lo
    if hi < len(block):
        cut = block.rfind(" ", end, hi)
        hi = cut if cut != -1 else hi
    return block[lo:hi].strip()


def _name_pattern(key: str) -> re.Pattern[str]:
    """A normalised name as whole words: "arc limited" must not match inside
    "monarc limited"."""
    return re.compile(r"(?<![\w&])" + re.escape(key) + r"(?!\w)")


def _is_issuer(name: str, issuer_key: str) -> bool:
    """Whether a name printed beside a CIN is the issuer's: the issuer's name
    ends it, allowing leading words the name extraction kept ("Company
    Information Silver Touch Technologies Limited")."""
    normalized = _normalize_name(name)
    return normalized == issuer_key or normalized.endswith(" " + issuer_key)


def _full_text(ast: Mapping[str, object]) -> str:
    return "\n".join(text for blocks in _blocks(ast) for _, text in blocks)


def _display_casing(normalized: str, text: str, fallback: str) -> str:
    """The registered name as the Report prints it: spelled out ("REMC
    Limited", not the more frequent "REMC Ltd."), and not in capitals
    ("REMC LIMITED" on a certificate) when a mixed-case form exists."""
    words = r"\s+".join(re.escape(w) for w in normalized.split())
    pattern = re.compile(words.replace("limited", r"(?:limited|ltd\.?)"), re.IGNORECASE)
    forms = Counter(m.group() for m in pattern.finditer(text))
    mixed = [
        (f.lower().endswith("limited"), n, f) for f, n in forms.items() if not f.isupper()
    ]
    if mixed:
        return max(mixed)[2]
    return fallback


def _score(occurrence: Mapping[str, object], issuer_key: str | None) -> int:
    score = 0
    name = occurrence["name"]
    if isinstance(name, str) and issuer_key is not None:
        score += _NAME_FIT if _is_issuer(name, issuer_key) else _NAME_MISMATCH
    before = str(occurrence["before"])
    after = str(occurrence["after"])
    if _LABEL_RE.search(before[-40:]):
        score += _LABEL
    near = before[-160:] + after[:80]
    if _OFFICE_RE.search(near):
        score += _OFFICE
    if _AGAINST_RE.search(near):
        score += _AGAINST
    return score


def _evidence(occurrence: Mapping[str, object]) -> dict[str, object]:
    return {
        "node_id": occurrence["node_id"],
        "page": occurrence["page"],
        "text": occurrence["text"],
    }


# --- Fiscal year -------------------------------------------------------------
#
# Indian Fiscal-year conventions live here and nowhere else. A Report's
# Fiscal year is read from two independent places:
#
# * the statements: "for the year ended 31st March 2026" -- the audited
#   period. Statements also print the comparative year ("... 31st March
#   2025"), and a Report never reports a period after its own, so the LATEST
#   year end printed is the Report's;
# * the cover: "Annual Report 2025-26" -- on the earliest page that prints it
#   (running headers repeat it; the cover is the first).
#
# They agree -> that Fiscal year, high confidence. They disagree -> no Fiscal
# year, low confidence, and a reason naming both: an Invitee settles it, the
# code never picks a side silently. Only one present -> that one.

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
            ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
            ("september", "sept", "sep"), ("october", "oct"), ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = r"(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_ORDINAL = r"(\d{1,2})(?:st|nd|rd|th)?"
# "31st March, 2026" | "March 31, 2026" | "31.03.2026" / "31-03-2026" / "31/03/2026".
_DATE = (
    rf"(?:{_ORDINAL}\s+{_MONTH},?\s+(\d{{4}})"
    rf"|{_MONTH}\s+{_ORDINAL},?\s+(\d{{4}})"
    r"|(\d{1,2})[./-](\d{1,2})[./-](\d{4}))"
)
# "year ended" / "period ended (on)" -- not "half year ended" (an interim
# period) and not "year ending" (a future one: outlook, not the audit).
_YEAR_ENDED_RE = re.compile(
    rf"(?<!half )(?<!half-)\b(?:financial\s+)?(?:year|period)\s+ended\s+(?:on\s+)?{_DATE}",
    re.IGNORECASE,
)
_COVER_RE = re.compile(
    r"\bannual\s+report\s+(?:FY\s*)?(\d{4})\s*[-–/]\s*(\d{4}|\d{2})\b", re.IGNORECASE
)


def _fiscal_label(start: date, end: date) -> str:
    """`FY2025-26` for April 2025 - March 2026; a calendar-year period is
    `FY2025`."""
    if start.year == end.year:
        return f"FY{end.year}"
    return f"FY{start.year}-{end.year % 100:02d}"


def _period_ending(end: date) -> tuple[date, date]:
    """The twelve months closing on `end`: 31 March 2026 -> 1 April 2025."""
    following = end + timedelta(days=1)
    return following.replace(year=following.year - 1), end


def _year_end(match: re.Match[str]) -> date | None:
    g = match.groups()
    try:
        if g[0] is not None:
            return date(int(g[2]), _MONTHS[g[1].lower()], int(g[0]))
        if g[3] is not None:
            return date(int(g[5]), _MONTHS[g[3].lower()], int(g[4]))
        return date(int(g[8]), int(g[7]), int(g[6]))
    except (ValueError, KeyError):
        return None


def _cover_period(match: re.Match[str]) -> tuple[date, date] | None:
    """"Annual Report 2025-26" -> the Indian Fiscal year April 2025 - March 2026."""
    first = int(match.group(1))
    second = match.group(2)
    last = int(second) if len(second) == 4 else (first // 100) * 100 + int(second)
    if last != first + 1:
        return None
    return date(first, 4, 1), date(last, 3, 31)


def _fy_candidate(
    source: str, period: tuple[date, date], node_id: str, page: int | None, text: str
) -> dict[str, object]:
    start, end = period
    return {
        "fiscal_year": _fiscal_label(start, end),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "source": source,
        "evidence": {"node_id": node_id, "page": page, "text": text},
    }


def _fiscal_year(ast: Mapping[str, object], pages: Mapping[str, int]) -> dict[str, object]:
    statements: dict[str, dict[str, object]] = {}
    covers: list[dict[str, object]] = []
    for blocks in _blocks(ast):
        for node_id, text in blocks:
            page = pages.get(node_id)
            for match in _YEAR_ENDED_RE.finditer(text):
                end = _year_end(match)
                if end is None:
                    continue
                candidate = _fy_candidate(
                    "statements", _period_ending(end), node_id, page,
                    _passage(text, match.start(), match.end()),
                )
                statements.setdefault(str(candidate["fiscal_year"]), candidate)
            for match in _COVER_RE.finditer(text):
                period = _cover_period(match)
                if period is not None:
                    covers.append(
                        _fy_candidate(
                            "cover", period, node_id, page,
                            _passage(text, match.start(), match.end()),
                        )
                    )

    by_end = sorted(statements.values(), key=lambda c: str(c["period_end"]), reverse=True)
    # The Report's own title is the one it prints most (cover, running
    # headers); a one-off citation of an older Report loses. Ties go to the
    # earliest page, evidence is the earliest printing.
    covers.sort(key=_page_order)
    printed = Counter(str(c["fiscal_year"]) for c in covers)
    cover = (
        min(covers, key=lambda c: (-printed[str(c["fiscal_year"])], _page_order(c)))
        if covers
        else None
    )
    stated = by_end[0] if by_end else None
    candidates = [*by_end, *([cover] if cover is not None else [])]

    chosen: dict[str, object] | None
    if stated is not None and cover is not None and stated["fiscal_year"] != cover["fiscal_year"]:
        chosen, confidence = None, "low"
        reason = (
            f"the cover says {cover['fiscal_year']} (page {_page_of(cover)}) but the "
            f"statements say {stated['fiscal_year']} (page {_page_of(stated)})"
        )
    elif stated is not None:
        chosen, confidence = stated, "high"
        reason = f"statements for the year ended {stated['period_end']} (page {_page_of(stated)})"
        if cover is not None:
            reason += " and the cover agree"
    elif cover is not None:
        chosen, confidence = cover, "high"
        reason = f"the cover names {cover['fiscal_year']} (page {_page_of(cover)})"
    else:
        chosen, confidence = None, "none"
        reason = "no Fiscal year is printed in the Report"

    return {
        "fiscal_year": chosen["fiscal_year"] if chosen is not None else None,
        "period_start": chosen["period_start"] if chosen is not None else None,
        "period_end": chosen["period_end"] if chosen is not None else None,
        "fiscal_year_confidence": confidence,
        "fiscal_year_reason": reason,
        "fiscal_year_candidates": candidates,
    }


def _page_of(candidate: Mapping[str, object]) -> object:
    evidence = candidate["evidence"]
    return evidence.get("page") if isinstance(evidence, Mapping) else None


def _page_order(candidate: Mapping[str, object]) -> int:
    page = _page_of(candidate)
    return page if isinstance(page, int) else 10**9


def _empty(reason: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "cin": None,
        "issuer_name": None,
        "confidence": "none",
        "reason": reason,
        "cin_candidates": [],
        "name_candidates": [],
    }


def identify_issuer(
    ast: Mapping[str, object], provenance: Mapping[str, object]
) -> dict[str, object]:
    """Identify the Report's issuer from its `ast` + `provenance` artifacts.

    Returns the chosen `cin` and `issuer_name` (or None), a `confidence`
    ("high" when one CIN clearly wins, "low" when CINs are present but none
    does, "none" when the Report prints no CIN), a human-readable `reason`,
    and every CIN and name candidate with its evidence (node id, page, the
    passage around the CIN) so a link is auditable like every other FDI fact. Candidates are
    ordered best first.

    Also the Report's Fiscal year: `fiscal_year` ("FY2025-26"), `period_start`
    / `period_end` (ISO dates) or all None, a `fiscal_year_confidence`
    ("high"; "low" when the cover and the statements disagree; "none" when
    no year is printed), a `fiscal_year_reason`, and the
    `fiscal_year_candidates` from the statements (newest first) and the cover,
    each with its evidence.
    """
    if not isinstance(ast, Mapping) or not isinstance(provenance, Mapping):
        return {**_empty("the Report has no readable text"), **_fiscal_year({}, {})}
    pages = _node_pages(provenance)
    return {**_identify_cin(ast, pages), **_fiscal_year(ast, pages)}


def _identify_cin(ast: Mapping[str, object], pages: Mapping[str, int]) -> dict[str, object]:
    occurrences = _occurrences(ast, pages)
    if not occurrences:
        return _empty("no CIN is printed anywhere in the Report")

    text = _full_text(ast)
    normalized_text = _normalize_name(text)
    mentions: dict[str, int] = {}
    printed: dict[str, str] = {}
    first_seen: dict[str, dict[str, object]] = {}
    for occ in occurrences:
        name = occ["name"]
        if isinstance(name, str):
            key = _normalize_name(name)
            mentions[key] = len(_name_pattern(key).findall(normalized_text))
            printed.setdefault(key, name)
            first_seen.setdefault(key, occ)
    issuer_key = max(mentions, key=lambda k: (mentions[k], -len(k))) if mentions else None

    best: dict[str, tuple[int, dict[str, object]]] = {}
    for occ in occurrences:
        score = _score(occ, issuer_key)
        cin = str(occ["cin"])
        if cin not in best or score > best[cin][0]:
            best[cin] = (score, occ)

    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
    candidates = [
        {
            "cin": cin,
            "score": score,
            "name": occ["name"],
            "evidence": _evidence(occ),
        }
        for cin, (score, occ) in ranked
    ]
    name_candidates = [
        {"name": printed[key], "mentions": count, "evidence": _evidence(first_seen[key])}
        for key, count in sorted(mentions.items(), key=lambda kv: kv[1], reverse=True)
    ]
    issuer_name = (
        _display_casing(issuer_key, text, printed[issuer_key]) if issuer_key is not None else None
    )

    top_score = ranked[0][1][0]
    runner_up = ranked[1][1][0] if len(ranked) > 1 else None
    if top_score < _MIN_WINNING_SCORE:
        confidence, cin_out = "low", None
        reason = "no CIN is printed beside the issuer's name"
    elif runner_up is not None and top_score - runner_up < _MIN_MARGIN:
        confidence, cin_out = "low", None
        reason = (
            f"CINs {ranked[0][0]} and {ranked[1][0]} are equally plausible as the issuer's"
        )
    else:
        confidence, cin_out = "high", ranked[0][0]
        reason = f"CIN printed beside the issuer's name on page {ranked[0][1][1]['page']}"

    return {
        "schema_version": SCHEMA_VERSION,
        "cin": cin_out,
        "issuer_name": issuer_name if cin_out is not None else None,
        "confidence": confidence,
        "reason": reason,
        "cin_candidates": candidates,
        "name_candidates": name_candidates,
    }
