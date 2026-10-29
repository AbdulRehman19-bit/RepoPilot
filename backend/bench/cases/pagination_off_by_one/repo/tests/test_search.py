from libapp.models import Book
from libapp.search import search_titles


def test_search_is_case_insensitive():
    books = [Book("Dune", "Herbert", 1965), Book("Emma", "Austen", 1815)]
    assert [b.title for b in search_titles(books, "dUN")] == ["Dune"]
