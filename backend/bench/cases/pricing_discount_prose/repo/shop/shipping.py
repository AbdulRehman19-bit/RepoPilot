"""Shipping cost rules."""

FREE_SHIPPING_FROM = 50


def shipping_cost(basket_total):
    """Orders at or above the threshold ship for free, others pay a flat price."""
    return 0 if basket_total >= FREE_SHIPPING_FROM else 4.99
