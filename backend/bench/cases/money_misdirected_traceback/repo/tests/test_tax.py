from billing.tax import tax_for


def test_tax():
    assert tax_for(100) == 20.0
