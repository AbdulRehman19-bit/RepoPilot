import os

import pytest

from app.patching.apply import Edit, PatchError, apply_edits, is_protected, make_candidate

CALC = "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "calc.py").write_text(CALC, encoding="utf-8")
    (root / "pkg" / "util.py").write_text("X = 1\nY = 2\n", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / "notes.md").write_text("hello\n", encoding="utf-8")
    return root


def edit(
    path="calc.py",
    search="def add(a, b):\n    return a - b",
    replace="def add(a, b):\n    return a + b",
):
    return Edit(path=path, search=search, replace=replace)


def test_applies_edit_and_produces_a_unified_diff(repo):
    applied = apply_edits(repo, [edit()])

    assert "return a + b" in (repo / "calc.py").read_text()
    assert "def sub(a, b):\n    return a - b" in (repo / "calc.py").read_text()  # untouched
    assert applied.files == ["calc.py"]
    assert applied.changed_lines == 2
    assert "--- a/calc.py" in applied.diff and "+++ b/calc.py" in applied.diff
    assert "-    return a - b" in applied.diff and "+    return a + b" in applied.diff


def test_surrounding_blank_lines_in_search_are_tolerated(repo):
    apply_edits(
        repo,
        [
            edit(
                search="\ndef add(a, b):\n    return a - b\n\n",
                replace="def add(a, b):\n    return a + b\n",
            )
        ],
    )
    assert "return a + b" in (repo / "calc.py").read_text()


def test_several_edits_to_one_file_apply_in_order(repo):
    apply_edits(
        repo,
        [
            edit(search="def add(a, b):", replace="def add(a, b, c=0):"),
            edit(
                search="def add(a, b, c=0):\n    return a - b",
                replace="def add(a, b, c=0):\n    return a + b + c",
            ),
        ],
    )
    assert "return a + b + c" in (repo / "calc.py").read_text()


def test_crlf_line_endings_are_preserved(repo):
    (repo / "calc.py").write_bytes(CALC.replace("\n", "\r\n").encode())
    apply_edits(repo, [edit()])
    data = (repo / "calc.py").read_bytes()
    assert b"return a + b\r\n" in data
    assert data.count(b"\r\n") == data.count(b"\n")  # no bare LF introduced


@pytest.mark.parametrize(
    ("search", "message"),
    [
        ("def nope():", "not found"),
        ("return a - b", "matches 2 places"),
        ("   \n", "empty search"),
        ("def add(a, b):\n    return a - b", "changes nothing"),
    ],
)
def test_bad_search_text_is_rejected(repo, search, message):
    replace = search if message == "changes nothing" else "x"
    with pytest.raises(PatchError, match=message):
        apply_edits(repo, [edit(search=search, replace=replace)])
    assert (repo / "calc.py").read_text() == CALC


LOOSE = 'def f(d):\n    return {k : v for k, v in d.items()}\n\n\ndef g():\n    return "x" + "y"\n'


@pytest.fixture
def loose_repo(tmp_path):
    (tmp_path / "m.py").write_text(LOOSE, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "search",
    [
        "return {k: v for k, v in d.items()}",  # space before the colon dropped
        "    return {k : v for k , v in d.items()}",  # extra spaces, same indentation
        "return {k : v\n    for k, v in d.items()}",  # different line breaks
        "return {k:v for k,v in d.items()}",  # spaces removed
    ],
)
def test_near_miss_search_text_is_matched_ignoring_layout(loose_repo, search):
    apply_edits(loose_repo, [Edit(path="m.py", search=search, replace="return dict(d)")])
    assert "return dict(d)" in (loose_repo / "m.py").read_text()
    assert "k : v" not in (loose_repo / "m.py").read_text()


def test_quote_style_is_ignored_but_words_are_never_joined_or_split(loose_repo):
    apply_edits(loose_repo, [Edit(path="m.py", search="return 'x'+'y'", replace="return 1")])
    assert (loose_repo / "m.py").read_text().endswith("def g():\n    return 1\n")
    for bad in ("defg():", "retur n", "def f (d) :returnx"):
        with pytest.raises(PatchError, match="not found"):
            apply_edits(loose_repo, [Edit(path="m.py", search=bad, replace="x")])


