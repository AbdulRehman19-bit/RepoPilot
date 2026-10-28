from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.indexing.scanner import scan_repository
from app.models import IndexedFile, Repository, Symbol


def index_repository(session: Session, repo: Repository, root: Path, commit_sha: str) -> int:
    """Replace the stored index for `repo` with a fresh scan of `root`. Returns the file count."""
    scanned = scan_repository(root)

    session.execute(delete(IndexedFile).where(IndexedFile.repository_id == repo.id))
    session.expire(repo, ["files"])

    for info in scanned:
        session.add(
            IndexedFile(
                repository_id=repo.id,
                path=info.path,
                language=info.language,
                size=info.size,
                line_count=info.line_count,
                content=info.content,
                symbols=[
                    Symbol(
                        name=s.name,
                        qualname=s.qualname,
                        kind=s.kind,
                        start_line=s.start_line,
                        end_line=s.end_line,
                        docstring=s.docstring,
                    )
                    for s in info.symbols
                ],
            )
        )
    repo.indexed_commit = commit_sha
    session.flush()
    return len(scanned)
