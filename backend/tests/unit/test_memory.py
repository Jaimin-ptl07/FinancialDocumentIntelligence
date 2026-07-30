"""Mapping memory: reuse a resolution, but only where reuse is safe."""

from fdi.core.memory import (
    InMemoryStore,
    MemoryKey,
    confirm,
    observe,
    promotable_aliases,
    recall,
    stats,
)


def test_context_is_part_of_the_key_so_a_label_cannot_leak_across_sections():
    """"Computer equipment" under PPE is a fixed asset; the same words under
    intangibles are not. A label-only key would carry the wrong answer."""
    store = InMemoryStore()
    confirm(store, MemoryKey.of("Computer equipment", "ppe"), "GRSPNE")

    assert recall(store, MemoryKey.of("Computer equipment", "ppe")) is not None
    assert recall(store, MemoryKey.of("Computer equipment", "intangibles")) is None


def test_an_unconfirmed_observation_is_not_yet_trusted():
    store = InMemoryStore()
    observe(store, MemoryKey.of("Rent income"), "RENTOI",
            confidence=0.9, method="semantic")

    assert recall(store, MemoryKey.of("Rent income")) is None  # seen, not confirmed
    confirm(store, MemoryKey.of("Rent income"), "RENTOI")
    assert recall(store, MemoryKey.of("Rent income")) is not None


def test_low_confidence_never_becomes_trusted_by_repetition():
    store = InMemoryStore()
    key = MemoryKey.of("Sundry balances")
    for _ in range(5):
        observe(store, key, "OTEXP", confidence=0.2, method="semantic",
                confirmed=True)

    assert recall(store, key) is None  # repetition is not evidence


def test_a_disagreeing_observation_resets_rather_than_averages():
    store = InMemoryStore()
    key = MemoryKey.of("Other equipment", "ppe")
    confirm(store, key, "GRSPNE")
    confirm(store, key, "GRSPNE")
    assert store.get(key).times_confirmed == 2

    entry = observe(store, key, "GRSOTTA", confidence=0.8, method="semantic",
                    confirmed=True)
    assert entry.tag == "GRSOTTA"
    assert entry.times_confirmed == 1  # the disagreement is not merged away


def test_settled_mappings_are_promotable_into_the_schema_aliases():
    store = InMemoryStore()
    key = MemoryKey.of("Plant and machinery", "ppe")
    for _ in range(3):
        confirm(store, key, "GRSPNE")

    assert promotable_aliases(store) == {"GRSPNE": ["plant and machinery"]}
    # one confirmation short of settled promotes nothing
    fresh = InMemoryStore()
    confirm(fresh, MemoryKey.of("Odd wording"), "OTEXP")
    assert promotable_aliases(fresh) == {}


def test_stats_report_coverage_rather_than_hiding_it():
    store = InMemoryStore()
    confirm(store, MemoryKey.of("Rent income"), "RENTOI")
    observe(store, MemoryKey.of("Mystery line"), "OTEXP",
            confidence=0.3, method="semantic")

    assert stats(store) == {"entries": 2, "trusted": 1, "settled": 0, "tags": 2}


def test_a_broken_store_degrades_to_no_memory_and_never_fails_extraction():
    class Broken:
        def get(self, key): raise RuntimeError("db down")
        def put(self, entry): raise RuntimeError("db down")
        def all(self): raise RuntimeError("db down")

    broken = Broken()
    assert recall(broken, MemoryKey.of("x")) is None
    assert observe(broken, MemoryKey.of("x"), "T", confidence=1.0,
                   method="semantic").tag == "T"
    assert promotable_aliases(broken) == {}
    assert stats(broken)["entries"] == 0
