from textkit.case import snake_case, title_case


def test_cases():
    assert title_case("hello there") == "Hello There"
    assert snake_case("Hello There") == "hello_there"
