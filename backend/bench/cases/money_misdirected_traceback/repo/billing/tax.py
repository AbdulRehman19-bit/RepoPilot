"""Sales tax."""


def tax_for(amount, rate=0.2):
    return round(amount * rate, 2)
