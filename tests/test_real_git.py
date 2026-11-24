"""End to end test against a real git repository built in a temp folder.

The other command line tests fake git. This one runs the real `git log` and
`git show` calls, so it covers the range walk, merge detection from real
parent lists and reading the grandfather list from the base ref.
"""

import io
import shutil
import subprocess

import pytest

from trailer_guard import cli

KEY = "Change-Source"
PATTERN = "(human|bot)-[a-z0-9-]+"
LIST = ".github/trailer-grandfather.txt"
TRAILER = "\n\nChange-Source: human-desktop"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(repo, *args):
    cmd = ["git", "-c", "user.name=Test User", "-c", "user.email=test@example.com",
           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args]
    return subprocess.run(cmd, cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, content, message):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def guard(repo, base, head, *extra):
    out = io.StringIO()
    argv = ["--key", KEY, "--value-pattern", PATTERN, "--repo", str(repo),
            "--base", base, "--head", head, *extra]
    return cli.main(argv, env={}, out=out), out.getvalue()


@pytest.fixture()
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "a.txt", "a", "Start project" + TRAILER)
    return tmp_path


def test_real_repository_range_merge_and_grandfather_from_base(repo):
    legacy = commit(repo, "legacy.txt", "x", "Old commit made before the rule")
    base = commit(repo, LIST, legacy + "\n", "List the legacy commit" + TRAILER)
    from_base = ["--grandfather-file", LIST, "--grandfather-ref", base]

    git(repo, "checkout", "-q", "-b", "feature")
    good = commit(repo, "b.txt", "b", "Add export job\n\nChange-Source: bot-exporter")
    git(repo, "checkout", "-q", "main")
    commit(repo, "c.txt", "c", "Main moves on" + TRAILER)
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'main' into feature", "main")

    code, out = guard(repo, base, "HEAD")
    assert code == cli.EXIT_PASS, out
    assert f"ok                   {good[:10]}" in out
    assert "skip (merge)" in out

    bad = commit(repo, "d.txt", "d", "Fix typo")
    # The pull request tries to exempt its own bad commit; the list at base wins.
    commit(repo, LIST, f"{legacy}\n{bad}\n", "Exempt myself" + TRAILER)
    code, out = guard(repo, base, "HEAD", *from_base)
    assert code == cli.EXIT_FAIL, out
    assert f"MISSING              {bad[:10]}  Fix typo" in out

    # The legacy commit fails without the list and is skipped with it.
    assert guard(repo, f"{legacy}~1", legacy)[0] == cli.EXIT_FAIL
    code, out = guard(repo, f"{legacy}~1", legacy, *from_base)
    assert code == cli.EXIT_PASS, out
    assert "skip (grandfathered)" in out


def test_real_repository_unknown_base_is_a_configuration_error(repo):
    code, out = guard(repo, "f" * 40, "HEAD")
    assert code == cli.EXIT_CONFIG
    assert "configuration error" in out
