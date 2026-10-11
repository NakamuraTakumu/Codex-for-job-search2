#!/usr/bin/env python3
"""Build the encrypted viewer and commit it to the gh-pages branch; pushing is left to the caller."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_viewer import DEFAULT_OUTPUT, PASSWORD_ENV, main as build_viewer  # noqa: E402

BRANCH = "gh-pages"


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def main() -> int:
    if not os.environ.get(PASSWORD_ENV):
        print(f"{PASSWORD_ENV} が未設定または空のため、公開版を作れない。", file=sys.stderr)
        return 1
    if build_viewer([]) != 0:
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp) / BRANCH
        git("worktree", "add", "--quiet", str(worktree), BRANCH)
        try:
            shutil.copyfile(DEFAULT_OUTPUT, worktree / "index.html")
            (worktree / ".nojekyll").touch()
            git("add", "index.html", ".nojekyll", cwd=worktree)
            if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=worktree).returncode == 0:
                print(f"{BRANCH}: 変更なし")
            else:
                git("commit", "--quiet", "-m", "Publish viewer", cwd=worktree)
                print(f"{BRANCH}: {git('rev-parse', '--short', 'HEAD', cwd=worktree)} をコミットした。公開は git push origin {BRANCH}")
        finally:
            git("worktree", "remove", "--force", str(worktree))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
