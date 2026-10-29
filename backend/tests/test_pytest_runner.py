import pytest

from app.sandbox.deps import DependencyEnv, collect_requirements, requirements_hash
from app.sandbox.pytest_runner import (
    InvalidSelector,
    extract_node_ids,
    parse_pytest_output,
    validate_selectors,
)

FAILED_OUTPUT = """\
..F                                                                      [100%]
=================================== FAILURES ===================================
___________________________________ test_add ___________________________________
test_calc.py:5: in test_add
    assert add(1, 2) == 3
E   assert -1 == 3
=========================== short test summary info ============================
FAILED test_calc.py::test_add - assert -1 == 3
ERROR tests/test_io.py::test_read - OSError: boom
1 failed, 2 passed, 1 skipped, 1 error in 0.05s
"""


def test_parse_pytest_output():
    parsed = parse_pytest_output(FAILED_OUTPUT)
    assert (parsed["passed"], parsed["failed"], parsed["errors"], parsed["skipped"]) == (2, 1, 1, 1)
    assert [(f.kind, f.node_id) for f in parsed["failures"]] == [
        ("FAILED", "test_calc.py::test_add"),
        ("ERROR", "tests/test_io.py::test_read"),
    ]
    assert parsed["failures"][0].message == "assert -1 == 3"


def test_parse_collection_error_and_empty_output():
    parsed = parse_pytest_output(
        "ERROR test_x.py - ImportError: no module named foo\n1 error in 0.1s\n"
    )
    assert parsed["errors"] == 1
    assert parsed["failures"][0].node_id == "test_x.py"
    assert parse_pytest_output("")["passed"] == 0


def test_extract_node_ids():
    text = (
        "Fails: tests/test_cfg.py::TestLoad::test_empty[a-b] and also pkg/test_x.py::test_y. "
        "Mentioning plain file.py:12 or text::like::this should not count."
    )
    assert extract_node_ids(text) == [
        "tests/test_cfg.py::TestLoad::test_empty[a-b]",
        "pkg/test_x.py::test_y",
    ]


def test_extract_node_ids_exact():
    ids = extract_node_ids("run tests/test_a.py::test_one then tests/test_a.py::test_one again")
    assert ids == ["tests/test_a.py::test_one"]


@pytest.mark.parametrize(
    "selector",
    ["tests/test_a.py", "tests/test_a.py::TestX::test_y", "a/b/test_c.py::test_d[1-2]", "tests"],
)
def test_valid_selectors(selector):
    assert validate_selectors([selector]) == [selector]


@pytest.mark.parametrize(
    "selector",
    [
        "--rootdir=/",
        "-p",
        "-x",
        "../outside.py",
        "tests/../../etc/passwd",
        "a.py; rm -rf /",
        "a.py b.py",
        "$(whoami)",
        "",
    ],
)
def test_option_injection_and_odd_selectors_are_rejected(selector):
    with pytest.raises(InvalidSelector):
        validate_selectors([selector])


def test_too_many_selectors_rejected():
    with pytest.raises(InvalidSelector):
        validate_selectors([f"t{n}.py" for n in range(26)])


def test_collect_requirements_from_files_and_pyproject(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "requests>=2.0  # http\n# comment\n\n-r other.txt\n-e .\n"
        "git+https://example.com/x.git#egg=x\nmypkg @ https://example.com/mypkg.whl\n./local\nclick\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["rich>=13", "click"]\n'
        '[project.optional-dependencies]\ntest = ["pytest-mock"]\ndocs = ["sphinx"]\n'
        '[dependency-groups]\ndev = ["hypothesis", {include-group = "test"}]\n'
    )
    found = collect_requirements(tmp_path)
    assert found.specs == ["requests>=2.0", "click", "rich>=13", "pytest-mock", "hypothesis"]
    assert "sphinx" not in found.specs  # docs extras are not needed to run tests
    assert len(found.warnings) == 5  # -r, -e, git+https, direct URL, local path


def test_collect_requirements_with_nothing(tmp_path):
    found = collect_requirements(tmp_path)
    assert found.specs == []
    assert "no dependency manifest" in found.warnings[0]


def test_requirements_hash_ignores_order_but_not_content():
    assert requirements_hash(["a", "b"], "img") == requirements_hash(["b", "a"], "img")
    assert requirements_hash(["a"], "img") != requirements_hash(["a", "b"], "img")
    assert requirements_hash(["a"], "img1") != requirements_hash(["a"], "img2")


def test_dependency_env_paths(tmp_path):
    empty = DependencyEnv(None, "none", True, [])
    mounts, env = empty.mounts_and_env()
    assert mounts == []
    assert env["PYTHONPATH"] == "/workspace:/workspace/src"

    ready = DependencyEnv(tmp_path, "k", True, [])
    mounts, env = ready.mounts_and_env()
    assert [(m.target, m.writable) for m in mounts] == [("/deps", False)]
    assert env["PYTHONPATH"].startswith("/deps:")
