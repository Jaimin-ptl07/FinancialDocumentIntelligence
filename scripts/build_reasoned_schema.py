"""Reasoning-driven schema build, sheet by sheet, from the raw taxonomy grid.

Unlike the earlier mechanical parser, this encodes the placement decisions we
reasoned out from reading the sheet (see the M1/M4 specs + chat 2026-07-28):

 * numbering is coalesced across the TWO adjacent columns (labeled "Sr. No."
   and its unlabeled neighbour), giving the full up-to-4-level dotted path;
 * numbered nodes nest by dotted PREFIX (parent = drop last segment);
 * the five kinds of NO-NUMBER rows are placed by role, not by position:
     A subtotal        -> child of the node at the common prefix of its
                          "Total of X to Y" range; derivation.inputs resolved
                          to the tags in that range (option 1: supplementary
                          siblings are NOT folded in);
     B text sub-header -> a group node under the current numbered parent;
     C supplementary   -> child of the current text sub-header (or numbered
                          parent); kept, but excluded from subtotals;
     D mirror "(as shown in exp)" -> leaf flagged reference (has a redirect);
     E memo (EPS, territory note) -> leaf flagged memo;
 * TOTAL TO TAG totals are origin=extracted (read from the report) AND carry a
   reconciliation formula so validation compares reported vs computed;
 * the author's row-267+ "Summary"/scratch notes are dropped.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW = ROOT / "docs" / "architecture" / "taxonomy" / "taxonomy.raw.json"

SRNUM = re.compile(r"^\d+(\.\d+)*$")


def norm(s):
    return re.sub(r"\s+", " ", s).strip() if isinstance(s, str) else ""


def flag_role(flag):
    f = norm(flag).lower()
    if f in ("donot tag", "do not tag", "donot tag - old tag",
             "do not tag - old tag", "not tagged"):
        return "derived"
    if f == "total to tag":
        return "total_tagged"
    if f == "not considered":
        return "not_considered"
    if f == "new":
        return "new"
    return ""


_FLAG_VOCAB = ("donot tag", "do not tag", "not tagged", "total to tag",
               "not considered", "donot tag - old tag")


def load_sheet(sheet):
    grid = sheet["grid"]
    hdr = next(i for i, row in enumerate(grid[:6])
               if any(norm(c).lower() == "particulars" for c in row))
    H = grid[hdr]
    def find(pred):
        return next((j for j, c in enumerate(H) if pred(norm(c).lower())), None)
    srcol = find(lambda h: "sr" in h.replace(" ", "") and "no" in h.replace(" ", ""))
    partc = find(lambda h: "particular" in h)
    comc = find(lambda h: "comment" in h)
    tagc = find(lambda h: "tag" in h and "redir" not in h)
    r2c = find(lambda h: "redirection" in h and "2" in h)
    blockc = find(lambda h: "block" in h)

    # The flag/"Comments" column is UNLABELED on Assets/Liabilities/Equity; find
    # it as the still-unmapped column richest in the known flag vocabulary.
    if comc is None:
        taken = {srcol, srcol + 1, partc, tagc, r2c, blockc}
        width = max((len(r) for r in grid[hdr + 1:]), default=0)
        best, best_hits = None, 0
        for j in range(width):
            if j in taken:
                continue
            hits = sum(1 for row in grid[hdr + 1:]
                       if j < len(row) and norm(row[j]).lower() in _FLAG_VOCAB)
            if hits > best_hits:
                best, best_hits = j, hits
        comc = best

    def g(row, j):
        return norm(row[j]) if j is not None and j < len(row) else ""

    def sr_of(row):
        for j in (srcol, srcol + 1):
            v = g(row, j)
            if SRNUM.match(v):
                return v
        return ""

    rows = []
    for row in grid[hdr + 1:]:
        part = g(row, partc)
        if not part:
            continue
        rows.append({
            "sr": sr_of(row), "tag": g(row, tagc), "particulars": part,
            "flag": flag_role(g(row, comc)), "block": g(row, blockc),
            "r2": g(row, r2c),
        })
    return rows


def prefix(path):
    parts = path.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else None


def parse_range_prefix(particulars, sr_to_tag):
    """(inputs, common_prefix) for a subtotal label, using the whole sheet's
    coalesced sr->tag map. Handles 'Total of a to b [& c to d]' and 'a minus b'."""
    m = re.findall(r"\(([^()]*)\)", particulars)
    span = m[-1] if m else particulars
    span = re.sub(r"(?i)total\s*of", " ", span)
    nums = re.findall(r"\d+(?:\.\d+)*", span)
    if not nums:
        return [], None
    is_minus = bool(re.search(r"\bminus\b", span, re.I))
    tuples = [tuple(int(x) for x in n.split(".")) for n in nums]

    def in_any_range(t):
        if is_minus:
            return t in tuples
        # union of consecutive (lo,to,hi) pairs and singletons
        ok = False
        i = 0
        pairs = []
        # rebuild pairs by scanning 'a to b'
        toks = re.split(r"\bto\b|&", span, flags=re.I)
        for seg in re.split(r"&", span):
            seg = seg.strip()
            parts = re.split(r"\bto\b", seg, flags=re.I)
            if len(parts) == 2:
                a = parts[0].strip(); b = parts[1].strip()
                if SRNUM.match(a) and SRNUM.match(b):
                    lo = tuple(int(x) for x in a.split("."))
                    hi = tuple(int(x) for x in b.split("."))
                    pairs.append((lo, hi))
            elif SRNUM.match(seg):
                v = tuple(int(x) for x in seg.split("."))
                pairs.append((v, v))
        for lo, hi in pairs:
            if len(t) == len(lo) and lo <= t <= hi:
                return True
        return False

    inputs = []
    for sr, tag in sr_to_tag.items():
        t = tuple(int(x) for x in sr.split("."))
        if in_any_range(t):
            inputs.append(tag)
    # common prefix of the referenced numbers
    common = []
    for i in range(min(len(t) for t in tuples)):
        col = {t[i] for t in tuples}
        if len(col) == 1:
            common.append(next(iter(col)))
        else:
            break
    common_prefix = ".".join(str(x) for x in common) if common else None
    return inputs, common_prefix


def parse_tag_arith(part, known_tags):
    """A bare tag-arithmetic label like 'GRSTOTTA - NETTOTTA' -> (op, [a, b])."""
    m = re.fullmatch(r"\s*([A-Z][A-Z0-9/]+)\s*([+\-])\s*([A-Z][A-Z0-9/]+)\s*", part)
    if not m:
        return None
    a, op, b = m.group(1), m.group(2), m.group(3)
    if a in known_tags and b in known_tags:
        return ("diff" if op == "-" else "sum", [a, b])
    return None


def build(rows, statement, key, name):
    sr_to_tag = {r["sr"]: r["tag"] for r in rows if r["sr"] and r["tag"]}
    known_tags = {r["tag"] for r in rows if r["tag"]}
    roots = []
    stack = []          # [(depth, node, sr)] — order+depth nesting
    current_group = [None]
    pending_label = [None]  # a text header awaiting its meaning (label vs group)

    def cur_numbered():
        return stack[-1][1] if stack else None

    def add(node, parent):
        if parent is None:
            roots.append(node)
        else:
            parent.setdefault("fields", []).append(node)

    for r in rows:
        sr, tag, part, flag = r["sr"], r["tag"], r["particulars"], r["flag"]
        low = part.lower()
        # drop the author's summary / scratch block at the foot of the sheet
        if not tag and not sr and ("line item" in low or low == "summary"
                                   or re.match(r"^\d+(\.\d+)*$", part)):
            continue

        is_subtotal = (flag in ("derived", "total_tagged")
                       or re.search(r"total of|minus", part, re.I))
        redirect = r["r2"] if r["r2"] and norm(r["r2"]).lower() != "not considered" else None

        arith = parse_tag_arith(part, known_tags)

        # a text header immediately followed by a single numbered/subtotal line
        # is that line's DESCRIPTION -> merge; clear it once consumed.
        display = part
        if pending_label[0] and (sr or is_subtotal):
            display = f"{pending_label[0]} — {part}"
            pending_label[0] = None

        if sr:
            depth = sr.count(".") + 1
            origin = "derived" if flag == "derived" else "extracted"
            node = {"field_name": display, "tag": tag or None, "origin": origin,
                    "sr_no": sr, "role": "line"}
            if arith and tag:  # 'GRSTOTTA - NETTOTTA' style formula
                node["origin"], node["role"] = "derived", "subtotal"
                node["derivation"] = {"method": arith[0],
                                      "inputs": [t for t in arith[1] if t != tag]}
            elif flag in ("derived", "total_tagged"):
                inputs, _ = parse_range_prefix(part, sr_to_tag)
                inputs = [t for t in inputs if t != tag]
                if flag == "total_tagged":
                    node["origin"], node["role"] = "extracted", "total_reported"
                    node["reconciliation"] = {"method": "sum", "inputs": inputs}
                else:
                    node["origin"], node["role"] = "derived", "subtotal"
                    node["derivation"] = {"method": "sum", "inputs": inputs}
            elif re.search(r"as reported", part, re.I):
                node["role"] = "total_reported"
            if redirect:
                node["redirect"] = redirect
            while stack and stack[-1][0] >= depth:
                stack.pop()
            add(node, cur_numbered())
            stack.append((depth, node, sr))
            current_group[0] = None
            continue

        if is_subtotal:
            inputs, common = parse_range_prefix(part, sr_to_tag)
            inputs = [t for t in inputs if t != tag]
            origin = "extracted" if flag == "total_tagged" else "derived"
            node = {"field_name": display, "tag": tag or None, "origin": origin,
                    "sr_no": None,
                    "role": "total_reported" if flag == "total_tagged" else "subtotal"}
            fld = "reconciliation" if flag == "total_tagged" else "derivation"
            node[fld] = {"method": "sum", "inputs": inputs}
            parent = next((n for (d, n, s) in reversed(stack) if s == common), cur_numbered())
            add(node, parent)
            current_group[0] = None
            continue

        if not tag:  # text header -> hold as pending; its role is decided by
            # what follows (a numbered line = label to merge; no-number rows = a
            # real sub-header group).
            pending_label[0] = part
            current_group[0] = None
            continue

        # no-number tagged leaf. Distinguish:
        #  * mirror reference  -> "(as shown in exp)"/"(as reported)"; excluded;
        #  * supplementary      -> under a text sub-header (extra breakdown that
        #                          the parent's stated total does NOT include);
        #  * component          -> directly under a numbered parent = a genuine
        #                          part (e.g. the Current/Non-Current split of a
        #                          liability), INCLUDED in the parent's sum.
        # A redirect is just the Dataset-2 rollup and does NOT imply a reference.
        # A held text header followed by no-number rows IS a real sub-header
        # group: materialise it now and nest these rows under it.
        if pending_label[0] is not None:
            gnode = {"field_name": pending_label[0], "node_type": "group",
                     "sr_no": None, "role": "sub_header", "fields": []}
            add(gnode, cur_numbered())
            current_group[0] = gnode
            pending_label[0] = None

        if re.search(r"as shown in exp|as reported", part, re.I):
            role = "reference"
        elif current_group[0] is not None:
            role = "supplementary"
        else:
            role = "component"
        node = {"field_name": part, "tag": tag or None, "origin": "extracted",
                "sr_no": None, "role": role}
        if redirect:
            node["redirect"] = redirect
        if flag == "not_considered":
            node["not_considered"] = True
        add(node, current_group[0] or cur_numbered())

    # A derived parent with no explicit "Total of" formula sums its own numbered
    # children — but option 1: supplementary/reference children stay excluded.
    def fill(nodes):
        for n in nodes:
            fill(n.get("fields", []))
            deriv = n.get("derivation")
            if deriv is not None and not deriv.get("inputs"):
                kids = [c["tag"] for c in n.get("fields", [])
                        if c.get("tag") and not c.get("not_considered")
                        and c.get("role") in
                        ("line", "subtotal", "total_reported", "component")]
                if kids:
                    deriv["inputs"] = kids
    fill(roots)

    return {"key": key, "name": name, "statement": statement,
            "retrieval_units": [{"unit_id": statement[:2].upper(),
                                 "statement_type": statement,
                                 "retrieval_query": f"{name} statement",
                                 "fields": roots}]}


# A tagless container-father's total lives in a differently-numbered TOTxxx row,
# so a "Total of 1 to 8" grand total cannot resolve it by number. These reasoned
# mappings close that gap (see chat 2026-07-28).
FATHER_TOTAL_OVERRIDES = {
    "liabilities": {"TOTL": ["TOTBOR", "NETDTL", "LL", "TRP", "TOTOL",
                             "TOTPRV", "TAXL", "LAHS"]},
    "equity": {"TOTEQNR": ["TOTEQ", "TOTOTEQ", "TOTRESV", "TOTRE/SOPL"]},
}

# The document states its own unit ("(Rs crore)"); extraction never converts.
_REPORTING_UNIT = "as reported"
_LABELS = {"profit_and_loss": "Profit and Loss", "assets": "Assets",
           "liabilities": "Liabilities", "equity": "Equity"}


def data_type(particulars):
    p = particulars.lower()
    if "%" in p or "percentage" in p or "ratio" in p or "margin" in p:
        return "decimal", True
    if "days" in p or "number of" in p:
        return "integer", False
    if "date" in p or "as at" in p or "year ended" in p:
        return "date", False
    if any(k in p for k in ("name", "address", "policy")):
        return "text", False
    return "decimal", False


def prompt(particulars, statement):
    """Per-field instruction.

    It must NOT name a target unit. The shared extraction rules already say
    "return the number exactly as printed -- do not rescale, convert, or
    reinterpret units"; an earlier version of this prompt said "report the
    figures in INR Lakhs", which CONTRADICTED that rule and was measured
    inflating TCS values 10x (Prepaid expenses 2,049 -> 20,490) once note
    evidence entered the prompt. Canonicalising to a target unit is the
    Normalization stage's job, downstream of extraction."""
    return (f"Extract the value of '{particulars}' from the "
            f"{_LABELS.get(statement, statement)} statement. Report the "
            f"current-year and prior-year figures exactly as printed in the "
            f"document's own reporting unit; return null if the line item is "
            f"absent.")


