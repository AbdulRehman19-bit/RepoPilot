"""Invoices made of priced lines."""

from billing.money import parse_amount


class Invoice:
    def __init__(self):
        self.lines = []

    def add_line(self, description, raw):
        amount = parse_amount(raw)
        self.lines.append((description, amount))

    def total(self):
        return round(sum(amount for _, amount in self.lines), 2)
