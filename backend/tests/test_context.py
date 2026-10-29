from fastapi.testclient import TestClient

from app.main import app
from app.models import IndexedFile, Issue, Repository, Symbol, User
from app.retrieval.context import build_blocks, estimate_tokens, explain
from app.retrieval.ranker import Focus, Match

# 100 numbered lines: "line 1" ... "line 100"
CONTENT = "\n".join(f"line {n}" for n in range(1, 101))


def match(file_id: int = 1, path: str = "a.py", **kwargs) -> Match:
    return Match(file_id=file_id, path=path, **kwargs)


def test_focus_window_is_padded():
    m = match(focus=[Focus("f", 10, 20)])
    [block] = build_blocks([m], {1: CONTENT}, [], budget_tokens=3000)
    assert (block.start_line, block.end_line) == (8, 22)  # 2 lines of padding each side
    assert block.label == "f"
    assert block.text.splitlines()[0] == "line 8"


def test_overlapping_windows_merge():
    m = match(focus=[Focus("f", 10, 20), Focus("g", 22, 30)])
    [block] = build_blocks([m], {1: CONTENT}, [], budget_tokens=3000)
    assert (block.start_line, block.end_line) == (8, 32)
    assert block.label == "f, g"


def test_distant_windows_stay_separate():
    m = match(focus=[Focus("f", 5, 6), Focus("g", 80, 82)])
    blocks = build_blocks([m], {1: CONTENT}, [], budget_tokens=3000)
    assert [b.label for b in blocks] == ["f", "g"]


def test_huge_symbol_is_narrowed_around_traceback_line():
    m = match(focus=[Focus("big", 1, 100)], hint_lines=[70])
    [block] = build_blocks([m], {1: CONTENT}, [], budget_tokens=3000)
    assert block.start_line == 45 - 2
    assert block.end_line == 95 + 2


def test_traceback_line_without_symbol_uses_radius():
    m = match(hint_lines=[50])
    [block] = build_blocks([m], {1: CONTENT}, [], budget_tokens=3000)
    assert (block.start_line, block.end_line) == (38, 62)
    assert block.label == "line 50"


def test_text_only_match_centres_on_best_line():
    content = "\n".join(["noise"] * 30 + ["invoice tax total"] + ["noise"] * 30)
    [block] = build_blocks([match()], {1: content}, ["invoice", "tax"], budget_tokens=3000)
    assert block.label == "closest text match"
    assert "invoice tax total" in block.text


def test_file_without_any_hit_falls_back_to_head():
    [block] = build_blocks([match()], {1: CONTENT}, ["zzz"], budget_tokens=3000)
    assert block.label == "start of file"
    assert block.start_line == 1


def test_budget_truncates_then_stops():
    big = "\n".join("x" * 79 for _ in range(400))  # ~100 tokens per 5 lines
    first = match(1, "a.py", focus=[Focus("a", 1, 60)])
    second = match(2, "b.py", focus=[Focus("b", 1, 10)])
    blocks = build_blocks([first, second], {1: big, 2: big}, [], budget_tokens=500)

    assert [b.path for b in blocks] == ["a.py"]
    assert blocks[0].truncated
    assert sum(b.tokens for b in blocks) <= 500
    assert blocks[0].end_line == blocks[0].start_line + blocks[0].text.count("\n")


def test_best_files_get_budget_first():
    big = "\n".join("x" * 79 for _ in range(400))
    first = match(1, "a.py", focus=[Focus("a", 1, 10)])
    second = match(2, "b.py", focus=[Focus("b", 1, 10)])
    blocks = build_blocks([first, second], {1: big, 2: big}, [], budget_tokens=3000)
    assert [b.path for b in blocks] == ["a.py", "b.py"]
    assert sum(b.tokens for b in blocks) <= 3000


def test_missing_content_is_skipped():
    assert build_blocks([match()], {}, [], budget_tokens=3000) == []


def test_estimate_tokens_rounds_up():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_explain():
    m = match(
        reasons=[
            "named in the issue: a.py (line 7)",
            "line 7 is inside `f`",
            "text match (relevance 0.10)",
        ]
    )
    sentence = explain(m)
    assert sentence == "Likely relevant: named in the issue: a.py (line 7); line 7 is inside `f`."
    assert explain(match(reasons=["text match (relevance 0.10)"])) == (
        "Matched by the words used in the issue."
    )


def test_retrieve_endpoint_returns_context(db_session):
    repo = Repository(
        owner=User(github_id=444_001, login="c"),
        github_id=444_002,
        full_name="c/demo",
        index_status="ready",
    )
    code = "def parse_config(text):\n    if not text:\n        raise ValueError('empty')\n    return text\n"
    repo.files.append(
        IndexedFile(
            path="cfg.py",
            language="python",
            size=len(code),
            line_count=4,
            content=code,
            symbols=[
                Symbol(
                    name="parse_config",
                    qualname="parse_config",
                    kind="function",
                    start_line=1,
                    end_line=4,
                )
            ],
        )
    )
    issue = Issue(repository=repo, number=1, title="parse_config fails on empty input", body="")
    db_session.add_all([repo, issue])
    db_session.flush()

    client = TestClient(app)
    body = client.post(f"/issues/{issue.id}/retrieve").json()
    assert body["matches"][0]["explanation"].startswith("Likely relevant")
    assert body["context"][0]["path"] == "cfg.py"
    assert "def parse_config" in body["context"][0]["text"]
    assert body["context_tokens"] == sum(b["tokens"] for b in body["context"])

    small = client.post(f"/issues/{issue.id}/retrieve?budget_tokens=100")
    assert small.status_code == 422  # below the allowed minimum budget
