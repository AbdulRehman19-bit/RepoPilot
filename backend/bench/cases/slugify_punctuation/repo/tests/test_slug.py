from textkit.slug import slugify


def test_simple_title():
    assert slugify("Hello World") == "hello-world"


def test_extra_spaces_collapse():
    assert slugify("  Multiple   spaces ") == "multiple-spaces"


def test_existing_hyphens_are_kept():
    assert slugify("Already-slugged") == "already-slugged"


def test_punctuation_is_removed():
    assert slugify("Hello, World!") == "hello-world"


def test_apostrophes_and_question_marks():
    assert slugify("What's new?") == "whats-new"
