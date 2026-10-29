"""Turn ranked files into a token-budgeted set of code windows, with plain explanations."""

from dataclasses import dataclass

from app.retrieval.ranker import Match

CHARS_PER_TOKEN = 4
CONTEXT_PAD = 2  # lines of surrounding code added to every window
MAX_SYMBOL_LINES = 60  # symbols longer than this are narrowed around the traceback line
TRACEBACK_RADIUS = 10
TEXT_MATCH_RADIUS = 8
HEAD_LINES = 40
MIN_USEFUL_TOKENS = 100  # don't bother truncating a window into less than this


@dataclass
class ContextBlock:
    path: str
    start_line: int
    end_line: int
    label: str
    text: str
    tokens: int
    truncated: bool = False


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


def explain(match: Match) -> str:
    """One readable sentence summarising why a file was selected."""
    reasons = [r for r in match.reasons if not r.startswith("text match")]
    if not reasons:
        return "Matched by the words used in the issue."
    # Deduplicate while keeping order, and keep the sentence short.
    unique = list(dict.fromkeys(reasons))[:3]
    return "Likely relevant: " + "; ".join(unique) + "."


def _best_text_line(lines: list[str], words: list[str]) -> int | None:
    """1-based line containing the most distinct search words, or None if no line has any."""
    best_line, best_count = None, 0
    for number, line in enumerate(lines, start=1):
        lowered = line.lower()
        count = sum(1 for word in words if word in lowered)
        if count > best_count:
            best_line, best_count = number, count
    return best_line


def _windows(match: Match, lines: list[str], words: list[str]) -> list[tuple[int, int, str]]:
    total = len(lines)
    windows: list[tuple[int, int, str]] = []

    for focus in match.focus:
        start, end = focus.start_line, focus.end_line
        if end - start + 1 > MAX_SYMBOL_LINES:
            inside = [n for n in match.hint_lines if start <= n <= end]
            if inside:
                start, end = max(start, inside[0] - 25), min(end, inside[0] + 25)
            else:
                end = start + MAX_SYMBOL_LINES - 1
        windows.append((start, end, focus.label))

    for line in match.hint_lines:
        if not any(start <= line <= end for start, end, _ in windows):
            windows.append((line - TRACEBACK_RADIUS, line + TRACEBACK_RADIUS, f"line {line}"))

    if not windows:
        best = _best_text_line(lines, words)
        if best is not None:
            windows.append(
                (best - TEXT_MATCH_RADIUS, best + TEXT_MATCH_RADIUS, "closest text match")
            )
        else:
            windows.append((1, HEAD_LINES, "start of file"))

    padded = sorted(
        (max(1, start - CONTEXT_PAD), min(total, end + CONTEXT_PAD), label)
        for start, end, label in windows
    )
    merged: list[tuple[int, int, str]] = []
    for start, end, label in padded:
        if merged and start <= merged[-1][1] + 1:
            prev_start, prev_end, prev_label = merged[-1]
            joined = prev_label if label in prev_label.split(", ") else f"{prev_label}, {label}"
            merged[-1] = (prev_start, max(prev_end, end), joined)
        else:
            merged.append((start, end, label))
    return merged


def build_blocks(
    matches: list[Match],
    contents: dict[int, str],
    words: list[str],
    budget_tokens: int,
) -> list[ContextBlock]:
    """Select code windows from the best files first until the token budget runs out."""
    blocks: list[ContextBlock] = []
    remaining = budget_tokens

    for match in matches:
        content = contents.get(match.file_id)
        if not content:
            continue
        lines = content.splitlines()
        for start, end, label in _windows(match, lines, words):
            if start > end:
                continue
            text = "\n".join(lines[start - 1 : end])
            tokens = estimate_tokens(text)
            truncated = False
            if tokens > remaining:
                if remaining < MIN_USEFUL_TOKENS:
                    return blocks
                text = text[: remaining * CHARS_PER_TOKEN].rsplit("\n", 1)[0]
                end = start + text.count("\n")
                tokens, truncated = estimate_tokens(text), True
            blocks.append(ContextBlock(match.path, start, end, label, text, tokens, truncated))
            remaining -= tokens
            if truncated:
                return blocks
    return blocks
