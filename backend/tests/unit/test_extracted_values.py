"""How an Extracted value is judged and shown (S3): validation, the status
an Invitee sees, and derived Fields from effective inputs."""

from fdi.core.extracted_values import compute_derived, validate_value, value_status
from fdi.core.extraction_schema import field_from_mapping


def _f(**raw: object):
    return field_from_mapping({"name": "X", **raw}, field_id=str(raw.get("tag", "x")))


def _status(**kw: object) -> str:
    args = {"read_status": "extracted", "review_action": None, "problems": [],
            "confidence": 0.9, "threshold": 0.6, "required": False}
    args.update(kw)
    return value_status(**args)  # type: ignore[arg-type]


def test_numbers_are_checked_against_type_and_rules() -> None:
    f = _f(data_type="decimal", validation={"min": 0, "max": 100})
    assert validate_value(50, f) == []
    assert validate_value({"current": 5, "prior": None}, f) == []
    assert validate_value("1,234", f) == ["expected a number, got str"]
    assert validate_value(-1, f) == ["-1 is below the minimum 0"]
    assert validate_value({"current": 101, "prior": 3}, f) == ["101 is above the maximum 100"]
    assert validate_value(1.5, _f(data_type="integer")) == ["expected a whole number"]
    assert validate_value(-2, _f(validation={"non_negative": True})) == ["-2 is negative"]


def test_text_is_checked_against_allowed_values_and_pattern() -> None:
    enum = _f(data_type="enum", allowed_values=["Unqualified", "Qualified"])
    assert validate_value("Qualified", enum) == []
    assert validate_value("Adverse", enum) == ["'Adverse' is not one of the allowed values"]
    cin = _f(data_type="string", validation={"pattern": r"[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}"})
    assert validate_value("L22210MH1995PLC084781", cin) == []
    assert validate_value("not a cin", cin) == ["does not match the Field's pattern"]


def test_a_null_value_has_no_problems() -> None:
    assert validate_value(None, _f(validation={"min": 0})) == []


def test_status_a_review_wins_over_everything() -> None:
    assert _status(read_status="failed", review_action="corrected") == "corrected"
    assert _status(problems=["bad"], review_action="accepted") == "accepted"


def test_status_doubtful_values_need_review() -> None:
    assert _status() == "extracted"
    assert _status(confidence=0.4) == "needs_review"
    assert _status(confidence=None) == "needs_review"
    assert _status(problems=["-1 is negative"]) == "needs_review"


def test_status_absence_and_failure() -> None:
    assert _status(read_status="not_found") == "not_found"
    assert _status(read_status="not_found", required=True) == "needs_review"
    assert _status(read_status="failed") == "failed"


def _derived(tag: str, inputs: list[object], method: str = "sum"):
    return field_from_mapping({"name": tag, "tag": tag, "origin": "derived",
                               "derivation": {"method": method, "inputs": inputs}},
                              field_id=tag)


def test_a_derived_field_sums_its_inputs_per_period() -> None:
    fields = [_f(tag="A"), _f(tag="B"), _derived("T", ["A", "B"])]
    out = compute_derived(fields, {"A": {"current": 10, "prior": 8},
                                   "B": {"current": 5, "prior": 2}})
    assert out["T"].value == {"current": 15.0, "prior": 10.0}
    assert out["T"].missing == ()


def test_weights_diff_and_nested_derivations() -> None:
    fields = [
        _f(tag="A"), _f(tag="B"),
        _derived("W", [{"tag": "A", "weight": 1}, {"tag": "B", "weight": -1}]),
        _derived("D", ["A", "B"], method="diff"),
        _derived("N", ["W", "A"]),
    ]
    out = compute_derived(fields, {"A": 10, "B": 4})
    assert out["W"].value == 6.0
    assert out["D"].value == 6.0
    assert out["N"].value == 16.0


def test_a_missing_input_leaves_the_total_uncomputed() -> None:
    fields = [_f(tag="A"), _f(tag="B"), _derived("T", ["A", "B"])]
    out = compute_derived(fields, {"A": 10, "B": None})
    assert out["T"].value is None
    assert out["T"].missing == ("B",)


def test_a_cycle_or_an_empty_formula_is_never_computed() -> None:
    fields = [_derived("X", ["Y"]), _derived("Y", ["X"]), _derived("E", [])]
    out = compute_derived(fields, {})
    assert out["X"].value is None and out["Y"].value is None and out["E"].value is None
