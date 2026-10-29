import pytest

from libapp.pagination import page_count, paginate

ITEMS = list(range(1, 26))


def test_first_page():
    assert paginate(ITEMS, 1, 10) == list(range(1, 11))


def test_last_page_is_partial():
    assert paginate(ITEMS, 3, 10) == [21, 22, 23, 24, 25]


def test_page_count_rounds_up():
    assert page_count(25, 10) == 3


def test_page_must_be_positive():
    with pytest.raises(ValueError):
        paginate(ITEMS, 0, 10)
