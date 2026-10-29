from inventory.reports import low_stock
from inventory.skus import normalize_sku


def test_normalize():
    assert normalize_sku(" ab1 ") == "AB1"


def test_low_stock():
    assert low_stock({"a": 1, "b": 9}) == ["a"]
