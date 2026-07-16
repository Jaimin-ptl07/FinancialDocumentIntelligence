from fdi.core.eval_types import Dataset, EvalCase, dataset_from_dict, dataset_to_dict


def test_dataset_roundtrips_through_dict() -> None:
    ds = Dataset(name="toy", version=1, cases=(
        EvalCase("c1", given="a", expected="A"),
        EvalCase("c2", given="b", expected="B"),
    ))
    d = dataset_to_dict(ds)
    assert d == {"name": "toy", "version": 1, "cases": [
        {"case_id": "c1", "given": "a", "expected": "A"},
        {"case_id": "c2", "given": "b", "expected": "B"},
    ]}
    assert dataset_from_dict(d) == ds
