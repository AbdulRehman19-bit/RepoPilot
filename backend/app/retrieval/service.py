from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IndexedFile, Issue
from app.retrieval.context import ContextBlock, build_blocks
from app.retrieval.ranker import Match, rank_files
from app.retrieval.signals import Signals, extract_signals


@dataclass
class Investigation:
    issue: Issue
    signals: Signals
    matches: list[Match]
    blocks: list[ContextBlock]


def investigate(
    session: Session, issue: Issue, limit: int = 8, budget_tokens: int = 3000
) -> Investigation:
    """Rank the repository's files for an issue and pack the relevant code into a budget."""
    text = f"{issue.title}\n{issue.body}"
    signals = extract_signals(text)
    matches = rank_files(session, issue.repository_id, text, limit, signals=signals)
    contents = dict(
        session.execute(
            select(IndexedFile.id, IndexedFile.content).where(
                IndexedFile.id.in_([m.file_id for m in matches])
            )
        ).all()
    )
    blocks = build_blocks(matches, contents, signals.words, budget_tokens)
    return Investigation(issue, signals, matches, blocks)
