"""Offline demo: run the guard over synthetic pull requests.

No git repository, network or keys are needed. Run from the repo root:

    python demo/run_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trailer_guard.cli import main  # noqa: E402

KEY = "Change-Source"
PATTERN = "(human|bot)-[a-z0-9-]+"

SCENARIOS = [
    (
        "1. Pull request with problems, no grandfather list",
        ["--from-json", "demo/pr_failing.json", "--key", KEY, "--value-pattern", PATTERN],
    ),
    (
        "2. Same pull request, old typo fix grandfathered",
        ["--from-json", "demo/pr_failing.json", "--key", KEY, "--value-pattern", PATTERN,
         "--grandfather-file", "demo/grandfather.txt"],
    ),
    (
        "3. Clean pull request (merge commit skipped, key match is case insensitive)",
        ["--from-json", "demo/pr_clean.json", "--key", KEY, "--value-pattern", PATTERN],
    ),
    (
        "4. Broken configuration (invalid regex) is reported as exit code 2, not as a pass",
        ["--from-json", "demo/pr_clean.json", "--key", KEY, "--value-pattern", "(human|bot"],
    ),
]


def run() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")
    expected = [1, 1, 0, 2]
    codes = []
    for title, argv in SCENARIOS:
        print("=" * 78)
        print(title)
        print("$ python -m trailer_guard " + " ".join(
            f'"{arg}"' if any(ch in arg for ch in "(|") else arg for arg in argv))
        print("-" * 78)
        code = main(argv, env={}, out=sys.stdout)
        codes.append(code)
        print(f"exit code: {code}")
        print()
    print("=" * 78)
    print(f"exit codes: {codes} (expected {expected})")
    return 0 if codes == expected else 1


if __name__ == "__main__":
    import os

    os.chdir(ROOT)
    sys.exit(run())
