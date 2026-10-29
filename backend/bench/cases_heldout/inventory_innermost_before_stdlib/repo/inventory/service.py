from inventory.storage import parse_stock


def load_stock(path):
    with open(path) as handle:
        text = handle.read()
    return parse_stock(text)
