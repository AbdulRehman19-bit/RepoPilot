from inventory.report import render


def test_render_lists_tags():
    entries = [{"item": "hat", "tags": ["blue"]}, {"item": "cap", "tags": []}]
    assert render(entries) == "hat: blue\ncap: no tags"
