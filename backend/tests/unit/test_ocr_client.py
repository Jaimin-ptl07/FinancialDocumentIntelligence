from fdi.pipeline.ocr import FakeOcrClient, OcrClient


def test_fake_records_and_returns() -> None:
    fake = FakeOcrClient({"content": "x"})
    client: OcrClient = fake
    assert client.analyze(b"PDF") == {"content": "x"}
    assert fake.calls == 1 and fake.last_pdf == b"PDF"
