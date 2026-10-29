import pytest
from fastapi.testclient import TestClient

from app.indexing.scanner import extract_symbols
from app.main import app
from app.models import IndexedFile, Issue, Repository, Symbol, User
from app.retrieval.ranker import rank_files
from app.retrieval.signals import PathRef, extract_signals

CONFIG_PY = """
class ConfigParser:
    def load(self, path):
        return self.parse_config(open(path).read())

    def parse_config(self, text):
        if not text:
            raise ValueError("empty config")
        return dict(line.split("=") for line in text.splitlines())
"""
BILLING_PY = '''
def compute_invoice_total(items):
    """Sum the line items and apply tax."""
    return sum(item.price for item in items) * 1.2
'''
README = "# Demo\n\nA tiny project that parses configuration and prints invoices.\n"

TRACEBACK = """Crash when loading an empty file

Traceback (most recent call last):
  File "/home/user/proj/src/app/config.py", line 4, in load
    return self.parse_config(open(path).read())
  File "/home/user/proj/src/app/config.py", line 7, in parse_config
    raise ValueError("empty config")
ValueError: empty config
"""


def test_signals_from_traceback():
    signals = extract_signals(TRACEBACK)
    assert PathRef("/home/user/proj/src/app/config.py", 4, frame=True) in signals.paths
    assert {"load", "parse_config"} <= signals.identifiers
    assert "ValueError" in signals.error_types
    assert "empty" in signals.words
    assert "the" not in signals.words


def test_signals_from_prose():
    text = (
        "Calling `Greeter.hello()` fails and compute_invoice_total returns the wrong value. "
        "See src/app/billing.py:12 and docs/README.md. Raises KeyError sometimes (e.g. never)."
    )
    signals = extract_signals(text)
    assert {"Greeter.hello", "Greeter", "hello", "compute_invoice_total"} <= signals.identifiers
    assert PathRef("src/app/billing.py", 12) in signals.paths
    assert PathRef("docs/README.md", None) in signals.paths
    assert signals.error_types == {"KeyError"}


def test_signals_normalize_windows_paths():
    signals = extract_signals(r'File "C:\proj\src\app\config.py", line 3, in <module>')
    assert signals.paths == [PathRef("C:/proj/src/app/config.py", 3, frame=True)]
    assert "<module>" not in signals.identifiers


@pytest.fixture
def indexed_repo(db_session):
    owner = User(github_id=666_001, login="r")
    repo = Repository(owner=owner, github_id=666_002, full_name="r/demo", index_status="ready")
    db_session.add(repo)
    db_session.flush()

    for path, language, content in [
        ("src/app/config.py", "python", CONFIG_PY),
        ("src/app/billing.py", "python", BILLING_PY),
        ("README.md", "text", README),
    ]:
        db_session.add(
            IndexedFile(
                repository_id=repo.id,
                path=path,
                language=language,
                size=len(content),
                line_count=content.count("\n"),
                content=content,
                symbols=[
                    Symbol(
                        name=s.name,
                        qualname=s.qualname,
                        kind=s.kind,
                        start_line=s.start_line,
                        end_line=s.end_line,
                        docstring=s.docstring,
                    )
                    for s in (extract_symbols(content) if language == "python" else [])
                ],
            )
        )
    db_session.flush()
    return repo


def test_traceback_ranks_named_file_first(db_session, indexed_repo):
    matches = rank_files(db_session, indexed_repo.id, TRACEBACK)
    top = matches[0]
    assert top.path == "src/app/config.py"
    assert any("named in the issue" in r for r in top.reasons)
    assert any("line 7 is inside `ConfigParser.parse_config`" in r for r in top.reasons)
    assert any(f.label == "ConfigParser.parse_config" for f in top.focus)


def test_symbol_mention_without_a_path(db_session, indexed_repo):
    text = "compute_invoice_total gives the wrong amount for empty orders"
    matches = rank_files(db_session, indexed_repo.id, text)
    assert matches[0].path == "src/app/billing.py"
    assert any("defines `compute_invoice_total`" in r for r in matches[0].reasons)


def test_full_text_only_issue(db_session, indexed_repo):
    matches = rank_files(
        db_session, indexed_repo.id, "Something wrong with invoice tax calculation"
    )
    assert matches[0].path == "src/app/billing.py"
    assert all("text match" in reason for reason in matches[0].reasons)


def test_camel_case_identifier_matches_text(db_session, indexed_repo):
    matches = rank_files(db_session, indexed_repo.id, "ConfigParser is broken")
    assert matches[0].path == "src/app/config.py"


def test_no_signals_returns_nothing(db_session, indexed_repo):
    assert rank_files(db_session, indexed_repo.id, "it is a bug") == []


