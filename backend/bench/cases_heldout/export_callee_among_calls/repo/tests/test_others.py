from tools.jsonfmt import to_json_line
from tools.report import summarize


def test_json():
    assert to_json_line([1, "a"]) == '[1, "a"]'


def test_summary():
    assert summarize([1, 2]) == {"count": 2}
