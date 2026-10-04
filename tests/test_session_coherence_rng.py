"""Issue #237: injectable RNG for deterministic session-coherence tests.

Timing choices in session_coherence are behavioral camouflage, not security
material, so a seeded random.Random is an acceptable injection point.
Unpredictability-sensitive identifiers keep secrets/SystemRandom by contract
(profile_identity already defaults to SystemRandom when no seed is given).
"""

import random

from sieve.session_coherence import current_rng, human_delay, set_rng, warmup_sequence
from sieve.session_coherence import rng_scope


def test_concurrent_seeded_clients_keep_independent_behavior_streams():
    import asyncio
    from sieve.sdk import SieveClient
    from sieve.browser import _generate_fingerprint_profile
    from sieve.search_metasearch import _google_ua

    class Backend:
        async def smart_fetch(self, **kwargs):
            await asyncio.sleep(0)
            return human_delay(), _generate_fingerprint_profile(), _google_ua()

    async def run(perturb=False):
        first = SieveClient(Backend(), rng=random.Random(42))
        second = SieveClient(Backend(), rng=random.Random(42))
        if perturb:
            await first.fetch("https://example.test/extra")
        return await asyncio.gather(first.fetch("https://example.test/one"), second.fetch("https://example.test/two"))

    initial = asyncio.run(run())
    assert initial[0] == initial[1]
    changed = asyncio.run(run(perturb=True))
    assert changed[1] == initial[1]
    assert changed[0] != initial[0]


def test_behavior_scope_does_not_replace_security_randomness():
    from sieve import search_metasearch
    secure_source = search_metasearch.random
    with rng_scope(random.Random(42)):
        assert isinstance(current_rng(), random.Random)
        assert search_metasearch.random is secure_source
        assert isinstance(secure_source, random.SystemRandom)


def test_human_delay_with_seeded_rng_is_reproducible():
    a = human_delay(rng=random.Random(42))
    b = human_delay(rng=random.Random(42))
    assert a == b
    assert a > 0


def test_human_delay_explicit_rng_beats_module_rng():
    set_rng(random.Random(7))
    try:
        module_draw = human_delay()
        explicit_draw = human_delay(rng=random.Random(7))
        assert explicit_draw == human_delay(rng=random.Random(7))
        # Independent streams may differ; both stay in the valid band.
        assert 0 < module_draw < 9
    finally:
        set_rng(None)


def test_set_rng_none_restores_ambient():
    set_rng(random.Random(1))
    set_rng(None)
    assert current_rng() is random


def test_warmup_sequence_seeded_rng_is_reproducible():
    url = "https://example.test/catalog/items/42"
    a = warmup_sequence(url, steps=3, rng=random.Random(99))
    b = warmup_sequence(url, steps=3, rng=random.Random(99))
    assert a == b
    assert [s["kind"] for s in a] == ["home", "category", "target"]
    assert a[-1]["url"] == url
    assert all(s["pause_ms"] >= 0 for s in a)


def test_human_delay_injected_rng_honors_bounds():
    # Force the ordinary-pause branch: a Random stream that never draws < 0.10
    # on the first call keeps us out of the long-pause window deterministically.
    class FixedStream(random.Random):
        def random(self):
            return 0.5

    d = human_delay(min_ms=100, max_ms=200, rng=FixedStream())
    assert 0.1 <= d <= 0.2
