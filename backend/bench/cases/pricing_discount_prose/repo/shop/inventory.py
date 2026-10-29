"""Stock levels for the shop."""


def in_stock(stock, name):
    return stock.get(name, 0) > 0


def reserve(stock, name, quantity):
    if stock.get(name, 0) < quantity:
        raise ValueError("not enough stock")
    stock[name] -= quantity
