"""Counting words and characters."""


def word_count(text):
    return len(text.split())


def char_count(text, ignore_spaces=False):
    return len(text.replace(" ", "")) if ignore_spaces else len(text)
