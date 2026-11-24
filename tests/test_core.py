"""Unit tests for the pure checking logic, driven by fixture commit messages."""

from pathlib import Path

import pytest

from trailer_guard import core
from trailer_guard.core import Commit, ConfigError

FIXTURES = Path(__file__).parent / "fixtures" / "messages"
KEY = "Change-Source"
PATTERN = "(human|bot)-[a-z0-9-]+"
SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
PARENT = "00aa11bb22cc33dd44ee55ff66778899aabbccdd"
OTHER_PARENT = "77889900aabbccddeeff00112233445566778899"


def message(name: str) -> str:
    # Read bytes so the CRLF fixture keeps its line endings on every platform.
    return (FIXTURES / f"{name}.txt").read_bytes().decode("utf-8")


def commit(name: str, sha: str = SHA, parents=(PARENT,)) -> Commit:
    return Commit(sha, tuple(parents), message(name))


def policy(grandfathered=()):
    return core.build_policy(KEY, PATTERN, grandfathered)


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("valid", core.OK),
        ("lowercase_key", core.OK),
        ("multiple_trailers", core.OK),
        ("folded", core.OK),
        ("crlf", core.OK),
        ("missing", core.MISSING),
        ("subject_only", core.MISSING),
        ("trailer_as_subject", core.MISSING),
        ("body_only", core.MISSING),
        ("mixed_last", core.MISSING),
        ("wrong_value", core.BAD_VALUE),
        ("duplicate_one_bad", core.BAD_VALUE),
        ("empty_value", core.BAD_VALUE),
    ],
)
def test_fixture_messages(fixture, expected):
    assert core.check_commit(commit(fixture), policy()).status == expected


def test_parse_trailers_returns_all_pairs_in_order():
    assert core.parse_trailers(message("multiple_trailers")) == [
        ("Reviewed-by", "Alex Sample <alex.sample@example.com>"),
        ("Change-Source", "bot-codegen"),
        ("Refs", "TICKET-0142"),
    ]


def test_parse_trailers_folds_continuation_lines():
    trailers = core.parse_trailers(message("folded"))
    assert trailers[0] == ("Notes", "first line of a folded value that continues here")
    assert trailers[1] == ("Change-Source", "human-laptop")


def test_parse_trailers_empty_and_blank_messages():
    assert core.parse_trailers("") == []
    assert core.parse_trailers("\n\n  \n") == []


def test_ok_result_carries_the_value():
    result = core.check_commit(commit("valid"), policy())
    assert result.detail == "human-laptop"
    assert not result.failed


def test_bad_value_detail_names_the_value_and_pattern():
    result = core.check_commit(commit("wrong_value"), policy())
    assert result.failed
    assert "Robot 7" in result.detail and PATTERN in result.detail


def test_value_pattern_must_match_the_whole_value():
    loose = core.build_policy(KEY, "human")
    assert core.check_commit(commit("valid"), loose).status == core.BAD_VALUE


def test_default_pattern_accepts_any_non_empty_value():
    anything = core.build_policy(KEY)
    assert core.check_commit(commit("wrong_value"), anything).status == core.OK
    assert core.check_commit(commit("empty_value"), anything).status == core.BAD_VALUE


def test_merge_commit_is_skipped_even_without_trailer():
    merge = commit("missing", parents=(PARENT, OTHER_PARENT))
    assert merge.is_merge
    assert core.check_commit(merge, policy()).status == core.MERGE


def test_root_commit_without_parents_is_still_checked():
    root = commit("missing", parents=())
    assert core.check_commit(root, policy()).status == core.MISSING


def test_grandfathered_full_sha_is_skipped():
    result = core.check_commit(commit("missing"), policy([SHA]))
    assert result.status == core.GRANDFATHERED


def test_grandfathered_prefix_and_case_are_accepted():
    result = core.check_commit(commit("missing"), policy([SHA[:12].upper()]))
    assert result.status == core.GRANDFATHERED


def test_grandfather_list_does_not_cover_other_commits():
    other = "f" * 40
    assert core.check_commit(commit("missing", sha=other), policy([SHA])).status == core.MISSING


def test_parse_grandfather_text_handles_comments_and_blanks():
    text = f"# header\n\n{SHA}  # reason\n  {SHA[:8].upper()}\n"
    assert core.parse_grandfather_text(text) == [SHA, SHA[:8]]


@pytest.mark.parametrize("line", ["abc123", "not-a-sha-at-all", "g" * 40, "1a2b3c4 extra"])
def test_parse_grandfather_text_rejects_bad_lines(line):
    with pytest.raises(ConfigError, match="line 2"):
        core.parse_grandfather_text(f"# header\n{line}\n")


@pytest.mark.parametrize("key", ["", "   ", "Has Space", "Bad:Key", "-leading"])
def test_build_policy_rejects_bad_keys(key):
    with pytest.raises(ConfigError):
        core.build_policy(key, PATTERN)


def test_build_policy_rejects_bad_regex():
    with pytest.raises(ConfigError, match="not a valid regex"):
        core.build_policy(KEY, "(human|bot")


def test_build_policy_rejects_short_grandfather_sha():
    with pytest.raises(ConfigError, match="7 to 64"):
        core.build_policy(KEY, PATTERN, ["abc12"])


def test_summarize_counts_each_status():
    commits = [
        commit("valid", sha="a" * 40),
        commit("missing", sha="b" * 40),
        commit("wrong_value", sha="c" * 40),
        commit("missing", sha="d" * 40, parents=(PARENT, OTHER_PARENT)),
        commit("missing", sha="e" * 40),
    ]
    stats = core.summarize(core.check_commits(commits, policy(["e" * 40])))
    assert stats == {
        "total": 5, "checked": 3, "ok": 1, "failed": 2, "skipped": 2,
        "missing": 1, "bad_value": 1, "merge": 1, "grandfathered": 1, "passed": False,
    }


def test_empty_range_passes():
    assert core.summarize([])["passed"] is True
