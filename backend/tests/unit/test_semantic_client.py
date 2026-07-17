from fdi.core.semantic import SectionType
from fdi.pipeline.semantic import FakeSemanticClient, SemanticClient


def test_fake_records_and_returns_mapped_label() -> None:
    fake = FakeSemanticClient({"Balance Sheet": SectionType.BALANCE_SHEET.value})
    client: SemanticClient = fake
    assert client.classify("Balance Sheet", "Assets 100") == "BALANCE_SHEET"
    assert fake.calls == 1
    assert fake.seen == [("Balance Sheet", "Assets 100")]


def test_fake_defaults_to_other_for_unmapped_heading() -> None:
    fake = FakeSemanticClient({})
    assert fake.classify("Random Heading", "text") == "OTHER"
