from cachekit.ttl import TTLCache


def test_entries_expire():
    now = [0]
    cache = TTLCache(10, clock=lambda: now[0])
    cache.put("a", 1)
    assert cache.get("a") == 1
    now[0] = 11
    assert cache.get("a") is None