def test_limit_and_other_repositories_are_isolated(db_session, indexed_repo):
    other = Repository(
        owner=indexed_repo.owner, github_id=666_003, full_name="r/other", index_status="ready"
    )
    db_session.add(other)
    db_session.flush()
    db_session.add(
        IndexedFile(
            repository_id=other.id,
            path="other.py",
            language="python",
            size=1,
            line_count=1,
            content="def compute_invoice_total(): pass",
        )
    )
    db_session.flush()

    matches = rank_files(db_session, indexed_repo.id, "compute_invoice_total fails", limit=1)
    assert len(matches) == 1
    assert matches[0].path != "other.py"


def test_retrieve_endpoint(db_session, indexed_repo):
    issue = Issue(repository=indexed_repo, number=7, title="Crash", body=TRACEBACK)
    db_session.add(issue)
    db_session.flush()
    client = TestClient(app)

    response = client.post(f"/issues/{issue.id}/retrieve")
    assert response.status_code == 200
    body = response.json()
    assert body["matches"][0]["path"] == "src/app/config.py"
    assert "ValueError" in body["signals"]["error_types"]
    assert any(i["id"] == issue.id for i in client.get("/issues").json())

    assert client.post("/issues/999999999/retrieve").status_code == 404
    indexed_repo.index_status = "pending"
    db_session.flush()
    assert client.post(f"/issues/{issue.id}/retrieve").status_code == 409


def test_file_named_repeatedly_is_scored_once(db_session, indexed_repo):
    text = "see src/app/config.py:4 and src/app/config.py:7"
    top = rank_files(db_session, indexed_repo.id, text)[0]
    assert top.path == "src/app/config.py"
    # One path mention (10) + two lines inside symbols (3 each) + full-text bonus (3) = 19.
    # Counting the path per mention instead of per file would give 29.
    assert top.score == 19.0


# --- Innermost traceback frame and following calls -----------------------------------------------


def add_file(session, repo, path, content):
    session.add(
        IndexedFile(
            repository_id=repo.id,
            path=path,
            language="python",
            size=len(content),
            line_count=content.count("\n"),
            content=content,
            symbols=[
                Symbol(
                    name=s.name,
                    qualname=s.qualname,
                    kind=s.kind,
                    start_line=s.start_line,
                    end_line=s.end_line,
                    docstring=s.docstring,
                )
                for s in extract_symbols(content)
            ],
        )
    )
    session.flush()


def reasons(matches, path):
    return next(m.reasons for m in matches if m.path == path)


def test_innermost_project_frame_gets_a_bonus(db_session, indexed_repo):
    text = (
        "Traceback (most recent call last):\n"
        '  File "src/app/billing.py", line 3, in compute_invoice_total\n'
        '  File "src/app/config.py", line 7, in parse_config\n'
        "ValueError: boom"
    )
    matches = rank_files(db_session, indexed_repo.id, text)
    assert any("innermost" in r for r in reasons(matches, "src/app/config.py"))
    assert not any("innermost" in r for r in reasons(matches, "src/app/billing.py"))


def test_no_innermost_bonus_when_only_one_project_file_is_in_the_traceback(
    db_session, indexed_repo
):
    text = (
        "Traceback (most recent call last):\n"
        '  File "src/app/config.py", line 4, in load\n'
        '  File "src/app/config.py", line 7, in parse_config\n'
        '  File "/usr/lib/python3.11/json/decoder.py", line 355, in raw_decode\n'
        "ValueError: boom"
    )
    matches = rank_files(db_session, indexed_repo.id, text)
    assert not any("innermost" in r for r in reasons(matches, "src/app/config.py"))


CALLER = "def run_job():\n    return helper_fn(1)\n\n\ndef other():\n    return unrelated_fn()\n"
HELPER = "def helper_fn(x):\n    return x\n"


def test_files_defining_called_functions_are_credited(db_session, indexed_repo):
    add_file(db_session, indexed_repo, "src/jobs.py", CALLER)
    add_file(db_session, indexed_repo, "src/helpers.py", HELPER)
    add_file(db_session, indexed_repo, "src/misc.py", "def unrelated_fn():\n    return 0\n")

    matches = rank_files(db_session, indexed_repo.id, "`run_job` in src/jobs.py misbehaves")
    assert any("called from src/jobs.py" in r for r in reasons(matches, "src/helpers.py"))
    # Only the function the issue is about is followed, not the rest of the file.
    assert "src/misc.py" not in {m.path for m in matches}


def test_ambiguous_callee_names_are_not_followed(db_session, indexed_repo):
    add_file(db_session, indexed_repo, "src/jobs.py", CALLER)
    for name in ("a", "b", "c"):  # defined in three files: says nothing about any one of them
        add_file(db_session, indexed_repo, f"src/{name}.py", HELPER)

    matches = rank_files(db_session, indexed_repo.id, "`run_job` in src/jobs.py misbehaves")
    assert not any(m.path in {"src/a.py", "src/b.py", "src/c.py"} for m in matches)
