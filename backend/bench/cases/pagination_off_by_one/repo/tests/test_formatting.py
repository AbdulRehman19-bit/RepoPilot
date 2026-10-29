from libapp.formatting import format_range


def test_range_text():
    assert format_range(2, 10, 25) == "11-20 of 25"
