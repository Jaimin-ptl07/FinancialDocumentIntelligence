from fdi.pipeline.candidates.uppercase import UppercaseCandidate, toy_dataset


def test_uppercase_predicts() -> None:
    assert UppercaseCandidate().predict("abc") == "ABC"
    assert UppercaseCandidate().name == "uppercase"


def test_toy_dataset_shape() -> None:
    ds = toy_dataset()
    assert ds.name == "toy" and len(ds.cases) >= 2
