"""Rank indexed files by how likely they are to contain the bug described in an issue."""

import ast
import re
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import IndexedFile, Symbol
from app.retrieval.signals import Signals, extract_signals

PATH_MENTION_SCORE = 10.0
LINE_IN_SYMBOL_BONUS = 3.0
EXACT_SYMBOL_SCORE = 5.0
QUALNAME_SYMBOL_SCORE = 6.0
SYMBOL_SCORE_CAP = 12.0
FULL_TEXT_SCORE = 3.0
FULL_TEXT_CANDIDATES = 50
# Python prints the innermost call last. When the last frame lives in a different project file
# than the earlier ones, that is where the exception was raised, so it gets a little extra.
INNERMOST_FRAME_BONUS = 2.0
# Files defining functions that the best matches call, since a bug is often one call deeper than
# the code the issue names. Weaker than any direct mention.
FOLLOW_CALL_SCORE = 4.0
FOLLOW_FROM_TOP = 2
AMBIGUOUS_CALLEE_FILES = 2  # a name defined in more files than this says nothing about one of them


@dataclass
class Focus:
    label: str
    start_line: int
    end_line: int


@dataclass
class Match:
    file_id: int
    path: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    focus: list[Focus] = field(default_factory=list)
    hint_lines: list[int] = field(default_factory=list)  # traceback lines in this file


def _path_matches(mentioned: str, indexed_paths: list[str]) -> list[str]:
    """Indexed paths that `mentioned` refers to, tolerating absolute or partial prefixes."""
    mentioned = mentioned.lstrip("./")
    return [
        path
        for path in indexed_paths
        if path == mentioned or mentioned.endswith("/" + path) or path.endswith("/" + mentioned)
    ]


def _tsquery(signals: Signals) -> str | None:
    terms = list(signals.words)
    # Postgres keeps CamelCase as one lowercase token, so search whole names as well as parts.
    terms += [name.lower() for name in signals.identifiers if "." not in name]
    cleaned = dict.fromkeys(re.sub(r"[^a-z0-9]", "", term) for term in terms)
    cleaned.pop("", None)
    return " | ".join(cleaned) or None


