"""Pure XBRL-style export of the Fact graph -- Milestone 4, Pillar 6.

Because our model already mirrors the XBRL calculation/formula linkbases (see the
vision doc), we can emit a well-formed XBRL *instance* document: one context, one
unit (INR), and one numeric fact per tag with a value. Derived subtotals export
alongside extracted facts, and their calculation relationships (parent -> signed
children) export as calculation arcs -- the linkbase a regulator's tools consume.

Element names are sanitised to valid XML NCNames while the true customer tag is
preserved in a `concept` attribute, so a tag like "TOTRE/SOPL" or "WCL#2" still
produces valid XML. No I/O; total (never raises on odd input).
"""

import re
from collections.abc import Mapping
from xml.etree import ElementTree as ET

_XBRLI = "http://www.xbrl.org/2003/instance"
_FDI = "https://fdi.local/taxonomy"
_LINK = "http://www.xbrl.org/2003/linkbase"
_XLINK = "http://www.w3.org/1999/xlink"

ET.register_namespace("xbrli", _XBRLI)
ET.register_namespace("fdi", _FDI)
ET.register_namespace("link", _LINK)


def _q(ns: str, name: str) -> str:
    """ElementTree's namespaced-name form: '{uri}local'."""
    return f"{{{ns}}}{name}"


def _ncname(tag: str) -> str:
    """A valid XML element local-name for a tag: invalid chars -> '_', and a
    leading non-letter/underscore gets an underscore prefix."""
    name = re.sub(r"[^0-9A-Za-z_.-]", "_", tag)
    if not name or not re.match(r"[A-Za-z_]", name[0]):
        name = "_" + name
    return name


def _number(v: object) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def export_instance(
    facts: Mapping[str, Mapping[str, object]],
    *,
    period: str = "2024-03-31",
    entity: str = "urn:fdi:entity:unknown",
    unit_measure: str = "iso4217:INR",
) -> str:
    """A well-formed XBRL instance for every fact carrying a numeric value.

    Facts are emitted in sorted tag order for deterministic output.
    """
    root = ET.Element(_q(_XBRLI, "xbrl"))

    schema_ref = ET.SubElement(root, _q(_LINK, "schemaRef"))
    schema_ref.set(_q(_XLINK, "type"), "simple")
    schema_ref.set(_q(_XLINK, "href"), "fdi-taxonomy.xsd")

    ctx = ET.SubElement(root, _q(_XBRLI, "context"))
    ctx.set("id", "C")
    ent = ET.SubElement(ctx, _q(_XBRLI, "entity"))
    ident = ET.SubElement(ent, _q(_XBRLI, "identifier"))
    ident.set("scheme", "urn:fdi:entity")
    ident.text = entity
    per = ET.SubElement(ctx, _q(_XBRLI, "period"))
    ET.SubElement(per, _q(_XBRLI, "instant")).text = period

    unit = ET.SubElement(root, _q(_XBRLI, "unit"))
    unit.set("id", "U-INR")
    ET.SubElement(unit, _q(_XBRLI, "measure")).text = unit_measure

    for tag in sorted(facts):
        val = _number(facts[tag].get("value"))
        if val is None:
            continue
        el = ET.SubElement(root, _q(_FDI, _ncname(tag)))
        el.set("contextRef", "C")
        el.set("unitRef", "U-INR")
        el.set("decimals", "2")
        el.set("concept", tag)
        origin = facts[tag].get("origin")
        if isinstance(origin, str):
            el.set("origin", origin)
        el.text = f"{val:.2f}"

    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def calculation_arcs(derived: Mapping[str, Mapping[str, object]]) -> list[dict[str, object]]:
    """The calculation linkbase, as data: one arc-set per resolved derived tag,
    listing its input tags. Weight is +1 (sum) or, for a difference, +1 on the
    left operand and -1 on the right -- carried on the DerivedFact's method.
    Only `status == "ok"` derivations produce arcs."""
    arcs: list[dict[str, object]] = []
    for tag, d in derived.items():
        if d.get("status") != "ok":
            continue
        deriv = d.get("derivation")
        if not isinstance(deriv, Mapping):
            continue
        inputs = deriv.get("inputs")
        if not isinstance(inputs, list):
            continue
        arcs.append({
            "parent": tag,
            "method": deriv.get("method"),
            "children": [c for c in inputs if isinstance(c, str)],
            "explanation": deriv.get("explanation"),
        })
    return arcs
