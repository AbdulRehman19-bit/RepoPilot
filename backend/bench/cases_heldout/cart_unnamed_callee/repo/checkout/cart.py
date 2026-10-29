from checkout.rules import percent_off


class Cart:
    def __init__(self):
        self.items = []
        self.coupon = None
        self.member = False

    def add(self, price):
        self.items.append(price)

    def total(self):
        subtotal = sum(self.items)
        if self.coupon:
            subtotal = percent_off(subtotal, self.coupon)
        if self.member:
            subtotal = percent_off(subtotal, 5)
        return round(subtotal, 2)
