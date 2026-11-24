"""Pure checking logic for commit-trailer-guard.

Nothing in this module touches git, the filesystem or the environment, so
every rule can be unit tested against plain commit message strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

TRAILER_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
TRAILER_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)[ \t]*:[ \t]*(.*)$")
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")

OK = "ok"
MISSING = "missing"
BAD_VALUE = "bad-value"
MERGE = "merge"
GRANDFATHERED = "grandfathered"

FAILING = (MISSING, BAD_VALUE)
SKIPPED = (MERGE, GRANDFATHERED)


class ConfigError(ValueError):
    """Raised when the policy or its inputs are not usable."""


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: Tuple[str, ...]
    message: str

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def subject(self) -> str:
        for line in self.message.splitlines():
            if line.strip():
                return line.strip()
        return "(empty message)"


@dataclass(frozen=True)
class Policy:
    key: str
    value_pattern: "re.Pattern[str]"
    grandfathered: Tuple[str, ...] = ()

    def is_grandfathered(self, sha: str) -> bool:
        sha = sha.lower()
        return any(sha.startswith(prefix) for prefix in self.grandfathered)


@dataclass(frozen=True)
class Result:
    commit: Commit
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status in FAILING


def build_policy(key: str, value_pattern: str = ".+", grandfathered: Iterable[str] = ()) -> Policy:
    """Validate raw settings and return a Policy, or raise ConfigError."""
    key = (key or "").strip()
    if not key:
        raise ConfigError("trailer key is required")
    if not TRAILER_KEY_RE.match(key):
        raise ConfigError(
            f"trailer key {key!r} is not valid: use letters, digits and hyphens only"
        )
    try:
        compiled = re.compile(value_pattern or ".+")
    except re.error as exc:
        raise ConfigError(f"value pattern {value_pattern!r} is not a valid regex: {exc}") from exc
    shas = []
    for raw in grandfathered:
        sha = raw.strip().lower()
        if not SHA_RE.match(sha):
            raise ConfigError(
                f"grandfathered SHA {raw!r} is not valid: need 7 to 64 hex characters"
            )
        shas.append(sha)
    return Policy(key=key, value_pattern=compiled, grandfathered=tuple(dict.fromkeys(shas)))


def parse_grandfather_text(text: str) -> List[str]:
    """Parse a grandfather list: one SHA per line, '#' starts a comment."""
    shas = []
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.split("#", 1)[0].strip().lower()
        if not entry:
            continue
        if not SHA_RE.match(entry):
            raise ConfigError(
                f"grandfather list line {number}: {entry!r} is not a SHA (7 to 64 hex characters)"
            )
        shas.append(entry)
    return shas


def parse_trailers(message: str) -> List[Tuple[str, str]]:
    """Return (key, value) pairs from the trailer block of a commit message.

    The trailer block is the last paragraph of the message. It only counts
    when the message has more than one paragraph (a subject alone is never a
    trailer block) and when every line in the paragraph is a "Key: value"
    line or an indented continuation of one. This is stricter than git
    itself, on purpose: a required trailer should not be satisfied by a
    sentence in the body that happens to contain the key.
    """
    text = message.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    blank_indexes = [index for index, line in enumerate(lines) if not line]
    if not blank_indexes:
        return []
    block = lines[blank_indexes[-1] + 1:]
    trailers: List[Tuple[str, str]] = []
    for line in block:
        if line[0] in " \t":
            if not trailers:
                return []
            key, value = trailers[-1]
            trailers[-1] = (key, (value + " " + line.strip()).strip())
            continue
        match = TRAILER_LINE_RE.match(line)
        if not match:
            return []
        trailers.append((match.group(1), match.group(2).strip()))
    return trailers


def check_commit(commit: Commit, policy: Policy) -> Result:
    """Apply the policy to one commit."""
    if commit.is_merge:
        return Result(commit, MERGE)
    if policy.is_grandfathered(commit.sha):
        return Result(commit, GRANDFATHERED)
    wanted = policy.key.lower()
    values = [value for key, value in parse_trailers(commit.message) if key.lower() == wanted]
    if not values:
        return Result(commit, MISSING, f"no {policy.key} trailer in the last paragraph")
    bad = [value for value in values if not policy.value_pattern.fullmatch(value)]
    if bad:
        return Result(
            commit,
            BAD_VALUE,
            f'value "{bad[0]}" does not match {policy.value_pattern.pattern}',
        )
    return Result(commit, OK, values[0])


def check_commits(commits: Sequence[Commit], policy: Policy) -> List[Result]:
    return [check_commit(commit, policy) for commit in commits]


def summarize(results: Sequence[Result]) -> dict:
    """Count results by status. 'checked' excludes skipped commits."""
    counts = {status: 0 for status in (OK, MISSING, BAD_VALUE, MERGE, GRANDFATHERED)}
    for result in results:
        counts[result.status] += 1
    failed = counts[MISSING] + counts[BAD_VALUE]
    skipped = counts[MERGE] + counts[GRANDFATHERED]
    return {
        "total": len(results),
        "checked": len(results) - skipped,
        "ok": counts[OK],
        "failed": failed,
        "skipped": skipped,
        "missing": counts[MISSING],
        "bad_value": counts[BAD_VALUE],
        "merge": counts[MERGE],
        "grandfathered": counts[GRANDFATHERED],
        "passed": failed == 0,
    }
