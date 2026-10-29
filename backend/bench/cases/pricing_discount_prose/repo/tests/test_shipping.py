from shop.shipping import shipping_cost


def test_free_shipping_threshold():
    assert shipping_cost(50) == 0
    assert shipping_cost(49.99) == 4.99