def _uniq(base, seen):
    if base not in seen:
        seen.add(base); return base
    n = 2
    while f"{base}#{n}" in seen:
        n += 1
    seen.add(f"{base}#{n}"); return f"{base}#{n}"


ALIASES = ROOT / "docs" / "architecture" / "taxonomy" / "concept-note-aliases.json"
DEFINITIONS = ROOT / "docs" / "architecture" / "taxonomy" / "concept-definitions.json"


def _definition_table(statement):
    """{normalized concept name: definition} for a statement."""
    if not DEFINITIONS.exists():
        return {}
    raw = json.loads(DEFINITIONS.read_text("utf-8")).get("definitions", {})
    return {
        re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", k.lower())).strip(): v
        for k, v in raw.get(statement, {}).items()
    }


def _alias_table(statement):
    """{normalized concept name: [report-side label variants]} for a statement."""
    if not ALIASES.exists():
        return {}
    raw = json.loads(ALIASES.read_text("utf-8")).get("concepts", {})
    return {
        re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", k.lower())).strip(): v
        for k, v in raw.get(statement, {}).items()
    }


def _uniq_tag(tag, seen_tags):
    """A tag used once. The taxonomy reuses `WCL` for working-capital loans and
    again for Buyers Credit; leaving both as `WCL` makes the second unreachable
    in by_tag / all_concepts / for_tag / the XBRL export. The customer's code is
    kept on the node as `taxonomy_tag`."""
    if tag not in seen_tags:
        seen_tags.add(tag)
        return tag
    n = 2
    while f"{tag}{n}" in seen_tags:
        n += 1
    out = f"{tag}{n}"
    seen_tags.add(out)
    return out


