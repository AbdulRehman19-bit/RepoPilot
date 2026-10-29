"""Parse and format money amounts."""

from billing.numbers import clean_number


def parse_amount(text):
    """Parse a user-entered amount such as '$1,299.50' into a float."""
    cleaned = clean_number(text)
    return float(cleaned)


def format_amount(value):
    return f"${value:,.2f}"
