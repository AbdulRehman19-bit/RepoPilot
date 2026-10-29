from inventory.tags import add_tag, has_tag


def test_tag_is_added():
    entry = add_tag("shirt", "red")
    assert entry == {"item": "shirt", "tags": ["red"]}
    assert has_tag(entry, "red")


def test_existing_tags_are_extended():
    assert add_tag("hat", "blue", ["wool"])["tags"] == ["wool", "blue"]


def test_tags_do_not_leak_between_items():
    add_tag("shirt", "red")
    assert add_tag("hat", "blue")["tags"] == ["blue"]


def test_input_list_is_not_modified():
    existing = ["wool"]
    add_tag("hat", "blue", existing)
    assert existing == ["wool"]
