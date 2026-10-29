"""The shopping basket."""


class Cart:
    def __init__(self):
        self.items = []

    def add(self, name, price, quantity=1):
        self.items.append({"name": name, "price": price, "quantity": quantity})

    def line_prices(self):
        return [item["price"] * item["quantity"] for item in self.items]

    def count(self):
        return sum(item["quantity"] for item in self.items)
