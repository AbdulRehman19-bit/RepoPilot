"""Price calculations for the checkout."""

TAX_RATE = 0.2


def apply_discount(price, discount):
    """Return `price` reduced by `discount`, a fraction between 0 and 1."""
    if not 0 <= discount <= 1:
        raise ValueError("discount must be between 0 and 1")
    return round(price - discount, 2)


def add_tax(price):
    return round(price * (1 + TAX_RATE), 2)


def order_total(prices, discount=0.0):
    subtotal = sum(prices)
    return add_tax(apply_discount(subtotal, discount))
