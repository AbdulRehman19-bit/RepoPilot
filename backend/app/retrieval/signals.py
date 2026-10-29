"""Deterministic extraction of search signals from free-text bug reports."""

import re
from dataclasses import dataclass, field

TRACEBACK_FRAME = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([\w<>.]+))?')
PATH_REF = re.compile(
    r"(?<![\w/\\.-])((?:[A-Za-z]:)?[\w./\\-]*[\w-]\.(?:pyi?|md|rst|txt|toml|cfg|ini|ya?ml|json))"
    r"(?::(\d+))?"
)
BACKTICKED = re.compile(r"`([^`\n]+)`")
DOTTED_IDENT = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
CALL = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\(")
SNAKE_CASE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
CAMEL_CASE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
CAMEL_PART = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")

STOPWORDS = frozenset(
    [
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "can",
        "had",
        "her",
        "was",
        "one",
        "our",
        "out",
        "has",
        "have",
        "this",
        "that",
        "with",
        "from",
        "they",
        "will",
        "would",
        "there",
        "their",
        "what",
        "about",
        "which",
        "when",
        "where",
        "while",
        "should",
        "could",
        "been",
        "being",
        "into",
        "than",
        "then",
        "them",
        "these",
        "those",
        "some",
        "such",
        "only",
        "also",
        "just",
        "like",
        "more",
        "most",
        "other",
        "over",
        "very",
        "does",
        "did",
        "doing",
        "done",
        "here",
        "how",
        "why",
        "who",
        "its",
        "it's",
        "isn't",
        "doesn't",
        "don't",
        "can't",
        "won't",
        "error",
        "issue",
        "bug",
        "problem",
        "expected",
        "actual",
        "steps",
        "reproduce",
        "reproduction",
        "behavior",
        "behaviour",
        "result",
        "results",
        "using",
        "used",
        "use",
        "get",
        "got",
        "set",
        "sets",
        "fix",
        "fixes",
        "fixed",
        "please",
        "thanks",
        "thank",
        "see",
        "seen",
        "run",
        "running",
        "runs",
        "return",
        "returns",
        "returned",
        "file",
        "line",
        "call",
        "called",
        "calling",
        "code",
        "none",
        "true",
        "false",
        "traceback",
        "recent",
        "last",
        "most",
    ]
)
MAX_WORDS = 30


@dataclass(frozen=True)
class PathRef:
    path: str  # normalized to forward slashes
    line: int | None = None
    frame: bool = False  # a traceback frame (Python lists the innermost call last)


@dataclass
class Signals:
    paths: list[PathRef] = field(default_factory=list)
    identifiers: set[str] = field(default_factory=set)  # high-confidence code names
    error_types: set[str] = field(default_factory=set)
    words: list[str] = field(default_factory=list)  # lowercase search terms for full-text


def _add_ident(target: set[str], dotted: str) -> None:
    """Add a possibly dotted name plus each of its parts."""
    target.add(dotted)
    parts = dotted.split(".")
    if len(parts) > 1:
        target.update(part for part in parts if len(part) > 1)


def _normalize_path(raw: str) -> str:
    return raw.replace("\\", "/")


def _words(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for token in WORD.findall(text):
        for part in CAMEL_PART.findall(token):
            lowered = part.lower()
            if len(lowered) >= 3 and lowered not in STOPWORDS:
                seen.setdefault(lowered)
    return list(seen)[:MAX_WORDS]


def extract_signals(text: str) -> Signals:
    signals = Signals()
    seen_paths: set[PathRef] = set()

    def add_path(raw: str, line: str | None, frame: bool = False) -> None:
        ref = PathRef(_normalize_path(raw), int(line) if line else None, frame)
        if ref not in seen_paths:
            seen_paths.add(ref)
            signals.paths.append(ref)

    for path, line, func in TRACEBACK_FRAME.findall(text):
        add_path(path, line, frame=True)
        if func and not func.startswith("<"):
            _add_ident(signals.identifiers, func)

    framed = {ref.path for ref in signals.paths}
    for path, line in PATH_REF.findall(text):
        # Traceback frames already recorded these paths with their line numbers.
        if _normalize_path(path) not in framed:
            add_path(path, line or None)

    for inner in BACKTICKED.findall(text):
        for dotted in DOTTED_IDENT.findall(inner):
            if len(dotted) > 1 and dotted not in {"self", "cls"}:
                _add_ident(signals.identifiers, dotted)

    for dotted in CALL.findall(text):
        _add_ident(signals.identifiers, dotted)
    signals.identifiers.update(SNAKE_CASE.findall(text))

    for name in CAMEL_CASE.findall(text):
        signals.identifiers.add(name)
        if name.endswith(("Error", "Exception")):
            signals.error_types.add(name)

    signals.identifiers -= {"self", "cls", "print", "len", "str", "int", "list", "dict"}
    signals.words = _words(text)
    return signals
