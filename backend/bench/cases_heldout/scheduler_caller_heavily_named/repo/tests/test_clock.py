from scheduler.clock import minutes_since_midnight


def test_clock():
    assert minutes_since_midnight(1, 30) == 90
