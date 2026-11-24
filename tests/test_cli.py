"""Tests for the command line layer. Git is always faked; no repository is needed."""

import io
import json
import re
from pathlib import Path

import pytest

from trailer_guard import cli
from trailer_guard.cli import FIELD_SEP, RECORD_SEP, GitError

ROOT = Path(__file__).resolve().parent.parent
KEY = "Change-Source"
PATTERN = "(human|bot)-[a-z0-9-]+"
BASE = "00aa11bb22cc33dd44ee55ff66778899aabbccdd"
HEAD = "c" * 40
GOOD = ("a" * 40, BASE, "Add export job\n\nChange-Source: human-desktop\n")
BAD = ("b" * 40, "a" * 40, "Fix typo\n")
MERGE = ("d" * 40, "b" * 40 + " " + "e" * 40, "Merge branch 'main'\n")


def raw_log(*records):
    return "".join(
        f"{sha}{FIELD_SEP}{parents}{FIELD_SEP}{message}{RECORD_SEP}\n"
        for sha, parents, message in records
    )


class FakeGit:
    """Stands in for run_git: answers `log` and `show` from canned data."""

    def __init__(self, log="", files=None):
        self.log = log
        self.files = files or {}
        self.calls = []

    def __call__(self, args, repo):
        self.calls.append(list(args))
        if args[0] == "log":
            return self.log
        if args[0] == "show":
            if args[1] in self.files:
                return self.files[args[1]]
            raise GitError("git show failed: path does not exist")
        raise AssertionError(f"unexpected git call: {args}")


def run(argv, env=None, git=None):
    out = io.StringIO()
    code = cli.main(argv, env=env or {}, git=git or FakeGit(), out=out)
    return code, out.getvalue()


def test_parse_git_log_reads_sha_parents_and_message():
    commits = cli.parse_git_log(raw_log(GOOD, MERGE))
    assert [c.sha for c in commits] == [GOOD[0], MERGE[0]]
    assert commits[0].parents == (BASE,)
    assert commits[1].is_merge
    assert commits[0].message.startswith("Add export job\n\nChange-Source")


def test_parse_git_log_empty_output_means_no_commits():
    assert cli.parse_git_log("") == []
    assert cli.parse_git_log("\n") == []


def test_parse_git_log_rejects_garbage():
    with pytest.raises(GitError):
        cli.parse_git_log("no separators here" + RECORD_SEP)


def test_git_mode_passes_and_asks_git_for_the_range():
    git = FakeGit(raw_log(GOOD, MERGE))
    code, out = run(["--key", KEY, "--value-pattern", PATTERN, "--base", BASE, "--head", HEAD], git=git)
    assert code == cli.EXIT_PASS
    assert "RESULT: PASS" in out
    assert git.calls[0][0] == "log" and git.calls[0][-1] == f"{BASE}..{HEAD}"


def test_git_mode_fails_on_missing_trailer_and_explains_the_fix():
    code, out = run(
        ["--key", KEY, "--value-pattern", PATTERN, "--base", BASE, "--head", HEAD],
        git=FakeGit(raw_log(GOOD, BAD)),
    )
    assert code == cli.EXIT_FAIL
    assert "MISSING" in out and "RESULT: FAIL" in out
    assert f'--trailer "{KEY}: <value>"' in out


def test_settings_can_come_from_the_environment():
    env = {
        "TRAILER_KEY": KEY,
        "TRAILER_VALUE_PATTERN": PATTERN,
        "TRAILER_BASE_SHA": BASE,
        "TRAILER_HEAD_SHA": HEAD,
        "TRAILER_GRANDFATHER_SHAS": f"{BAD[0]}, {'f' * 12}",
    }
    code, out = run([], env=env, git=FakeGit(raw_log(GOOD, BAD)))
    assert code == cli.EXIT_PASS
    assert "skip (grandfathered)" in out


def test_empty_env_values_fall_back_to_defaults():
    env = {"TRAILER_KEY": KEY, "TRAILER_VALUE_PATTERN": "", "TRAILER_GRANDFATHER_FILE": "",
           "TRAILER_BASE_SHA": BASE, "TRAILER_HEAD_SHA": HEAD}
    code, _ = run([], env=env, git=FakeGit(raw_log(GOOD)))
    assert code == cli.EXIT_PASS


def test_grandfather_file_is_read_from_the_given_ref():
    git = FakeGit(raw_log(GOOD, BAD), files={f"{BASE}:.github/gf.txt": f"{BAD[0]}  # old\n"})
    code, out = run(
        ["--key", KEY, "--base", BASE, "--head", HEAD,
         "--grandfather-file", ".github/gf.txt", "--grandfather-ref", BASE],
        git=git,
    )
    assert code == cli.EXIT_PASS
    assert ["show", f"{BASE}:.github/gf.txt"] in git.calls


def test_grandfather_file_missing_at_ref_is_an_empty_list_with_a_note():
    code, out = run(
        ["--key", KEY, "--base", BASE, "--head", HEAD,
         "--grandfather-file", ".github/gf.txt", "--grandfather-ref", BASE],
        git=FakeGit(raw_log(GOOD, BAD)),
    )
    assert code == cli.EXIT_FAIL
    assert "note: grandfather file .github/gf.txt not found" in out


