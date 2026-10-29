from checkout.cart import Cart


def make(*prices):
    cart = Cart()
    for price in prices:
        cart.add(price)
    return cart


def test_plain_total():
    assert make(100.0, 20.0).total() == 120.0


def test_coupon_only():
    cart = make(200.0)
    cart.coupon = 10
    assert cart.total() == 180.0


def test_coupon_and_member_prices_combine():
    cart = make(100.0)
    cart.coupon = 10
    cart.member = True
    assert cart.total() == 85.5
