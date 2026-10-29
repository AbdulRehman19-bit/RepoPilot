from billing.money import format_amount, parse_amount


def test_parse_plain_number():
    assert parse_amount("12.50") == 12.5


def test_parse_with_currency_symbol():
    assert parse_amount("$ 40") == 40.0


def test_parse_with_thousands_separator():
    assert parse_amount("$1,299.50") == 1299.5


def test_format_adds_separators():
    assert format_amount(1299.5) == "$1,299.50"
