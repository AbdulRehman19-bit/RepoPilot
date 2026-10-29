"""Low-level helpers for cleaning numeric text."""


def clean_number(text):
    """Strip currency symbols and whitespace from numeric text."""
    return text.replace("$", "").replace(" ", "")
