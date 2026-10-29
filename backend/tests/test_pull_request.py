import pytest

from app.github.pulls import (
    GitHubError,
    PatchMismatch,
    StaleBase,
    build_pr_body,
    open_draft_pr,
)
from app.patching.apply import Edit, apply_to_texts
from tests.fake_github import FakeGitHub

BUGGY = "def add(a, b):\n    return a - b\n"
EDITS = [{"path": "calc.py", "search": "    return a - b", "replace": "    return a + b"}]
VERIFIED = apply_to_texts({"calc.py": BUGGY}, [Edit(**e) for e in EDITS]).diff
SHA = "a" * 40


def open_pr(gh: FakeGitHub, **overrides):
    args = {
        "full_name": "o/r",
        "base_branch": "main",
        "base_sha": SHA,
        "edits": EDITS,
        "verified_diff": VERIFIED,
        "protected": {"test_calc.py"},
        "issue_number": 12,
        "patch_id": 5,
        "title": "Fix #12: add is wrong",
        "body": "body",
    }
    return open_draft_pr(gh.client(), **{**args, **overrides})


def test_opens_a_draft_pull_request_with_exactly_the_verified_change():
    gh = FakeGitHub({"calc.py": BUGGY.encode()})

    pr = open_pr(gh)

    assert (pr.number, pr.url, pr.branch) == (
        7,
        "https://github.test/o/r/pull/7",
        "repopilot/issue-12-patch-5",
    )
    assert gh.branches[pr.branch]["calc.py"] == b"def add(a, b):\n    return a + b\n"
    assert gh.files["calc.py"] == BUGGY.encode()  # the base branch is untouched
    [pull] = gh.pulls
    assert pull["draft"] is True
    assert (pull["head"], pull["base"], pull["title"]) == (
        pr.branch,
        "main",
        "Fix #12: add is wrong",
    )


def test_crlf_files_keep_their_line_endings():
    crlf = BUGGY.replace("\n", "\r\n").encode()
    gh = FakeGitHub({"calc.py": crlf})

    pr = open_pr(gh)

    assert gh.branches[pr.branch]["calc.py"] == b"def add(a, b):\r\n    return a + b\r\n"


def test_refuses_when_the_base_branch_has_moved():
    gh = FakeGitHub({"calc.py": BUGGY.encode()}, head_sha="b" * 40)

    with pytest.raises(StaleBase, match="has moved"):
        open_pr(gh)

    assert gh.branches == {} and gh.pulls == []  # nothing was written
    assert not any(m != "GET" for m, _ in gh.requests)


def test_refuses_when_github_files_differ_from_what_was_verified():
    changed = (BUGGY + "# someone edited this\n").encode()
    gh = FakeGitHub({"calc.py": changed})

    with pytest.raises(PatchMismatch, match="differ"):
        open_pr(gh)
    assert gh.branches == {} and gh.pulls == []


def test_refuses_when_the_patch_no_longer_applies():
    gh = FakeGitHub({"calc.py": b"def add(a, b):\n    return b - a\n"})
    with pytest.raises(PatchMismatch, match="no longer applies"):
        open_pr(gh)
    assert gh.branches == {}


def test_refuses_stored_edits_that_touch_protected_files():
    gh = FakeGitHub({"calc.py": BUGGY.encode(), "test_calc.py": b"def test_a(): ...\n"})
    cheat = [
        {"path": "test_calc.py", "search": "def test_a(): ...", "replace": "def test_a(): pass"}
    ]
    with pytest.raises(PatchMismatch, match="not allowed"):
        open_pr(gh, edits=cheat)
    assert gh.branches == {}


def test_failed_commit_removes_the_half_built_branch():
    gh = FakeGitHub({"calc.py": BUGGY.encode()})
    gh.fail_put = True

    with pytest.raises(GitHubError, match="409"):
        open_pr(gh)

    assert gh.deleted == ["repopilot/issue-12-patch-5"]
    assert gh.branches == {} and gh.pulls == []


def test_github_outage_surfaces_as_an_error():
    gh = FakeGitHub({"calc.py": BUGGY.encode()})
    gh.fail_everything = True
    with pytest.raises(GitHubError, match="500"):
        open_pr(gh)


def test_pr_body_shows_the_evidence_and_the_reviewer():
    attempts = [
        {"outcome": "target_failed"},
        {"outcome": "verified", "targeted": {"passed": 2}, "full": {"passed": 41}},
    ]
    body = build_pr_body(
        issue_number=12,
        issue_title="add is wrong",
        explanation="add subtracted instead of adding",
        attempts=attempts,
        before_failing=["test_calc.py::test_add"],
        reviewer="Sam",
        note="looks right",
    )
    assert "#12" in body and "add subtracted" in body
    assert "`test_calc.py::test_add`" in body
    assert "2 passed" in body and "41 passed" in body and "Attempts needed: 2" in body
    assert "Approved by Sam" in body and "looks right" in body
    assert "draft written by an AI model" in body
