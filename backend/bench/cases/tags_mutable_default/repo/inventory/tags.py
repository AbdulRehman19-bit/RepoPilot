"""Tagging helpers for catalogue items."""


def add_tag(item, tag, tags=[]):
    """Return a new tag list for `item` including `tag`."""
    tags.append(tag)
    return {"item": item, "tags": tags}


def has_tag(entry, tag):
    return tag in entry["tags"]
