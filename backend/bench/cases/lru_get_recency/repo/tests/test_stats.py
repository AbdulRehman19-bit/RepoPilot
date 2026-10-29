from cachekit.stats import Stats


def test_ratio():
    stats = Stats()
    stats.record(True)
    stats.record(False)
    assert stats.ratio() == 0.5
