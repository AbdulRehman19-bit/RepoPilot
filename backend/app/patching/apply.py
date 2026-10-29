"""Apply model-proposed search/replace edits to a checkout, safely and verifiably.

Models are unreliable at writing unified diffs with correct line numbers, so they propose
exact-match edits instead and we generate the diff ourselves.

The text-level core (`apply_to_texts`) is shared by the sandbox verification (which edits files
on disk) and by pull request creation (which edits files fetched from GitHub), so both produce
the identical diff.
"""

import difflib
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from app.sandbox.runner import COPY_IGNORE

MAX_FILES = 5
MAX_CHANGED_LINES = 200
MAX_EDIT_CHARS = 4000
PROTECTED_DIRS = {"tests", "test", "testing"}
PROTECTED_NAMES = {"conftest.py", "setup.py"}


class PatchError(ValueError):
    """The proposed edits cannot be applied or are not allowed."""


class Edit(BaseModel):
    path: str = Field(description="Path of an existing .py file, relative to the repository root")
    search: str = Field(description="Exact, contiguous lines copied from the current file")
    replace: str = Field(description="The lines that should replace the searched lines")


@dataclass
class AppliedPatch:
    diff: str
    files: list[str]
    changed_lines: int


@dataclass
class AppliedTexts:
    updated: dict[str, str]  # path -> new text, with "\n" line endings
    diff: str
    files: list[str]  # only the files that actually changed
    changed_lines: int


def is_protected(path: str, extra: Iterable[str] = ()) -> bool:
    """Tests and test infrastructure may not be edited: a fix must not 'pass' by changing them."""
    posix = PurePosixPath(path)
    name = posix.name
    return (
        path in set(extra)
        or name in PROTECTED_NAMES
        or name.startswith("test_")
        or name.endswith("_test.py")
        or any(part in PROTECTED_DIRS for part in posix.parts[:-1])
    )


def normalize_edit_path(raw: str, protected: Iterable[str] = ()) -> str:
    """Validate a path from the model without touching the filesystem."""
    normalized = raw.replace("\\", "/").strip()
    posix = PurePosixPath(normalized)
    if (
        not normalized
        or posix.is_absolute()
        or ":" in normalized  # drive letters such as C:/x
        or ".." in posix.parts
        or ".git" in posix.parts
    ):
        raise PatchError(f"path {raw!r} is not allowed")
    if posix.suffix != ".py":
        raise PatchError(f"only Python files can be edited, not {raw!r}")
    if is_protected(normalized, protected):
        raise PatchError(f"{raw!r} is a test or test-infrastructure file and cannot be edited")
    return normalized


def edit_paths(edits: list[Edit], protected: Iterable[str] = ()) -> list[str]:
    """The distinct, validated paths the edits touch, in order of first use."""
    if not edits:
        raise PatchError("the model proposed no edits")
    protected = set(protected)
    paths = list(dict.fromkeys(normalize_edit_path(e.path, protected) for e in edits))
    if len(paths) > MAX_FILES:
        raise PatchError(f"too many files edited (limit {MAX_FILES})")
    return paths


def _trim_blank_edges(text: str) -> str:
    return text.strip("\r\n")


def _loose_pattern(search: str) -> re.Pattern[str]:
    """A pattern for `search` that ignores whitespace layout and quote style.

    Small models copy code almost right: an extra space before a colon, ' for ", different
    indentation. Whitespace may differ or be absent next to punctuation, but two word characters
    are never joined or split (so `a b` does not match `ab`), and a quote only matches a quote.
    """
    parts: list[str] = []
    previous = ""
    pending_space = False
    for char in search:
        if char.isspace():
            pending_space = True
            continue
        if previous:
            both_words = (previous.isalnum() or previous == "_") and (char.isalnum() or char == "_")
            if pending_space:
                parts.append(r"\s+" if both_words else r"\s*")
            elif not both_words:
                parts.append(r"\s*")
        parts.append("[\"']" if char in "\"'" else re.escape(char))
        previous, pending_space = char, False
    return re.compile("".join(parts))


