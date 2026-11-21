"""Turn extracted facts into composed ones, and say how complete each is.

THE RULE MEASUREMENT FORCED
---------------------------
Requiring EVERY input before deriving anything was measured and rejected: on TCS
it took derivation from 10 facts to 1 and suppressed 37 nodes, because no real
filer discloses every child a taxonomy defines. The end-to-end run showed the
same shape at full scale -- the balance sheet derived **0 of 293** concepts, with
11 parents stuck holding some but not all of their inputs.

So a partial sum is COMPUTED and MARKED, never silently dropped and never
silently presented as complete. `coverage` is the fraction of inputs that were
present, and a partial value can never satisfy a reported total's
reconciliation -- otherwise "it ties" would stop meaning anything.

REPORTED WINS
-------------
A parent the report prints keeps the reported value. The formula becomes a
CHECK on it rather than a replacement, which is what lets TCS's DEPEXP = 4,288
stand while its DEP/AMT children are both null.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace

_MAX_PASSES = 12


@dataclass(frozen=True)
class Formula:
    """One node's arithmetic. `kind` is derivation | waterfall | reconciliation."""

    tag: str
    kind: str
    inputs: tuple[tuple[str, float], ...]   # (input tag, signed weight)


@dataclass(frozen=True)
class Composed:
    """A value the schema produced, and how much of it is actually supported."""

    tag: str
    value: float
    origin: str          # "derived" (every input present) | "partial"
    coverage: float      # present inputs / declared inputs
    present: int
    total: int
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.present == self.total


@dataclass(frozen=True)
class Check:
    """A reported value set against its own formula."""

    tag: str
    kind: str
    reported: float
    computed: float
    ties: bool
    coverage: float
    certified: bool = False

    @property
    def conclusive(self) -> bool:
        """A partial formula cannot clear a reported total -- UNLESS the issuer's
        own reported figure certifies it (see `certify`). Absent that proof, a
        partial agreement is evidence we did not read every input, not evidence
        of agreement."""
        return self.coverage >= 1.0 or self.certified


def read_formulas(schema: Mapping[str, object]) -> dict[str, Formula]:
    """Every derivation / waterfall / reconciliation in a v4.3 schema."""
    out: dict[str, Formula] = {}
    modules = schema.get("modules") if isinstance(schema, Mapping) else None
    if not isinstance(modules, list):
        return out

    def walk(fields: object) -> None:
        if not isinstance(fields, list):
            return
        for node in fields:
            if not isinstance(node, Mapping):
                continue
            tag = node.get("tag")
            if isinstance(tag, str) and tag:
                for kind in ("derivation", "waterfall", "reconciliation"):
                    spec = node.get(kind)
                    if not isinstance(spec, Mapping):
                        continue
                    raw = spec.get("inputs")
                    if not isinstance(raw, list):
                        continue
                    inputs: list[tuple[str, float]] = []
                    for item in raw:
                        if isinstance(item, str) and item:
                            inputs.append((item, 1.0))
                        elif isinstance(item, Mapping):
                            it = item.get("tag")
                            wt = item.get("weight", 1)
                            if isinstance(it, str) and it and isinstance(wt, (int, float)):
                                inputs.append((it, float(wt)))
                    if inputs:
                        out[tag] = Formula(tag, kind, tuple(inputs))
            walk(node.get("fields"))

    for module in modules:
        if not isinstance(module, Mapping):
            continue
        units = module.get("retrieval_units")
        if not isinstance(units, list):
            continue
        for unit in units:
            if isinstance(unit, Mapping):
                walk(unit.get("fields"))
    return out


def _value(
    tag: str, reported: Mapping[str, float], composed: Mapping[str, Composed]
) -> float | None:
    if tag in reported:
        return reported[tag]
    got = composed.get(tag)
    return got.value if got is not None else None


def _support(
    tag: str, reported: Mapping[str, float], composed: Mapping[str, Composed]
) -> float:
    """How well an input is itself supported: 1.0 reported, else its coverage.

    Coverage MUST propagate. Without this, a parent whose two inputs are both
    half-built partials reported "100% coverage" merely because both inputs
    existed -- measured on TCS, `TOTA` claimed a conclusive gap of 110,270 vs
    31,873 while both its inputs were partial sums. A conclusion drawn from
    partial evidence is not a conclusion."""
    if tag in reported:
        return 1.0
    got = composed.get(tag)
    return got.coverage if got is not None else 0.0


