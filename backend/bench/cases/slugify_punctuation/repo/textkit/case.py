"""Case conversion helpers."""


def title_case(text):
    return " ".join(word.capitalize() for word in text.split())


def snake_case(text):
    return "_".join(text.lower().split())