def rank_files(
    session: Session,
    repository_id: int,
    text: str,
    limit: int = 8,
    signals: Signals | None = None,
) -> list[Match]:
    signals = signals or extract_signals(text)
    files = session.execute(
        select(IndexedFile.id, IndexedFile.path).where(IndexedFile.repository_id == repository_id)
    ).all()
    by_path = {path: file_id for file_id, path in files}
    matches: dict[int, Match] = {}

    def match_for(file_id: int, path: str) -> Match:
        if file_id not in matches:
            matches[file_id] = Match(file_id=file_id, path=path)
        return matches[file_id]

    # 1. Files named in the issue or its traceback.
    line_hints: dict[int, list[int]] = {}
    path_scored: set[int] = set()
    for ref in signals.paths:
        resolved = _path_matches(ref.path, list(by_path))
        for path in resolved:
            match = match_for(by_path[path], path)
            # A file named several times (e.g. multiple traceback frames) scores once.
            if by_path[path] not in path_scored:
                path_scored.add(by_path[path])
                match.score += PATH_MENTION_SCORE / len(resolved)
            where = f" (line {ref.line})" if ref.line else ""
            match.reasons.append(f"named in the issue: {ref.path}{where}")
            if ref.line:
                line_hints.setdefault(by_path[path], []).append(ref.line)
                match.hint_lines.append(ref.line)

    # Of the traceback frames that are project files, the last one is the innermost.
    framed = [
        by_path[path]
        for ref in signals.paths
        if ref.frame
        for path in _path_matches(ref.path, list(by_path))[:1]
    ]
    if len(set(framed)) > 1:
        innermost = framed[-1]
        matches[innermost].score += INNERMOST_FRAME_BONUS
        matches[innermost].reasons.append("innermost project frame of the traceback")

    # 2. Symbols the issue names, and symbols enclosing a traceback line.
    strong = signals.identifiers
    if strong or line_hints:
        symbol_query = (
            select(Symbol, IndexedFile.path)
            .join(IndexedFile)
            .where(IndexedFile.repository_id == repository_id)
        )
        conditions = []
        if strong:
            conditions.append(Symbol.name.in_(list(strong)))
            conditions.append(Symbol.qualname.in_(list(strong)))
        if line_hints:
            conditions.append(Symbol.file_id.in_(list(line_hints)))
        symbols = session.execute(symbol_query.where(or_(*conditions))).all()

        symbol_scores: dict[int, float] = {}
        for symbol, path in symbols:
            file_id = symbol.file_id
            score = 0.0
            if symbol.qualname in strong:
                score, why = QUALNAME_SYMBOL_SCORE, "defines"
            elif symbol.name in strong:
                score, why = EXACT_SYMBOL_SCORE, "defines"
            if score:
                symbol_scores[file_id] = min(
                    symbol_scores.get(file_id, 0.0) + score, SYMBOL_SCORE_CAP
                )
                match = match_for(file_id, path)
                match.reasons.append(f"{why} `{symbol.qualname}` mentioned in the issue")
                match.focus.append(Focus(symbol.qualname, symbol.start_line, symbol.end_line))

        for file_id, lines in line_hints.items():
            candidates = [s for s, _ in symbols if s.file_id == file_id]
            for line in lines:
                enclosing = [s for s in candidates if s.start_line <= line <= s.end_line]
                if enclosing:
                    inner = min(enclosing, key=lambda s: s.end_line - s.start_line)
                    match = matches[file_id]
                    match.score += LINE_IN_SYMBOL_BONUS
                    match.reasons.append(f"line {line} is inside `{inner.qualname}`")
                    match.focus.append(Focus(inner.qualname, inner.start_line, inner.end_line))

        for file_id, score in symbol_scores.items():
            matches[file_id].score += score

    # 3. Full-text relevance over file contents.
    query_text = _tsquery(signals)
    if query_text:
        query = func.to_tsquery("simple", query_text)
        rank = func.ts_rank_cd(IndexedFile.tsv, query).label("rank")
        rows = session.execute(
            select(IndexedFile.id, IndexedFile.path, rank)
            .where(IndexedFile.repository_id == repository_id, IndexedFile.tsv.op("@@")(query))
            .order_by(rank.desc())
            .limit(FULL_TEXT_CANDIDATES)
        ).all()
        top_rank = max((row.rank for row in rows), default=0.0)
        for file_id, path, file_rank in rows:
            if top_rank <= 0:
                break
            match = match_for(file_id, path)
            match.score += FULL_TEXT_SCORE * file_rank / top_rank
            match.reasons.append(f"text match (relevance {file_rank:.2f})")

    ranked = sorted(matches.values(), key=lambda m: (-m.score, m.path))
    if _follow_calls(session, repository_id, ranked[:FOLLOW_FROM_TOP], matches, by_path):
        ranked = sorted(matches.values(), key=lambda m: (-m.score, m.path))
    for match in ranked:
        match.score = round(match.score, 3)
        match.focus = list({(f.start_line, f.end_line): f for f in match.focus}.values())
    return ranked[:limit]


def _called_names(source: str, ranges: list[tuple[int, int]]) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and any(a <= node.lineno <= b for a, b in ranges):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _follow_calls(
    session: Session,
    repository_id: int,
    top: list[Match],
    matches: dict[int, Match],
    by_path: dict[str, int],
) -> bool:
    """Credit files that define what the best matches' focused code calls. True if any scored."""
    changed = False
    for source in top:
        if not source.focus or not source.path.endswith(".py"):
            continue
        # Prefer the narrowest focus: drop a range that merely contains another (e.g. a class
        # around the method the issue is about).
        ranges = [(f.start_line, f.end_line) for f in source.focus]
        narrow = [
            r for r in ranges if not any(o != r and r[0] <= o[0] and o[1] <= r[1] for o in ranges)
        ]
        content = session.scalar(
            select(IndexedFile.content).where(IndexedFile.id == source.file_id)
        )
        names = _called_names(content or "", narrow)
        if not names:
            continue

        rows = session.execute(
            select(Symbol.name, Symbol.file_id, Symbol.qualname)
            .join(IndexedFile)
            .where(IndexedFile.repository_id == repository_id, Symbol.name.in_(names))
        ).all()
        files_defining: dict[str, set[int]] = {}
        for name, file_id, _ in rows:
            files_defining.setdefault(name, set()).add(file_id)

        path_of = {file_id: path for path, file_id in by_path.items()}
        credited: set[int] = set()
        for name, file_id, qualname in rows:
            if (
                file_id == source.file_id
                or file_id in credited
                or len(files_defining[name]) > AMBIGUOUS_CALLEE_FILES
            ):
                continue
            credited.add(file_id)
            target = matches.get(file_id) or Match(file_id=file_id, path=path_of[file_id])
            matches[file_id] = target
            target.score += FOLLOW_CALL_SCORE
            target.reasons.append(f"defines `{qualname}`, called from {source.path}")
            changed = True
    return changed
