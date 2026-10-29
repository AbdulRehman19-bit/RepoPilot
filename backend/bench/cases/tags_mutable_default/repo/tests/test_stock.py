from inventory.stock import low_stock, total_stock


def test_stock_helpers():
    levels = {"a": 2, "b": 10}
    assert total_stock(levels) == 12
    assert low_stock(levels) == ["a"]
