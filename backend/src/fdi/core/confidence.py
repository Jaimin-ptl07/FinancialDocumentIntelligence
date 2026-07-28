"""Pure confidence & accuracy calibration -- Milestone 3, Pillar 5.

Financials let numbers police each other; this turns that into a calibrated,
first-class confidence signal instead of the raw model score. Two mechanisms:

* triangulate() -- when the same tag is read from more than one place (face of
  statement AND the note), agreement corroborates and boosts; disagreement
  lowers confidence and flags for review. Never silently picks a side.
* calibrate()   -- folds validation outcomes into each Fact's confidence: a Fact
  that participates in a reconciliation/identity that PASSED is corroborated
  (boost); one in an error-severity check that FAILED is forced to the `failed`
  band. Everything else lands in high / needs_review / low by threshold.

Bands are what a bank analyst actually triages on. Pure, total, never raises.
"""

from collections.abc import Mapping, Sequence

_RECON_BOOST = 0.10
_IDENTITY_BOOST = 0.05

HIGH = 0.85
REVIEW = 0.60


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _number(v: object) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def triangulate(
    candidates: Mapping[str, Sequence[Mapping[str, object]]], *, tolerance: float = 1.0
) -> dict[str, dict[str, object]]:
    """Reconcile multiple readings of the same tag into one Fact.

    `candidates[tag]` is a list of `{value, confidence, source}` readings (e.g.
    the face of the statement and a note). Returns `{tag: {value, confidence,
    agreement, band, readings}}`:

    * all readings agree within `tolerance` -> value = the highest-confidence
      reading, confidence boosted, agreement "agree", band "high";
    * they disagree -> value = highest-confidence reading, confidence LOWERED,
      agreement "conflict", band "needs_review" (a human must pick);
    * a single reading -> passed through unchanged, agreement "single".
    """
    out: dict[str, dict[str, object]] = {}
    for tag, readings in candidates.items():
        numeric = [
            (n, _number(r.get("confidence")) or 0.0, r)
            for r in readings
            if (n := _number(r.get("value"))) is not None
        ]
        if not numeric:
            continue
        numeric.sort(key=lambda t: t[1], reverse=True)
        best_val, best_conf, _ = numeric[0]
        if len(numeric) == 1:
            out[tag] = {
                "value": best_val, "confidence": best_conf, "agreement": "single",
                "band": _band(best_conf, failed=False), "readings": len(numeric),
            }
            continue
        agree = all(abs(v - best_val) <= tolerance for v, _, _ in numeric)
        conf = _clamp(best_conf + 0.1) if agree else _clamp(best_conf - 0.3)
        out[tag] = {
            "value": best_val, "confidence": conf,
            "agreement": "agree" if agree else "conflict",
            "band": "high" if agree and conf >= HIGH else "needs_review",
            "readings": len(numeric),
        }
    return out


def _band(confidence: float, *, failed: bool) -> str:
    if failed:
        return "failed"
    if confidence >= HIGH:
        return "high"
    if confidence >= REVIEW:
        return "needs_review"
    return "low"


def calibrate(
    facts: Mapping[str, Mapping[str, object]],
    validations: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """Per-tag calibrated confidence + band, folding validation outcomes in.

    Returns `{tag: {confidence, band, origin, signals}}` where `signals` is the
    human-readable list of what moved the number (corroboration or failure).
    """
    failed_error: set[str] = set()
    passed: dict[str, list[Mapping[str, object]]] = {}
    failed_any: dict[str, list[Mapping[str, object]]] = {}
    for v in validations:
        tags = v.get("tags")
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if not isinstance(tag, str):
                continue
            if v.get("passed"):
                passed.setdefault(tag, []).append(v)
            else:
                failed_any.setdefault(tag, []).append(v)
                if v.get("severity") == "error":
                    failed_error.add(tag)

    out: dict[str, dict[str, object]] = {}
    for tag, f in facts.items():
        conf = _number(f.get("confidence")) or 0.0
        signals: list[str] = []
        for v in passed.get(tag, []):
            if v.get("kind") == "reconciliation":
                conf = _clamp(conf + _RECON_BOOST)
                signals.append(f"corroborated: {v.get('rule_id')} reconciles")
            elif v.get("kind") == "identity":
                conf = _clamp(conf + _IDENTITY_BOOST)
                signals.append(f"corroborated: {v.get('rule_id')} holds")
        for v in failed_any.get(tag, []):
            signals.append(f"failed check: {v.get('rule_id')} (delta {v.get('delta')})")

        band = _band(conf, failed=tag in failed_error)
        out[tag] = {
            "confidence": conf, "band": band,
            "origin": f.get("origin", "extracted"), "signals": signals,
        }
    return out
