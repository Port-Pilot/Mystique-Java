#!/usr/bin/env python3
"""Populate Neon with eligible Java rows and Git patches from BackportBench.

Existing rows are refreshed without changing their status or generated result.
New rows are inserted with status ``pending`` so the generation runner can
resume by processing every row whose status is not ``completed``.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import run_mystique_backportbench_java as benchmark


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, default=benchmark.WORKBOOK)
    parser.add_argument("--repos-dir", type=Path, default=benchmark.DEFAULT_REPOS_DIR)
    parser.add_argument("--case", help="workbook row number or BackportBench-Java-<row>")
    parser.add_argument("--count", "--limit", dest="count", type=int)
    parser.add_argument("--model", default=os.getenv("MYSTIQUE_MODEL", "gpt-5.5"))
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    benchmark.load_environment()
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.count is not None and args.count < 1:
        raise SystemExit("--count must be greater than zero")
    if not args.workbook.is_file():
        raise SystemExit(f"Workbook was not found: {args.workbook}")
    database_url = os.getenv("NEON_DATABASE_URL")
    if not database_url:
        raise SystemExit("Set NEON_DATABASE_URL in the environment or .env")

    try:
        import psycopg2
    except ImportError as error:
        raise SystemExit(
            "Install psycopg2-binary from ../Javabackports/requirements-mystique-java.txt"
        ) from error

    cases = benchmark.select_cases(
        benchmark.load_cases(args.workbook), args.case, args.count
    )
    logging.info("Selected %d eligible Java workbook row(s)", len(cases))
    connection = psycopg2.connect(database_url)
    inserted_or_updated = 0
    try:
        for index, case in enumerate(cases, start=1):
            repositories = benchmark.ensure_selected_repositories(
                [case], args.repos_dir
            )
            repository = repositories[case.owner_repo]
            source_patch = benchmark.git_patch(repository, case.source_sha)
            target_patch = benchmark.git_patch(repository, case.target_sha)
            with connection.cursor() as cursor:
                database_id = benchmark.ensure_database_row(
                    cursor, case, source_patch, target_patch, args.model
                )
            connection.commit()
            inserted_or_updated += 1
            logging.info(
                "[%d/%d] workbook row %d stored as database row %d",
                index, len(cases), case.excel_row, database_id,
            )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    logging.info("Populated %d BackportBench Java database row(s)", inserted_or_updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
