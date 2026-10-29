"""Search over book titles."""


def search_titles(books, term):
    term = term.lower()
    return [book for book in books if term in book.title.lower()]
