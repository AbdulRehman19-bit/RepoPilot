from inventory.storage import parse_stock


def test_parses_items():
    text = '{"items": [{"sku": "A1", "qty": 3}, {"sku": "B2", "qty": 0}]}'
    assert parse_stock(text) == {"A1": 3, "B2": 0}


def test_empty_file_means_empty_stock():
    assert parse_stock("") == {}
    assert parse_stock("  \n") == {}
