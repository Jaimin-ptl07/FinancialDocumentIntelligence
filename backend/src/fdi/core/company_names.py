"""How a Company is found by the names people actually use (S2.4).

Pure and total: no I/O, never raises. The company registry searches and
resolves through these functions, so every spelling rule lives here:

* a name's *key* ignores case, punctuation, spacing and the usual legal-form
  abbreviations -- "Reliance Industries Ltd." and "RELIANCE INDUSTRIES
  LIMITED" have one key;
* a search query matches a run of whole words of a name, alias or
  identifier, in order -- "RIL" finds the alias "RIL", never "April";
* a typed identifier (NSE symbol, BSE code, ISIN, alias) has one canonical
  form, or is malformed and rejected (None).

Only the CIN is ever detected from a Report (`fdi.core.issuer`); every other
identifier is entered by an Invitee, which is why they are validated here.
"""

import re
import unicodedata

IDENTIFIER_KINDS = ("nse_symbol", "bse_code", "isin", "alias")

# Abbreviations that name the same legal form or connective as their long
# spelling. Applied per word after punctuation is gone.
_WORD_EQUIVALENTS = {
    "ltd": "limited",
    "pvt": "private",
    "co": "company",
    "corp": "corporation",
    "&": "and",
}
# Dots and apostrophes join (R.I.L. -> ril, Moody's -> moodys); every other
# non-alphanumeric separates words. `&` is kept as its own word.
_JOINERS_RE = re.compile(r"[.'’]")
_SEPARATORS_RE = re.compile(r"[^0-9a-z&]+")

_CIN_RE = re.compile(r"[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}")
_NSE_RE = re.compile(r"[A-Z0-9][A-Z0-9&\-]{0,19}")
_BSE_RE = re.compile(r"\d{6}")
_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}\d")


def _words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = _JOINERS_RE.sub("", folded).replace("&", " & ")
    return [_WORD_EQUIVALENTS.get(w, w) for w in _SEPARATORS_RE.split(folded) if w]


def name_key(text: str) -> str:
    """The spelling-independent form of a name; "" when nothing is left."""
    return " ".join(_words(text))


def is_same_name(a: str, b: str) -> bool:
    """Whether two spellings name the same thing. An empty name is never the
    same as anything, itself included."""
    key = name_key(a)
    return bool(key) and key == name_key(b)


def matches_query(query: str, candidate: str) -> bool:
    """Whether `query` is a run of whole words of `candidate`, in order."""
    wanted = _words(query)
    words = _words(candidate)
    if not wanted:
        return False
    n = len(wanted)
    return any(words[i : i + n] == wanted for i in range(len(words) - n + 1))


def normalize_cin(value: str) -> str | None:
    """The CIN in canonical (upper-case, unspaced) form, or None if it is not
    a 21-character CIN."""
    cin = re.sub(r"\s+", "", value).upper()
    return cin if _CIN_RE.fullmatch(cin) else None


def normalize_identifier(kind: str, value: str) -> str | None:
    """The value as it is stored and shown, or None when `kind` is unknown or
    the value is malformed for it. An alias keeps its own spelling."""
    stripped = value.strip()
    if kind == "alias":
        return stripped if name_key(stripped) else None
    upper = stripped.upper()
    pattern = {"nse_symbol": _NSE_RE, "bse_code": _BSE_RE, "isin": _ISIN_RE}.get(kind)
    if pattern is None or not pattern.fullmatch(upper):
        return None
    return upper


def identifier_key(kind: str, value: str) -> str | None:
    """What makes two identifiers of a kind the same -- the key that is
    unique per (kind, key). Aliases compare by name key, so "RIL" and "r.i.l"
    are one alias; the coded kinds compare by canonical form."""
    canonical = normalize_identifier(kind, value)
    if canonical is None:
        return None
    return name_key(canonical) if kind == "alias" else canonical
