"""The publishing seam.

`api` and `workers` are independent siblings, so the API cannot import the task
object to publish it. `StageDispatcher` is the seam that keeps them apart; this
test pins what it sends.
"""

from fdi.pipeline.dispatch import StageDispatcher


def test_stage_dispatcher_publishes_args_and_correlation_kwarg() -> None:
    """`StageDispatcher` publishes through `TaskSender`: args=[doc_id, stage_name],
    correlation_id as a kwarg -- nothing carries a contextvar across the broker."""
    calls = []

    class _Rec:
        def apply_async(self, args, kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            return object()

    StageDispatcher(_Rec()).dispatch("doc1", "ping", "cid-9")
    assert calls == [(["doc1", "ping"], {"correlation_id": "cid-9"})]
