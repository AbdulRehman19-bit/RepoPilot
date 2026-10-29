"""Load catalogue items from simple CSV text."""


def load_items(text):
    rows = [line.split(",") for line in text.strip().splitlines()]
    return [{"name": name.strip(), "price": float(price)} for name, price in rows]