def compose(
    reported: Mapping[str, float], formulas: Mapping[str, Formula]
) -> dict[str, Composed]:
    """Resolve every formula that any input supports.

    Runs to a fixed point so a chain settles regardless of declaration order:
    a parent whose child is itself derived resolves on a later pass. Complete
    sums settle FIRST across all passes, so a node never gets locked in as
    partial while a fuller answer was still reachable."""
    out: dict[str, Composed] = {}
    reconciliations = {t for t, f in formulas.items() if f.kind == "reconciliation"}

    for _ in range(_MAX_PASSES):
        changed = False
        for tag, formula in formulas.items():
            if tag in reported or tag in out or tag in reconciliations:
                continue
            total = 0.0
            present = 0
            support = 0.0
            for name, weight in formula.inputs:
                got = _value(name, reported, out)
                if got is not None:
                    total += got * weight
                    present += 1
                    support += _support(name, reported, out)
            if present == len(formula.inputs) and present:
                coverage = support / present
                out[tag] = Composed(
                    tag, total, "derived" if coverage >= 1.0 else "partial",
                    coverage, present, present,
                )
                changed = True
        if not changed:
            break

    # Only now, with every COMPLETE sum settled, record the partials -- and
    # iterate these to a fixed point too, so a partial cascades into its parent.
    # Running this pass once left a parent unresolved whenever its subtotals
    # happened to be visited after it.
    for _ in range(_MAX_PASSES):
        changed = False
        for tag, formula in formulas.items():
            if tag in reported or tag in out or tag in reconciliations:
                continue
            total = 0.0
            present = 0
            support = 0.0
            missing: list[str] = []
            for name, weight in formula.inputs:
                got = _value(name, reported, out)
                if got is None:
                    missing.append(name)
                else:
                    total += got * weight
                    present += 1
                    support += _support(name, reported, out)
            if present:
                out[tag] = Composed(
                    tag, total, "partial", support / len(formula.inputs),
                    present, len(formula.inputs), tuple(missing),
                )
                changed = True
        if not changed:
            break
    return out


def certify(
    reported: Mapping[str, float],
    formulas: Mapping[str, Formula],
    composed: Mapping[str, Composed],
    *,
    tolerance: float = 0.005,
) -> tuple[dict[str, Composed], set[str]]:
    """Let the report prove its own completeness.

    WHY THIS EXISTS
    ---------------
    Coverage counts DECLARED children, but a taxonomy declares the union of what
    any filer might report while one filer reports a subset. Measured on TCS: of
    the 160 concepts blocking P&L derivations, 85 cannot be filled by a services
    company at all -- only 8 were genuine misses. `NONOI` declares nine children,
    eight inapplicable to TCS; holding the one that applies it read 11% coverage
    and was called inconclusive, when it was 100% of what TCS could ever report.

    Hand-maintaining an applicability list per issuer is unmaintainable. The
    report certifies itself instead: when a reported total EQUALS the sum of the
    inputs we did find, the ones we did not find are PROVEN absent (or zero,
    which composes identically). No ground truth needed.

    The proof propagates. Certifying a total also certifies the partial children
    that contributed to it -- their own unfound children are equally proven
    absent -- and that can let a higher total tie on the next pass.

    Returns (composed with certified entries at full coverage, certified tags).

    CAUTION: a single child that merely echoes its parent's total would also
    "tie". That is why `note_plan._echoes_an_ancestor` rejects such lines before
    they ever become facts -- the two guards are a pair.
    """
    out = dict(composed)
    certified: set[str] = set()
    for _ in range(_MAX_PASSES):
        changed = False
        for tag, formula in formulas.items():
            actual = reported.get(tag)
            if actual is None:
                continue
            total = 0.0
            present: list[str] = []
            absent: list[str] = []
            for name, weight in formula.inputs:
                got = _value(name, reported, out)
                if got is None:
                    absent.append(name)
                else:
                    total += got * weight
                    present.append(name)
            # Something must be under-supported for a proof to be worth making:
            # either an input we never found, or one that is itself partial.
            # (Requiring an ABSENT input was too strict -- it missed the case
            # where the only input is present but half-built beneath.)
            weak = [n for n in present if _support(n, reported, out) < 1.0]
            if not present or (not absent and not weak):
                continue
            if abs(actual - total) > max(1.0, abs(actual) * tolerance):
                continue                     # does not tie: proves nothing
            if tag not in certified:
                certified.add(tag)
                changed = True
            for name in present:
                node = out.get(name)
                if node is not None and node.coverage < 1.0:
                    out[name] = replace(node, coverage=1.0, origin="certified")
                    certified.add(name)
                    changed = True
        if not changed:
            break
    return out, certified


def reconcile(
    reported: Mapping[str, float],
    formulas: Mapping[str, Formula],
    composed: Mapping[str, Composed],
    *,
    certified: Collection[str] = (),
    tolerance: float = 0.005,
) -> list[Check]:
    """Set every REPORTED value against its own formula.

    Coverage rides along so a caller can tell "these agree" from "these agree
    on the half we managed to read"."""
    out: list[Check] = []
    for tag, formula in sorted(formulas.items()):
        actual = reported.get(tag)
        if actual is None:
            continue
        total = 0.0
        present = 0
        support = 0.0
        for name, weight in formula.inputs:
            got = _value(name, reported, composed)
            if got is not None:
                total += got * weight
                present += 1
                support += _support(name, reported, composed)
        if not present:
            continue
        ties = abs(actual - total) <= max(1.0, abs(actual) * tolerance)
        out.append(Check(
            tag, formula.kind, actual, total, ties,
            support / len(formula.inputs), tag in certified,
        ))
    return out


def summarise(checks: Sequence[Check]) -> dict[str, int]:
    """Counts a report can be built from, keeping partial evidence separate."""
    conclusive = [c for c in checks if c.conclusive]
    return {
        "checks": len(checks),
        "conclusive": len(conclusive),
        "conclusive_ties": sum(1 for c in conclusive if c.ties),
        "conclusive_gaps": sum(1 for c in conclusive if not c.ties),
        "inconclusive": len(checks) - len(conclusive),
    }
