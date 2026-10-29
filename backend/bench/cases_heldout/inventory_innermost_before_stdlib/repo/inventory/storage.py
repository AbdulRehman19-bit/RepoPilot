import json


def parse_stock(text):
    """Parse a stock file into {sku: quantity}."""
    data = json.loads(text)
    return {item["sku"]: item["qty"] for item in data["items"]}