def test_grandfather_file_on_disk(tmp_path):
    (tmp_path / "gf.txt").write_text(f"# old commits\n{BAD[0]}\n", encoding="utf-8")
    code, _ = run(
        ["--key", KEY, "--base", BASE, "--head", HEAD, "--repo", str(tmp_path),
         "--grandfather-file", "gf.txt"],
        git=FakeGit(raw_log(GOOD, BAD)),
    )
    assert code == cli.EXIT_PASS


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--base", BASE, "--head", HEAD], "trailer key is required"),
        (["--key", KEY, "--head", HEAD], "base revision is required"),
        (["--key", KEY, "--base", BASE], "head revision is required"),
        (["--key", KEY, "--base", "0" * 40, "--head", HEAD], "all zeros"),
        (["--key", KEY, "--base=--output=x", "--head", HEAD], "not a valid revision"),
        (["--key", KEY, "--value-pattern", "(", "--base", BASE, "--head", HEAD], "not a valid regex"),
        (["--key", KEY, "--base", BASE, "--head", HEAD, "--grandfather-file", "nope.txt"],
         "cannot read grandfather file"),
        (["--key", KEY, "--base", BASE, "--head", HEAD, "--grandfather-shas", "xyz"],
         "is not valid"),
        (["--key", KEY, "--from-json", "does-not-exist.json"], "cannot read commits file"),
    ],
)
def test_configuration_errors_exit_2_and_never_pass(argv, expected, tmp_path):
    code, out = run(argv + ["--repo", str(tmp_path)], git=FakeGit(raw_log(GOOD)))
    assert code == cli.EXIT_CONFIG
    assert expected in out
    assert "RESULT: PASS" not in out


def test_git_failure_is_a_configuration_error():
    def broken(args, repo):
        raise GitError("git log failed: bad revision")

    code, out = run(["--key", KEY, "--base", BASE, "--head", HEAD], git=broken)
    assert code == cli.EXIT_CONFIG
    assert "bad revision" in out


def test_from_json_accepts_bare_list_and_wrapped_object(tmp_path):
    commits = [{"sha": GOOD[0], "parents": [BASE], "message": GOOD[2]}]
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(commits), encoding="utf-8")
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"commits": commits}), encoding="utf-8")
    for path in (bare, wrapped):
        code, _ = run(["--key", KEY, "--from-json", str(path)])
        assert code == cli.EXIT_PASS


def test_from_json_rejects_malformed_entries(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"sha": GOOD[0]}]), encoding="utf-8")
    code, out = run(["--key", KEY, "--from-json", str(path)])
    assert code == cli.EXIT_CONFIG
    assert "needs sha and message" in out


def test_from_json_rejects_parents_that_are_not_a_list(tmp_path):
    # "parents": "aabb" used to become four one-letter parents, so the commit
    # was skipped as a merge and the run passed without checking it.
    path = tmp_path / "parents.json"
    path.write_text(json.dumps([{"sha": BAD[0], "parents": "aabb", "message": BAD[2]}]), encoding="utf-8")
    code, out = run(["--key", KEY, "--from-json", str(path)])
    assert code == cli.EXIT_CONFIG
    assert "parents must be a list" in out


def test_demo_fixtures_give_the_documented_exit_codes():
    failing = str(ROOT / "demo" / "pr_failing.json")
    clean = str(ROOT / "demo" / "pr_clean.json")
    args = ["--key", KEY, "--value-pattern", PATTERN, "--repo", str(ROOT)]
    assert run(args + ["--from-json", failing])[0] == cli.EXIT_FAIL
    code, out = run(args + ["--from-json", failing, "--grandfather-file", "demo/grandfather.txt"])
    assert code == cli.EXIT_FAIL and "Checked 4 commits: 2 ok, 2 failed" in out
    assert run(args + ["--from-json", clean])[0] == cli.EXIT_PASS


def test_github_annotations_outputs_and_summary(tmp_path):
    output_file = tmp_path / "output.txt"
    summary_file = tmp_path / "summary.md"
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_OUTPUT": str(output_file),
        "GITHUB_STEP_SUMMARY": str(summary_file),
    }
    code, out = run(
        ["--key", KEY, "--value-pattern", PATTERN, "--base", BASE, "--head", HEAD],
        env=env,
        git=FakeGit(raw_log(GOOD, BAD, MERGE)),
    )
    assert code == cli.EXIT_FAIL
    assert f"::error title=Commit trailer MISSING::{BAD[0][:10]} Fix typo" in out
    assert output_file.read_text(encoding="utf-8").splitlines() == [
        "checked=2", "failed=1", "skipped=1", "result=fail",
    ]
    summary = summary_file.read_text(encoding="utf-8")
    assert "commit-trailer-guard: FAIL" in summary
    assert f"| MISSING | `{BAD[0][:10]}` | Fix typo |" in summary


def test_no_annotations_outside_github_actions():
    _, out = run(["--key", KEY, "--base", BASE, "--head", HEAD], git=FakeGit(raw_log(BAD)))
    assert "::error" not in out


def test_annotation_text_is_escaped():
    assert cli._escape_annotation("50% done\nnext") == "50%25 done%0Anext"


def test_action_yml_and_env_example_cover_every_setting_the_cli_reads():
    cli_source = (ROOT / "trailer_guard" / "cli.py").read_text(encoding="utf-8")
    names = set(re.findall(r'from_env\("(TRAILER_[A-Z_]+)"', cli_source))
    assert len(names) == 7
    action = (ROOT / "action.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in names:
        assert f"{name}:" in action, f"{name} missing from action.yml"
        assert name in env_example, f"{name} missing from .env.example"


def test_readme_sample_output_is_copied_from_the_captured_demo_output():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    block = readme.split("## What you will see", 1)[1].split("```")[1].strip()
    assert "RESULT: FAIL" in block
    assert block in (ROOT / "demo" / "output.txt").read_text(encoding="utf-8")


def test_repo_contains_no_em_dashes():
    em_dash = chr(0x2014)
    skip = {".venv", ".deps", "__pycache__", ".pytest_cache"}
    for path in ROOT.rglob("*"):
        if path.is_file() and not skip.intersection(path.parts):
            assert em_dash not in path.read_text(encoding="utf-8", errors="ignore"), path
