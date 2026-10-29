"""Helpers for splitting long lists into pages."""


def page_count(total_items, per_page):
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    return (total_items + per_page - 1) // per_page


def paginate(items, page, per_page):
    """Return the items on a 1-based page."""
    if page < 1:
        raise ValueError("page must be at least 1")
    start = page * per_page
    end = start + per_page
    return list(items[start:end])
