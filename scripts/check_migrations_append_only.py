#!/usr/bin/env python3
"""Pre-commit guard: Alembic migrations are append-only.

Rejects any git diff that modifies an existing migration file in
``alembic/versions/``. New migration files are fine; changes to
existing ones are not.

Rationale: retroactively mutating a deployed migration produces
silently-divergent schemas across deployments (the Bug 68 pattern).
Once a revision ID has ever been seen in production, its ``upgrade()``
and ``downgrade()`` bodies are frozen — changes must be expressed as
a new migration, not as edits to the existing one.

Install as a pre-commit hook by symlinking or referencing from a
.pre-commit-config.yaml entry. Or run directly in CI:

    python scripts/check_migrations_append_only.py

Exits non-zero on violation, zero otherwise.

Rules:
    * Files added under alembic/versions/**.py  → OK
    * Files modified under alembic/versions/**.py  → FAIL
    * Files deleted under alembic/versions/**.py  → FAIL
      (deletion breaks the revision chain for upgrade paths)

The check compares the working tree against ``origin/main`` (or the
configured default branch). In local pre-commit mode it compares
against ``HEAD``, which covers staged changes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MIGRATIONS_PATH = "alembic/versions/"


def _changed_files_vs(ref: str) -> list[tuple[str, str]]:
    """Return [(status, path), ...] from git diff --name-status.

    status is one of A/M/D/R/C/U etc. We only care about M and D.
    """
    result = subprocess.run(
        ["git", "diff", "--name-status", ref],
        capture_output=True,
        text=True,
        check=True,
    )
    changes: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][0]  # R100 → R, M → M
        path = parts[-1]
        changes.append((status, path))
    return changes


def _default_ref() -> str:
    """Pick a reasonable comparison base.

    Prefers origin/main. Falls back to HEAD for local pre-commit runs
    (where staged changes are what matters)."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", "origin/main"],
            capture_output=True, check=True,
        )
        return "origin/main"
    except subprocess.CalledProcessError:
        return "HEAD"


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else _default_ref()
    violations: list[str] = []

    for status, path in _changed_files_vs(ref):
        if not path.endswith(".py"):
            continue
        if MIGRATIONS_PATH not in path:
            continue
        if status in ("M", "D"):
            violations.append(f"  {status}  {path}")

    if violations:
        print(
            "ERROR: Alembic migrations are append-only. The following "
            "existing migration files were modified or deleted:\n"
            + "\n".join(violations)
            + "\n\nIf the schema change is genuinely new, add a new "
            "migration file instead. If you're refactoring the "
            "initial migration before first deploy, bypass this "
            "check with --no-verify (and make sure no revision in "
            f"the diff has ever been applied to production).\n"
            f"\nCompared against: {ref}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
