from fdi.core.metrics import ExactMatch, exact_match


def test_exact_match_scores() -> None:
    assert exact_match("a", "a") == 1.0
    assert exact_match("a", "b") == 0.0
    assert exact_match({"x": 1}, {"x": 1}) == 1.0


def test_exact_match_metric_object() -> None:
    m = ExactMatch()
    assert m.name == "exact_match"
    assert m.score(1, 1) == 1.0 and m.score(1, 2) == 0.0
