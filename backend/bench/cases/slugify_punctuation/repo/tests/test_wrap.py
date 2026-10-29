from textkit.wrap import wrap


def test_wrap_respects_width():
    assert wrap("the quick brown fox", 9) == ["the quick", "brown fox"]
