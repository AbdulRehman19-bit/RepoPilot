import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.indexing.indexer import index_repository
from app.indexing.scanner import extract_symbols, scan_repository
from app.indexing.workspace import GitError, clone_repository
from app.models import IndexedFile, Repository, Symbol, User

SOURCE = '''
"""Module doc."""


def top(a):
    """Top-level function."""
    return a


class Greeter:
    """Says hello."""

    def hello(self):
        return "hi"

    async def later(self):
        def inner():
            pass
        return inner

    class Nested:
        def deep(self):
            pass
'''


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text(SOURCE, encoding="utf-8")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    (repo / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    (repo / "image.bin").write_bytes(b"\x00\x01\x02")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "skip.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "init", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


def test_extract_symbols():
    symbols = {s.qualname: s for s in extract_symbols(SOURCE)}
    assert symbols["top"].kind == "function"
    assert symbols["top"].docstring == "Top-level function."
    assert symbols["Greeter"].kind == "class"
    assert symbols["Greeter.hello"].kind == "method"
    assert symbols["Greeter.later"].kind == "method"
    assert symbols["Greeter.later.inner"].kind == "function"
    assert symbols["Greeter.Nested.deep"].kind == "method"
    assert symbols["Greeter.hello"].start_line < symbols["Greeter.hello"].end_line + 1


def test_extract_symbols_ignores_syntax_errors():
    assert extract_symbols("def oops(:\n") == []


def test_clone_and_scan(source_repo, tmp_path):
    dest = tmp_path / "work" / "clone"
    sha = clone_repository(str(source_repo), dest)
    assert len(sha) == 40

    paths = {f.path for f in scan_repository(dest)}
    assert paths == {"pkg/core.py", "README.md", "broken.py"}  # no .git, binary, or node_modules


def test_clone_refuses_non_empty_destination(source_repo, tmp_path):
    (tmp_path / "occupied").mkdir()
    (tmp_path / "occupied" / "file").write_text("x")
    with pytest.raises(GitError):
        clone_repository(str(source_repo), tmp_path / "occupied")


def test_clone_error_hides_token(tmp_path):
    with pytest.raises(GitError) as excinfo:
        clone_repository(str(tmp_path / "does-not-exist"), tmp_path / "dest", token="ghs_secret")
    assert "ghs_secret" not in str(excinfo.value)


def test_index_repository_replaces_previous_index(source_repo, tmp_path, db_session):
    owner = User(github_id=888_001, login="idx")
    repo = Repository(owner=owner, github_id=888_002, full_name="idx/demo")
    db_session.add(repo)
    db_session.flush()

    dest = tmp_path / "clone"
    sha = clone_repository(str(source_repo), dest)
    assert index_repository(db_session, repo, dest, sha) == 3
    assert repo.indexed_commit == sha

    core = db_session.scalar(select(IndexedFile).where(IndexedFile.path == "pkg/core.py"))
    assert core.language == "python"
    names = {s.qualname for s in core.symbols}
    assert {"top", "Greeter", "Greeter.hello"} <= names

    # Re-indexing replaces rows rather than duplicating them.
    (dest / "README.md").unlink()
    assert index_repository(db_session, repo, dest, sha) == 2
    db_session.expire_all()
    files = db_session.scalars(
        select(IndexedFile).where(IndexedFile.repository_id == repo.id)
    ).all()
    assert len(files) == 2
    assert db_session.scalars(select(Symbol).where(Symbol.file_id.in_([f.id for f in files]))).all()
