from tools.csvfmt import to_csv_line


def test_plain_values():
    assert to_csv_line(["a", 2]) == "a,2"


def test_values_with_commas_and_quotes_are_quoted():
    assert to_csv_line(["a,b", 'say "hi"', 3]) == '"a,b","say ""hi""",3'
