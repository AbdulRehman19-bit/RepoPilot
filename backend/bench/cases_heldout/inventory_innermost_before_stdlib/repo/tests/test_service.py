from inventory.service import load_stock


def test_loads_a_stock_file(tmp_path):
    path = tmp_path / "stock.json"
    path.write_text('{"items": [{"sku": "A1", "qty": 2}]}')
    assert load_stock(path) == {"A1": 2}
