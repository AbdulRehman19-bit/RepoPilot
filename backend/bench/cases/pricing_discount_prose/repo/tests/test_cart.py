from shop.cart import Cart


def test_cart_counts_quantities():
    cart = Cart()
    cart.add("pen", 2, quantity=3)
    cart.add("book", 10)
    assert cart.count() == 4
    assert cart.line_prices() == [6, 10]