def _locate(text: str, search: str) -> list[tuple[int, int]]:
    """Where `search` occurs in `text`: exactly if possible, otherwise ignoring layout."""
    exact = [(m.start(), m.end()) for m in re.finditer(re.escape(search), text)]
    if exact:
        return exact
    return [(m.start(), m.end()) for m in _loose_pattern(search).finditer(text)]


def apply_to_texts(
    texts: dict[str, str], edits: list[Edit], protected: Iterable[str] = ()
) -> AppliedTexts:
    """Apply `edits` to in-memory file contents (LF line endings) and build the unified diff."""
    protected = set(protected)
    paths = edit_paths(edits, protected)
    missing = [path for path in paths if path not in texts]
    if missing:
        raise PatchError(f"{missing[0]!r} does not exist in the repository")
    updated = {path: texts[path] for path in paths}

    for edit in edits:
        if len(edit.search) > MAX_EDIT_CHARS or len(edit.replace) > MAX_EDIT_CHARS:
            raise PatchError("an edit is too large; make smaller, targeted edits")
        path = normalize_edit_path(edit.path, protected)

        search = _trim_blank_edges(edit.search.replace("\r\n", "\n"))
        replace = _trim_blank_edges(edit.replace.replace("\r\n", "\n"))
        if not search.strip():
            raise PatchError(f"empty search text for {path}")
        if search == replace:
            raise PatchError(f"edit to {path} changes nothing")
        spans = _locate(updated[path], search)
        if not spans:
            first = search.splitlines()[0][:80]
            raise PatchError(f"search text not found in {path} (starts with {first!r})")
        if len(spans) > 1:
            raise PatchError(
                f"search text matches {len(spans)} places in {path}; include more surrounding lines"
            )
        start, end = spans[0]
        updated[path] = updated[path][:start] + replace + updated[path][end:]

    diff_parts: list[str] = []
    changed_files: list[str] = []
    changed = 0
    for path in paths:
        before, after = texts[path], updated[path]
        if before == after:
            continue
        lines = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        changed += sum(
            1 for line in lines if line[:1] in "+-" and not line.startswith(("+++", "---"))
        )
        diff_parts.append("".join(line if line.endswith("\n") else line + "\n" for line in lines))
        changed_files.append(path)
    if not diff_parts:
        raise PatchError("the edits do not change any file")
    if changed > MAX_CHANGED_LINES:
        raise PatchError(f"patch is too large ({changed} changed lines, limit {MAX_CHANGED_LINES})")
    return AppliedTexts(updated, "".join(diff_parts), changed_files, changed)


def _read_file(root: Path, path: str) -> tuple[Path, str, str]:
    target = root / path
    if target.is_symlink() or not target.is_file():
        raise PatchError(f"{path!r} does not exist in the repository")
    if root.resolve() not in target.resolve().parents:
        raise PatchError(f"path {path!r} escapes the repository")
    raw = target.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return target, newline, raw.replace("\r\n", "\n")


def apply_edits(root: Path, edits: list[Edit], protected: Iterable[str] = ()) -> AppliedPatch:
    """Apply `edits` to files under `root` in place, returning the resulting unified diff.

    Nothing is written unless every edit applies, and original line endings are preserved.
    """
    protected = set(protected)
    loaded = {path: _read_file(root, path) for path in edit_paths(edits, protected)}
    applied = apply_to_texts({p: text for p, (_, _, text) in loaded.items()}, edits, protected)

    for path in applied.files:
        target, newline, _ = loaded[path]
        target.write_bytes(applied.updated[path].replace("\n", newline).encode("utf-8"))
    return AppliedPatch(applied.diff, applied.files, applied.changed_lines)


def make_candidate(checkout: Path, destination: Path) -> Path:
    """A throwaway copy of the checkout that edits can be applied to."""
    if destination.exists():
        raise FileExistsError(destination)
    shutil.copytree(checkout, destination, ignore=COPY_IGNORE, symlinks=True)
    return destination
