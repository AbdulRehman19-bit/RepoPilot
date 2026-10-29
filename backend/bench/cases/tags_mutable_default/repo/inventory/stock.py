"""Stock counting for catalogue items."""


def total_stock(levels):
    return sum(levels.values())


def low_stock(levels, threshold=5):
    return sorted(name for name, count in levels.items() if count < threshold)
