import ast
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "site-packages",
}
TEXT_SUFFIXES = {".py", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json"}
MAX_FILE_BYTES = 200_000


@dataclass
class SymbolInfo:
    name: str
    qualname: str
    kind: str  # "class" | "function" | "method"
    start_line: int
    end_line: int
    docstring: str | None = None


@dataclass
class FileInfo:
    path: str  # posix-style, relative to the repo root
    language: str
    size: int
    line_count: int
    content: str
    symbols: list[SymbolInfo] = field(default_factory=list)


def extract_symbols(source: str) -> list[SymbolInfo]:
    """Top-level and nested classes, functions, and methods of a Python module."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    symbols: list[SymbolInfo] = []

    def visit(node: ast.AST, prefix: str, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                kind = "class"
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                kind = "method" if in_class else "function"
            else:
                visit(child, prefix, in_class)
                continue
            qualname = f"{prefix}{child.name}"
            symbols.append(
                SymbolInfo(
                    name=child.name,
                    qualname=qualname,
                    kind=kind,
                    start_line=child.lineno,
                    end_line=child.end_lineno or child.lineno,
                    docstring=ast.get_docstring(child),
                )
            )
            visit(child, f"{qualname}.", kind == "class")

    visit(tree, "", False)
    return symbols


def scan_repository(root: Path) -> list[FileInfo]:
    """Walk `root` and return text files worth indexing. Symlinks are never followed."""
    root = root.resolve()
    files: list[FileInfo] = []

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        if path.is_symlink() or not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        content = content.replace("\x00", "")  # Postgres text cannot hold NUL bytes

        is_python = path.suffix == ".py"
        files.append(
            FileInfo(
                path=relative.as_posix(),
                language="python" if is_python else "text",
                size=size,
                line_count=content.count("\n")
                + (0 if content.endswith("\n") or not content else 1),
                content=content,
                symbols=extract_symbols(content) if is_python else [],
            )
        )
    return files