def enrich(nodes, statement, seen, counter, table=None, inherited=(), defs=None,
           seen_tags=None):
    """Add field_path (unique) to tagged nodes and data_type/extraction_prompt
    to extracted LEAVES, so parse_field_specs + validate_extraction_schema
    accept the tree. A tagless VALUED leaf (a subtotal the customer never tagged)
    gets a synthetic deterministic tag so it stays an addressable Fact. Group and
    derived nodes need no extraction_prompt."""
    if seen_tags is None:
        seen_tags = set()
    if table is None:
        table = _alias_table(statement)
    if defs is None:
        defs = _definition_table(statement)
    for n in nodes:
        # A concept's report-side aliases are INHERITED by its whole subtree:
        # the note reference is printed against the parent ("Property, plant and
        # equipment | 8(a)"), while the leaves carry taxonomy names ("Buildings").
        key = re.sub(r"\s+", " ",
                     re.sub(r"[^a-z0-9 ]", " ", n.get("field_name", "").lower())).strip()
        aliases = table.get(key) or list(inherited)
        # A definition is NOT inherited: it describes THIS concept, and an
        # inherited one would wrongly claim a parent's scope for a child.
        definition = defs.get(key)
        if definition:
            n["definition"] = definition
        children = n.get("fields")
        if children:
            if n.get("tag"):
                original = n["tag"]
                n["tag"] = _uniq_tag(original, seen_tags)
                if n["tag"] != original:
                    n["taxonomy_tag"] = original
                n["field_path"] = _uniq(f"{statement}.{n['tag']}", seen)
            # the PARENT carries the aliases too: it is the concept a report
            # line is mapped against (fdi.core.concepts groups by parent).
            if aliases:
                n["note_aliases"] = list(dict.fromkeys(aliases))
            enrich(children, statement, seen, counter, table, aliases, defs,
                   seen_tags)
            continue
        # childless leaf
        tag = n.get("tag")
        if not tag and n.get("origin"):  # tagless computed value -> synthesize
            counter[0] += 1
            tag = f"AUTO{statement[:2].upper()}{counter[0]:03d}"
            n["tag"] = tag
            n["synthetic_tag"] = True
        if tag:
            unique = _uniq_tag(tag, seen_tags)
            if unique != tag:
                n["taxonomy_tag"] = tag
                n["tag"] = tag = unique
            n["field_path"] = _uniq(f"{statement}.{tag}", seen)
        if aliases:
            n["note_aliases"] = list(dict.fromkeys(aliases))
        if n.get("origin") == "extracted":
            dt, is_ratio = data_type(n.get("field_name", ""))
            n["data_type"] = dt
            if is_ratio:
                n["is_ratio"] = True
            n["extraction_prompt"] = prompt(n.get("field_name", ""), statement)
        elif n.get("origin"):  # derived leaf: type only, no prompt
            n["data_type"] = "decimal"


