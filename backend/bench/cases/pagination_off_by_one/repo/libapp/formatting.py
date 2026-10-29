"""Text shown above a list of results."""


def format_range(page, per_page, total):
    first = (page - 1) * per_page + 1
    last = min(page * per_page, total)
    return f"{first}-{last} of {total}"
