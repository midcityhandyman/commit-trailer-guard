"""Command line entry point: collect commits, apply the policy, report.

Commits come from one of two sources:
  * git:  `git log base..head` in a checked out repository (the Action path)
  * json: a file of synthetic commits (the offline demo and test path)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence, TextIO

from . import core
from .core import Commit, ConfigError, Policy, Result

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"
GIT_LOG_FORMAT = "--format=%H%x1f%P%x1f%B%x1e"
ZERO_SHA_RE = re.compile(r"^0+$")

LABELS = {
    core.OK: "ok",
    core.MISSING: "MISSING",
    core.BAD_VALUE: "BAD VALUE",
    core.MERGE: "skip (merge)",
    core.GRANDFATHERED: "skip (grandfathered)",
}

GitRunner = Callable[[Sequence[str], str], str]


class GitError(RuntimeError):
    pass


def run_git(args: Sequence[str], repo: str = ".") -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found") from exc
    if proc.returncode != 0:
        raise GitError(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout


def parse_git_log(raw: str) -> List[Commit]:
    """Parse output of `git log` produced with GIT_LOG_FORMAT."""
    commits = []
    for record in raw.split(RECORD_SEP):
        record = record.lstrip("\n")
        if not record.strip():
            continue
        parts = record.split(FIELD_SEP, 2)
        if len(parts) != 3:
            raise GitError("unexpected git log output; cannot parse commit record")
        sha, parents, message = parts
        commits.append(Commit(sha.strip(), tuple(parents.split()), message))
    return commits


def _check_revision(name: str, value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ConfigError(
            f"{name} revision is required (or use --from-json). On GitHub this "
            "check is meant for pull_request events."
        )
    if value.startswith("-"):
        raise ConfigError(f"{name} revision {value!r} is not a valid revision")
    if ZERO_SHA_RE.match(value):
        raise ConfigError(
            f"{name} revision is all zeros (a new branch push has no base). "
            "Run this check on pull_request events."
        )
    return value


def read_git_commits(base: str, head: str, repo: str, git: GitRunner) -> List[Commit]:
    base = _check_revision("base", base)
    head = _check_revision("head", head)
    raw = git(["log", "--reverse", GIT_LOG_FORMAT, f"{base}..{head}"], repo)
    return parse_git_log(raw)


def load_json_commits(path: str) -> List[Commit]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read commits file {path}: {exc.strerror}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"commits file {path} is not valid JSON: {exc}") from exc
    items = data.get("commits") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ConfigError(f"commits file {path} must hold a list of commits")
    commits = []
    for index, item in enumerate(items):
        try:
            sha, message, parents = item["sha"], item["message"], item.get("parents", [])
        except (KeyError, TypeError, AttributeError) as exc:
            raise ConfigError(f"commits file {path}: entry {index} needs sha and message") from exc
        # A string here would be split into characters and read as a merge, which is skipped.
        if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
            raise ConfigError(f"commits file {path}: entry {index} parents must be a list of SHAs")
        commits.append(Commit(str(sha), tuple(parents), str(message)))
    return commits


def load_grandfather(
    path: Optional[str],
    ref: Optional[str],
    inline: Optional[str],
    repo: str,
    git: GitRunner,
    notes: List[str],
) -> List[str]:
    """Collect grandfathered SHAs from a file (on disk or at a git ref) and inline text."""
    shas: List[str] = []
    if path:
        if ref:
            if ref.startswith("-"):
                raise ConfigError(f"grandfather ref {ref!r} is not a valid revision")
            try:
                text = git(["show", f"{ref}:{path}"], repo)
            except GitError:
                notes.append(
                    f"grandfather file {path} not found at {ref[:10]}; treating the list as empty"
                )
                text = ""
        else:
            try:
                text = Path(repo, path).read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot read grandfather file {path}: {exc.strerror}") from exc
        shas.extend(core.parse_grandfather_text(text))
    if inline:
        shas.extend(part for part in re.split(r"[,\s]+", inline.strip()) if part)
    return shas


def render_text(results: Sequence[Result], policy: Policy, notes: Sequence[str] = ()) -> str:
    stats = core.summarize(results)
    lines = [f"commit-trailer-guard: require '{policy.key}: <{policy.value_pattern.pattern}>'"]
    lines.extend(f"note: {note}" for note in notes)
    if not results:
        lines.append("  (no commits in range)")
    for result in results:
        line = f"  {LABELS[result.status]:<21}{result.commit.sha[:10]:<12}{result.commit.subject}"
        lines.append(line)
        if result.failed:
            lines.append(f"  {'':<21}{'':<12}-> {result.detail}")
    lines.append(
        "Checked {checked} commits: {ok} ok, {failed} failed. "
        "Skipped {skipped} ({merge} merge, {grandfathered} grandfathered).".format(**stats)
    )
    if stats["passed"]:
        lines.append("RESULT: PASS")
    else:
        lines.append("RESULT: FAIL")
        lines.append("How to fix:")
        lines.append(
            f'  latest commit:  git commit --amend --no-edit --trailer "{policy.key}: <value>"'
        )
        lines.append(
            "  older commits:  git rebase <base> -x "
            f"'git commit --amend --no-edit --trailer \"{policy.key}: <value>\"'"
        )
        lines.append(
            "  The trailer must sit in the last paragraph of the message. Commits already on a"
        )
        lines.append("  protected branch cannot be rewritten; add their SHAs to the grandfather list.")
    return "\n".join(lines)


def render_summary_markdown(results: Sequence[Result], policy: Policy) -> str:
    stats = core.summarize(results)
    verdict = "PASS" if stats["passed"] else "FAIL"
    lines = [
        f"### commit-trailer-guard: {verdict}",
        "",
        f"Required trailer: `{policy.key}` matching `{policy.value_pattern.pattern}`",
        "",
        "| Status | Commit | Subject |",
        "| --- | --- | --- |",
    ]
    for result in results:
        subject = result.commit.subject.replace("|", "\\|")
        lines.append(f"| {LABELS[result.status]} | `{result.commit.sha[:10]}` | {subject} |")
    lines.append("")
    return "\n".join(lines)


def _escape_annotation(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def emit_github(results: Sequence[Result], policy: Policy, env: Mapping[str, str], out: TextIO) -> None:
    """Write annotations, step outputs and the step summary when running in Actions."""
    stats = core.summarize(results)
    if env.get("GITHUB_ACTIONS") == "true":
        for result in results:
            if result.failed:
                message = _escape_annotation(
                    f"{result.commit.sha[:10]} {result.commit.subject}: {result.detail}"
                )
                print(f"::error title=Commit trailer {LABELS[result.status]}::{message}", file=out)
    output_path = env.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"checked={stats['checked']}\n")
            handle.write(f"failed={stats['failed']}\n")
            handle.write(f"skipped={stats['skipped']}\n")
            handle.write(f"result={'pass' if stats['passed'] else 'fail'}\n")
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(render_summary_markdown(results, policy) + "\n")


def build_parser(env: Mapping[str, str]) -> argparse.ArgumentParser:
    def from_env(name: str, default: Optional[str] = None) -> Optional[str]:
        return env.get(name) or default

    parser = argparse.ArgumentParser(
        prog="trailer_guard",
        description="Fail when a non merge commit lacks a required trailer line.",
    )
    parser.add_argument("--key", default=from_env("TRAILER_KEY"),
                        help="required trailer key, for example Change-Source [TRAILER_KEY]")
    parser.add_argument("--value-pattern", default=from_env("TRAILER_VALUE_PATTERN", ".+"),
                        help="regex the whole trailer value must match [TRAILER_VALUE_PATTERN]")
    parser.add_argument("--grandfather-file", default=from_env("TRAILER_GRANDFATHER_FILE"),
                        help="file of exempt SHAs, one per line [TRAILER_GRANDFATHER_FILE]")
    parser.add_argument("--grandfather-ref", default=from_env("TRAILER_GRANDFATHER_REF"),
                        help="read the grandfather file from this git ref instead of the "
                             "working tree [TRAILER_GRANDFATHER_REF]")
    parser.add_argument("--grandfather-shas", default=from_env("TRAILER_GRANDFATHER_SHAS"),
                        help="extra exempt SHAs, comma or space separated [TRAILER_GRANDFATHER_SHAS]")
    parser.add_argument("--base", default=from_env("TRAILER_BASE_SHA"),
                        help="base revision, exclusive [TRAILER_BASE_SHA]")
    parser.add_argument("--head", default=from_env("TRAILER_HEAD_SHA"),
                        help="head revision, inclusive [TRAILER_HEAD_SHA]")
    parser.add_argument("--repo", default=".", help="path to the git repository")
    parser.add_argument("--from-json", default=None,
                        help="read commits from a JSON file instead of git (offline mode)")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, str]] = None,
    git: GitRunner = run_git,
    out: Optional[TextIO] = None,
) -> int:
    import os

    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = build_parser(env).parse_args(argv)
    notes: List[str] = []
    try:
        # With --from-json there is no repository, so the list is read from disk.
        ref = None if args.from_json else args.grandfather_ref
        grandfathered = load_grandfather(
            args.grandfather_file, ref, args.grandfather_shas, args.repo, git, notes
        )
        policy = core.build_policy(args.key or "", args.value_pattern, grandfathered)
        if args.from_json:
            commits = load_json_commits(args.from_json)
        else:
            commits = read_git_commits(args.base or "", args.head or "", args.repo, git)
    except (ConfigError, GitError) as exc:
        print(f"commit-trailer-guard: configuration error: {exc}", file=out)
        if env.get("GITHUB_ACTIONS") == "true":
            print(f"::error title=commit-trailer-guard::{_escape_annotation(str(exc))}", file=out)
        return EXIT_CONFIG
    results = core.check_commits(commits, policy)
    print(render_text(results, policy, notes), file=out)
    emit_github(results, policy, env, out)
    return EXIT_PASS if core.summarize(results)["passed"] else EXIT_FAIL
