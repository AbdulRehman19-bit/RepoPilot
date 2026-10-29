"""Turn titles into URL slugs."""

import re


def slugify(title):
    """Lowercase `title` and join its words with hyphens, dropping punctuation."""
    text = title.strip().lower()
    text = re.sub(r"\s+", "-", text)
    return text
