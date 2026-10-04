from sieve.autothrottle import AutoThrottle
from concurrent.futures import ThreadPoolExecutor


def test_block_response_honors_retry_after_and_caps_delay():
    throttle = AutoThrottle(min_delay=1, max_delay=10)
    throttle.observe("example.test", latency=0.1, status=429, retry_after=7)

    assert throttle.get_delay("example.test") == 7


def test_delay_recovers_toward_latency_after_healthy_response():
    throttle = AutoThrottle(min_delay=1, max_delay=10)
    throttle.observe("example.test", latency=0.1, status=429, retry_after=7)
    throttle.observe("example.test", latency=1, status=200)

    assert 1 < throttle.get_delay("example.test") < 7


def test_minimum_maximum_and_403_backoff_are_domain_local():
    throttle = AutoThrottle(min_delay=1, max_delay=8)
    throttle.observe("blocked.test", latency=0, status=403)
    assert throttle.get_delay("blocked.test") == 2
    assert throttle.get_delay("healthy.test") == 1
    throttle.observe("blocked.test", latency=100, status=200)
    assert throttle.get_delay("blocked.test") == 8
    throttle.observe("healthy.test", latency=-10, status=200)
    assert throttle.get_delay("healthy.test") == 1
    throttle.observe("blocked.test", latency=0, status=429, retry_after=100)
    assert throttle.get_delay("blocked.test") == 8


def test_reset_discards_all_learned_domains():
    throttle = AutoThrottle(min_delay=1, max_delay=8)
    for domain in ("a.test", "b.test"):
        throttle.observe(domain, latency=4, status=200)
    throttle.reset()
    assert [throttle.get_delay(domain) for domain in ("a.test", "b.test")] == [1, 1]


def test_concurrent_block_observations_are_atomic_and_bounded():
    throttle = AutoThrottle(min_delay=1, max_delay=1024)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: throttle.observe("a.test", 0, 403), range(8)))
    assert throttle.get_delay("a.test") == 256
    assert throttle.get_delay("b.test") == 1
