import pytest

from shop.pricing import add_tax, apply_discount, order_total


def test_no_discount_keeps_price():
    assert apply_discount(100, 0) == 100


def test_quarter_off():
    assert apply_discount(100, 0.25) == 75


def test_discount_must_be_a_fraction():
    with pytest.raises(ValueError):
        apply_discount(100, 1.5)


def test_tax_is_added():
    assert add_tax(100) == 120


def test_order_total_without_discount():
    assert order_total([60, 40]) == 120


def test_order_total_with_discount():
    assert order_total([60, 40], discount=0.25) == 90
