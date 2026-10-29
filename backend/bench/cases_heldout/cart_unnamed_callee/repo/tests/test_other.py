from checkout.receipts import receipt_line
from checkout.shipping import shipping_cost
from checkout.tax import add_tax


def test_shipping():
    assert shipping_cost(1) == 5.0


def test_tax():
    assert add_tax(100) == 120.0


def test_receipt():
    assert receipt_line("tea", 2.5).endswith("2.50")
