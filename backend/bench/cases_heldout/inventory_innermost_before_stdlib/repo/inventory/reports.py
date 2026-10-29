def low_stock(stock, threshold=5):
    return sorted(sku for sku, qty in stock.items() if qty < threshold)