def test_loose_match_must_still_be_unique(tmp_path):
    (tmp_path / "m.py").write_text("a = f(1, 2)\nb = f(1,2)\n", encoding="utf-8")
    with pytest.raises(PatchError, match="matches 2 places"):
        apply_edits(tmp_path, [Edit(path="m.py", search="f(1 , 2)", replace="g()")])
    assert (tmp_path / "m.py").read_text() == "a = f(1, 2)\nb = f(1,2)\n"


def test_exact_match_wins_over_loose_matches(tmp_path):
    (tmp_path / "m.py").write_text("x = f(1, 2)\ny = f(1,2)\n", encoding="utf-8")
    apply_edits(tmp_path, [Edit(path="m.py", search="f(1,2)", replace="g()")])
    assert (tmp_path / "m.py").read_text() == "x = f(1, 2)\ny = g()\n"


def test_no_edits_is_an_error(repo):
    with pytest.raises(PatchError, match="no edits"):
        apply_edits(repo, [])


def test_nothing_is_written_if_any_edit_fails(repo):
    good = edit()
    bad = edit(path="pkg/util.py", search="Z = 3", replace="Z = 4")
    with pytest.raises(PatchError):
        apply_edits(repo, [good, bad])
    assert (repo / "calc.py").read_text() == CALC  # the good edit was not left behind


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_calc.py",
        "tests/helpers.py",
        "pkg/test_util.py",
        "pkg/util_test.py",
        "conftest.py",
        "pkg/conftest.py",
        "setup.py",
        "a/tests/b/c.py",
    ],
)
def test_tests_and_test_infrastructure_are_protected(repo, path):
    assert is_protected(path)
    with pytest.raises(PatchError, match="cannot be edited|does not exist"):
        apply_edits(repo, [edit(path=path)])


def test_files_named_by_the_reproduction_are_protected(repo):
    assert is_protected("pkg/checks.py", {"pkg/checks.py"})
    with pytest.raises(PatchError, match="cannot be edited"):
        apply_edits(
            repo, [edit(path="pkg/util.py", search="X = 1", replace="X = 2")], {"pkg/util.py"}
        )


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "pkg/../../outside.py",
        "/etc/passwd.py",
        "C:/Windows/x.py",
        ".git/hooks/x.py",
        "",
        "notes.md",
        "calc.txt",
    ],
)
def test_paths_outside_the_repo_or_non_python_are_rejected(repo, path):
    with pytest.raises(PatchError):
        apply_edits(repo, [edit(path=path)])


def test_missing_file_is_rejected(repo):
    with pytest.raises(PatchError, match="does not exist"):
        apply_edits(repo, [edit(path="ghost.py")])


def test_symlinks_are_rejected(repo, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n")
    try:
        os.symlink(outside, repo / "link.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    with pytest.raises(PatchError, match="does not exist"):
        apply_edits(repo, [edit(path="link.py", search="SECRET = 1", replace="SECRET = 2")])
    assert outside.read_text() == "SECRET = 1\n"


def test_limits(repo):
    for n in range(6):
        (repo / f"m{n}.py").write_text("V = 1\n")
    many = [edit(path=f"m{n}.py", search="V = 1", replace="V = 2") for n in range(6)]
    with pytest.raises(PatchError, match="too many files"):
        apply_edits(repo, many)

    big = "\n".join(f"line_{n} = {n}" for n in range(300))
    (repo / "big.py").write_text(big + "\n")
    with pytest.raises(PatchError, match="too large|too many|edit is too large"):
        apply_edits(repo, [edit(path="big.py", search=big, replace=big.replace("line", "row"))])


def test_make_candidate_copies_without_git_and_refuses_existing(repo, tmp_path):
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("secret")
    candidate = make_candidate(repo, tmp_path / "cand")
    assert (candidate / "calc.py").exists()
    assert not (candidate / ".git").exists()
    apply_edits(candidate, [edit()])
    assert (repo / "calc.py").read_text() == CALC  # the original is untouched
    with pytest.raises(FileExistsError):
        make_candidate(repo, tmp_path / "cand")
