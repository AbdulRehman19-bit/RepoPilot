"""Plain-text catalogue reports."""


def render(entries):
    lines = []
    for entry in entries:
        tags = ", ".join(entry["tags"]) or "no tags"
        lines.append(f"{entry['item']}: {tags}")
    return "\n".join(lines)