def apply_overrides(nodes, statement):
    overrides = FATHER_TOTAL_OVERRIDES.get(statement, {})
    for n in nodes:
        tag = n.get("tag")
        if tag in overrides and n.get("derivation") is not None:
            n["derivation"]["inputs"] = overrides[tag]
        apply_overrides(n.get("fields", []), statement)


def depth_counts(roots):
    counts = {}
    def walk(nodes, d):
        for n in nodes:
            if n.get("sr_no"):
                counts[n["sr_no"].count(".") + 1] = counts.get(n["sr_no"].count(".") + 1, 0) + 1
            walk(n.get("fields", []), d + 1)
    walk(roots, 1)
    return counts


SHEETS = [
    (0, "profit_and_loss", "Profit and Loss", "pl"),
    (1, "assets", "Assets", "assets"),
    (2, "liabilities", "Liabilities", "liabilities"),
    (3, "equity", "Equity", "equity"),
]


def assemble():
    """Build all four sheets, enrich, apply overrides, and write one integrated
    v4.1 schema that parses + validates in the pipeline."""
    sys.path.insert(0, str(ROOT / "backend" / "src"))
    from fdi.core.schema import validate_extraction_schema

    raw = json.loads(RAW.read_text("utf-8"))
    seen: set[str] = set()
    modules = []
    for idx, statement, name, _slug in SHEETS:
        module = build(load_sheet(raw["sheets"][idx]), statement,
                       f"module_{statement}", name)
        roots = module["retrieval_units"][0]["fields"]
        roots[:] = [n for n in roots if n.get("fields") or n.get("tag")]  # prune empties
        apply_overrides(roots, statement)
        enrich(roots, statement, seen, [0])
        modules.append(module)

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema-descriptor",
        "title": "FDI Taxonomy Field Schema (reasoned, v4.2 - concept note aliases)",
        "version": "4.2.0",
        "reporting_unit": _REPORTING_UNIT,
        "taxonomy_source": "Q3VENI-taxonomy-source.xlsx",
        "modules": modules,
    }
    errors = validate_extraction_schema(schema)
    out = ROOT / "docs" / "architecture" / "field-extraction-schema.v4_2.json"
    if errors:
        print("VALIDATION FAILED:", len(errors))
        for e in errors[:30]:
            print("  -", e)
        return 1
    out.write_text(json.dumps(schema, ensure_ascii=False, indent=1), "utf-8")

    from fdi.core.extractor import parse_field_specs
    specs = parse_field_specs(schema)
    print(f"OK  modules={len(modules)}  extracted fields={len(specs)}  -> {out.name}")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "all":
        raise SystemExit(assemble())
    raw = json.loads(RAW.read_text("utf-8"))
    which = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].isdigit() else "0"
    idx, statement, name, slug = SHEETS[int(which)]
    module = build(load_sheet(raw["sheets"][idx]), statement,
                   f"module_{statement}", name)
    roots = module["retrieval_units"][0]["fields"]
    dc = depth_counts(roots)
    print("numbered depth counts (father/son/grandson/breakup):",
          {k: dc.get(k, 0) for k in (1, 2, 3, 4)})
    # print the tree
    def show(nodes, d=0):
        for n in nodes:
            tag = n.get("tag") or ""
            role = n.get("role", n.get("node_type", ""))
            print("  " * d + f"{n.get('sr_no') or '·':8} {tag:11} [{role}] {n['field_name'][:44]}")
            show(n.get("fields", []), d + 1)
    if "--tree" in sys.argv:
        show(roots)
    total_tags = sum(1 for _ in _iter_tags(roots))
    print("total tagged nodes:", total_tags)
    out = ROOT / "docs" / "architecture" / "taxonomy" / f"{slug}.reasoned.json"
    out.write_text(json.dumps(module, ensure_ascii=False, indent=1), "utf-8")
    print("wrote", out.name)


def _iter_tags(nodes):
    for n in nodes:
        if n.get("tag"):
            yield n["tag"]
        yield from _iter_tags(n.get("fields", []))


if __name__ == "__main__":
    main()
