"""
CLI entry point for toelatingen data migrations.

Usage:
    python -m dossier_toelatingen.data_migrations [--dry-run] [--config PATH]
"""

import argparse
import asyncio
import logging
import os
import sys

# Guard against monorepo namespace-package collision.
# When run from the repo root, Python adds '' (cwd) to sys.path,
# which makes outer project directories shadow pip-installed packages.
_script_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(_script_dir)))
_to_remove = [p for p in sys.path if os.path.abspath(p) == _repo_root]
for _p in _to_remove:
    sys.path.remove(_p)

from dossier_engine.migrations import run_migrations
from dossier_toelatingen.data_migrations import MIGRATIONS


def main():
    parser = argparse.ArgumentParser(
        description="Run toelatingen data migrations",
    )
    parser.add_argument(
        "--config",
        default="dossier_app_repo/dossier_app/config.yaml",
        help="Path to config.yaml (default: dossier_app_repo/dossier_app/config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without writing anything",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    summary = asyncio.run(
        run_migrations(
            MIGRATIONS,
            config_path=args.config,
            dry_run=args.dry_run,
        )
    )

    # Print summary
    total_applied = sum(r["applied"] for r in summary.values())
    total_errors = sum(r["errors"] for r in summary.values())
    print(f"\nDone. Applied: {total_applied}, Errors: {total_errors}")
    if total_errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
