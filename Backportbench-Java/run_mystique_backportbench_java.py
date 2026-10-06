#!/usr/bin/env python3
"""Generate Mystique patches for BackportBench Java rows stored in Neon.

Run ``populate_backportbench_java.py`` first. This runner treats Neon as the
source of truth and resumes by selecting pending, running, and failed rows.
Rows left as ``running`` by an interrupted process are therefore retried.

Examples:
    python3 run_mystique_backportbench_java.py --build-input --count 1 --dry-run
    python3 run_mystique_backportbench_java.py --build-input --case 700
    python3 run_mystique_backportbench_java.py
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
WORKBOOK = ROOT / "backportbench.xlsx"
BASE_RUNNER = ROOT.parent / "Javabackports" / "run_mystique_java.py"
MYSTIQUE_SRC = ROOT.parent / "mystique-opensource.github.io" / "src"
DEFAULT_FULL_INPUT = ROOT / "backportbench-java-full.json"
DEFAULT_REPOS_DIR = ROOT / ".mystique-backportbench-repos"
TABLE = "backport_benchmark_results_mystique_backportbench_java"
DATASET = "BackportBench"
CASE_PREFIX = "BackportBench-Java-"
COMMIT_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/commits?/"
    r"(?P<sha>[0-9a-fA-F]{7,40})(?:[/?#].*)?$"
)
CELL_RE = re.compile(r"([A-Z]+)")


@dataclass(frozen=True)
class BenchmarkCase:
    excel_row: int
    vulnerability_url: str
    file_change: str
    content_change: str
    source_number: int
    commit1_url: str
    commit2_url: str
    project: str
    owner_repo: str
    source_url: str
    source_sha: str
    target_url: str
    target_sha: str

    @property
    def key(self) -> str:
        return f"{CASE_PREFIX}{self.excel_row}"

    @property
    def patch_type(self) -> str | None:
        values = [value for value in (self.file_change, self.content_change) if value]
        return "; ".join(values) or None


@dataclass(frozen=True)
class DatabaseCase:
    database_id: int
    project: str
    owner_repo: str
    source_url: str
    source_sha: str
    source_patch: str
    target_url: str
    target_sha: str
    target_patch: str
    status: str

    @property
    def key(self) -> str:
        return f"{CASE_PREFIX}{self.database_id}"


def load_base_runner() -> Any:
    if not BASE_RUNNER.is_file():
        raise SystemExit(f"Existing Java runner was not found: {BASE_RUNNER}")
    spec = importlib.util.spec_from_file_location("mystique_java_runner", BASE_RUNNER)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Unable to load existing Java runner: {BASE_RUNNER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # The custom runner may be stored beside the upstream checkout rather than
    # containing it. Override its module constant only; never edit upstream.
    module.MYSTIQUE_SRC = MYSTIQUE_SRC
    return module


def load_environment() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT.parent / ".env")
    load_dotenv(ROOT / ".env")


def column_index(reference: str) -> int:
    match = CELL_RE.match(reference)
    if not match:
        raise ValueError(f"Invalid spreadsheet cell reference: {reference!r}")
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def read_xlsx_rows(path: Path) -> list[list[str]]:
    """Read the first XLSX worksheet using only the Python standard library."""
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationships_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns = {"m": spreadsheet_ns, "r": relationships_ns}
    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.iter(f"{{{spreadsheet_ns}}}t"))
                for item in shared_root.findall("m:si", ns)
            ]

        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        first_sheet = workbook.find("m:sheets/m:sheet", ns)
        if first_sheet is None:
            raise ValueError(f"Workbook has no worksheets: {path}")
        relationship_id = first_sheet.attrib[f"{{{relationships_ns}}}id"]
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        target = next(
            relation.attrib["Target"]
            for relation in relationships
            if relation.attrib["Id"] == relationship_id
        ).lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        sheet = ElementTree.fromstring(archive.read(target))

        rows: list[list[str]] = []
        for row in sheet.findall(".//m:sheetData/m:row", ns):
            values: dict[int, str] = {}
            for cell in row.findall("m:c", ns):
                index = column_index(cell.attrib["r"])
                cell_type = cell.attrib.get("t")
                if cell_type == "inlineStr":
                    inline = cell.find("m:is", ns)
                    value = "" if inline is None else "".join(
                        node.text or ""
                        for node in inline.iter(f"{{{spreadsheet_ns}}}t")
                    )
                else:
                    raw = cell.find("m:v", ns)
                    value = "" if raw is None else raw.text or ""
                    if cell_type == "s" and value:
                        value = shared_strings[int(value)]
                values[index] = value.strip()
            width = max(values, default=-1) + 1
            rows.append([values.get(index, "") for index in range(width)])
    return rows


def parse_commit_url(url: str) -> tuple[str, str]:
    match = COMMIT_URL_RE.match(url.strip())
    if not match:
        raise ValueError(f"Unsupported GitHub commit URL: {url!r}")
    return f"{match.group('owner')}/{match.group('repo')}", match.group("sha").lower()


def load_cases(path: Path) -> list[BenchmarkCase]:
    rows = read_xlsx_rows(path)
    if not rows:
        raise ValueError(f"Workbook is empty: {path}")
    headers = {name.strip().lower(): index for index, name in enumerate(rows[0])}
    required = {
        "vulnerabilty url", "file change", "content change",
        "have backport relationship", "which commit is source",
        "commit1 url", "commit2 url", "repository", "ecosystem",
    }
    missing = sorted(required - headers.keys())
    if missing:
        raise ValueError(f"Workbook is missing columns: {', '.join(missing)}")

    def get(row: list[str], name: str) -> str:
        index = headers[name]
        return row[index].strip() if index < len(row) else ""

    cases: list[BenchmarkCase] = []
    for excel_row, row in enumerate(rows[1:], start=2):
        if get(row, "ecosystem").lower() != "maven":
            continue
        if get(row, "have backport relationship").lower() != "yes":
            continue
        source_text = get(row, "which commit is source")
        if source_text not in {"1", "2"}:
            logging.warning("Skipping workbook row %d: invalid source %r", excel_row, source_text)
            continue
        commit1_url = get(row, "commit1 url")
        commit2_url = get(row, "commit2 url")
        repo1, sha1 = parse_commit_url(commit1_url)
        repo2, sha2 = parse_commit_url(commit2_url)
        if repo1 != repo2:
            logging.warning(
                "Skipping workbook row %d: commits use different repositories", excel_row
            )
            continue
        source_number = int(source_text)
        cases.append(BenchmarkCase(
            excel_row=excel_row,
            vulnerability_url=get(row, "vulnerabilty url"),
            file_change=get(row, "file change"),
            content_change=get(row, "content change"),
            source_number=source_number,
            commit1_url=commit1_url,
            commit2_url=commit2_url,
            project=get(row, "repository") or repo1.split("/", 1)[1],
            owner_repo=repo1,
            source_url=commit1_url if source_number == 1 else commit2_url,
            source_sha=sha1 if source_number == 1 else sha2,
            target_url=commit2_url if source_number == 1 else commit1_url,
            target_sha=sha2 if source_number == 1 else sha1,
        ))
    return cases


def select_cases(cases: list[BenchmarkCase], requested: str | None,
                 count: int | None) -> list[BenchmarkCase]:
    if requested:
        normalized = requested if requested.startswith(CASE_PREFIX) else f"{CASE_PREFIX}{requested}"
        cases = [case for case in cases if case.key == normalized]
        if not cases:
            raise SystemExit(f"Case {requested!r} is not an eligible Java workbook row")
    return cases[:count] if count is not None else cases


def fetch_database_cases(connection: Any, requested: str | None,
                         count: int | None,
                         include_completed: bool = False) -> list[DatabaseCase]:
    """Load generation work from Neon in stable ID order."""
    requested_id: int | None = None
    if requested:
        value = requested.removeprefix(CASE_PREFIX)
        try:
            requested_id = int(value)
        except ValueError as error:
            raise SystemExit(
                f"--case must be a database ID or {CASE_PREFIX}<ID>"
            ) from error

    conditions = ["dataset = %s", "lower(programming_language) = 'java'"]
    parameters: list[Any] = [DATASET]
    if not include_completed:
        conditions.append(
            "(status IS NULL OR lower(status) IN ('pending', 'running', 'failed') "
            "OR lower(status) LIKE 'failed:%')"
        )
    if requested_id is not None:
        conditions.append("id = %s")
        parameters.append(requested_id)

    limit_sql = ""
    if count is not None:
        limit_sql = " LIMIT %s"
        parameters.append(count)
    query = f"""
        SELECT id, project,
               new_version_patch_commit_url, new_version_patch,
               old_version_patch_commit_url, old_version_patch,
               coalesce(status, 'pending')
        FROM {TABLE}
        WHERE {' AND '.join(conditions)}
        ORDER BY id{limit_sql}
    """
    with connection.cursor() as cursor:
        cursor.execute(query, parameters)
        rows = cursor.fetchall()

    cases: list[DatabaseCase] = []
    for (database_id, project, source_url, source_patch, target_url,
         target_patch, status) in rows:
        source_repo, source_sha = parse_commit_url(source_url or "")
        target_repo, target_sha = parse_commit_url(target_url or "")
        if source_repo != target_repo:
            raise ValueError(
                f"Database row {database_id} uses different source and target repositories"
            )
        if not source_patch or not target_patch:
            raise ValueError(f"Database row {database_id} has missing patch text")
        cases.append(DatabaseCase(
            database_id=int(database_id), project=project,
            owner_repo=source_repo, source_url=source_url, source_sha=source_sha,
            source_patch=source_patch, target_url=target_url, target_sha=target_sha,
            target_patch=target_patch, status=status,
        ))
    if requested and not cases:
        qualifier = "matching unfinished" if not include_completed else "matching"
        raise SystemExit(f"No {qualifier} database row was found for --case {requested}")
    return cases


def git_patch(repo_path: Path, sha: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "show", "--format=", "--no-ext-diff", sha],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", check=False, timeout=300,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Timed out after 300 seconds generating patch for {sha}"
        ) from error
    if completed.returncode != 0:
        raise RuntimeError(
            f"git show failed for {sha}: {completed.stderr.strip()}"
        )
    return completed.stdout


def has_commit(repo_path: Path, sha: str) -> bool:
    # These repositories are partial clones.  Without GIT_NO_LAZY_FETCH,
    # cat-file tries to retrieve a missing object from the promisor remote just
    # to answer this existence check.  On large repositories that can turn
    # into an enormous unbounded fetch.  Missing commits are fetched explicitly
    # (and with a timeout) by ensure_selected_repositories below.
    environment = os.environ.copy()
    environment["GIT_NO_LAZY_FETCH"] = "1"
    # A commit at a shallow boundary is not sufficient: `git show` treats it
    # as a root commit and attempts to output every file in the repository.
    # Requiring its parent makes the depth-2 fetch below repair such caches.
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet",
         f"{sha}^"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        env=environment,
    ).returncode == 0


def ensure_selected_repositories(
    cases: list[BenchmarkCase] | list[DatabaseCase], repos_dir: Path
) -> dict[str, Path]:
    """Fetch only selected commits and their parents, not entire large repos."""
    required: dict[str, set[str]] = {}
    for case in cases:
        required.setdefault(case.owner_repo, set()).update(
            (case.source_sha, case.target_sha)
        )

    repos_dir.mkdir(parents=True, exist_ok=True)
    repositories: dict[str, Path] = {}
    for owner_repo, commits in required.items():
        repo_path = repos_dir / owner_repo.replace("/", "__")
        repo_path.mkdir(parents=True, exist_ok=True)
        if not (repo_path / ".git").is_dir():
            subprocess.run(
                ["git", "-C", str(repo_path), "init", "--quiet"], check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_path), "remote", "add", "origin",
                 f"https://github.com/{owner_repo}.git"],
                check=True,
            )

        missing = sorted(sha for sha in commits if not has_commit(repo_path, sha))
        for index, sha in enumerate(missing, start=1):
            logging.info(
                "[%s] fetching selected commit %d/%d (%s)",
                owner_repo, index, len(missing), sha[:12],
            )
            for attempt in range(1, 4):
                try:
                    completed = subprocess.run(
                        ["git", "-C", str(repo_path), "fetch", "--quiet",
                         "--no-tags", "--depth=2", "--filter=blob:none",
                         "origin", sha],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        check=False, timeout=300,
                    )
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError(
                        f"Timed out after 300 seconds fetching {owner_repo}@{sha}"
                    ) from error
                if completed.returncode == 0:
                    break
                error_text = completed.stderr.strip()
                if "shallow file has changed since we read it" in error_text and attempt < 3:
                    logging.warning(
                        "[%s] shallow metadata changed during fetch; retrying (%d/3)",
                        owner_repo, attempt + 1,
                    )
                    continue
                raise RuntimeError(
                    f"Unable to fetch {owner_repo}@{sha}: {error_text}"
                )
        repositories[owner_repo] = repo_path
    return repositories


def prepare_inputs(cases: list[DatabaseCase], args: argparse.Namespace,
                   base: Any, runtime: dict[str, Any]) -> None:
    sha_data = {case.key: [case.source_sha, case.target_sha] for case in cases}
    repo_data = {case.key: case.owner_repo for case in cases}
    build_args = argparse.Namespace(
        sha_file=None, repo_file=None, full_input=args.full_input,
        repos_dir=args.repos_dir, case=None, count=None,
    )

    # The base extractor accepts JSON paths; keep these generated files beside
    # the derived full input so every artifact remains local to this dataset.
    sha_path = ROOT / ".backportbench-java-commits.json"
    repo_path = ROOT / ".backportbench-java-repos.json"
    import json
    sha_path.write_text(json.dumps(sha_data, indent=2), encoding="utf-8")
    repo_path.write_text(json.dumps(repo_data, indent=2), encoding="utf-8")
    build_args.sha_file = sha_path
    build_args.repo_file = repo_path
    original_ensure_repo = base.ensure_repo
    repository_cache = ensure_selected_repositories(cases, args.repos_dir)

    def cached_ensure_repo(owner_repo: str, repos_dir: Path) -> Path:
        return repository_cache[owner_repo]

    base.ensure_repo = cached_ensure_repo
    try:
        base.build_full_input(build_args, runtime)
    finally:
        base.ensure_repo = original_ensure_repo


def ensure_database_row(cursor: Any, case: BenchmarkCase, source_patch: str,
                        target_patch: str, model: str) -> int:
    cursor.execute(
        f"""
        SELECT id FROM {TABLE}
        WHERE dataset = %s AND project = %s
          AND new_version_patch_commit_url = %s
          AND old_version_patch_commit_url = %s
        ORDER BY id LIMIT 1
        """,
        (DATASET, case.project, case.source_url, case.target_url),
    )
    existing = cursor.fetchone()
    now = datetime.now(timezone.utc)
    if existing:
        database_id = int(existing[0])
        cursor.execute(
            f"""
            UPDATE {TABLE}
            SET programming_language = %s, new_version_patch = %s,
                old_version_patch = %s, patch_type = %s, method = %s,
                file_match = %s, content_match = %s, updated_at = %s
            WHERE id = %s
            """,
            ("Java", source_patch, target_patch, case.patch_type,
             base_method_label(model), case.file_change or None,
             case.content_change or None, now, database_id),
        )
        return database_id

    cursor.execute(
        f"""
        INSERT INTO {TABLE} (
            dataset, project, programming_language,
            new_version_patch_commit_url, new_version_patch,
            old_version_patch_commit_url, old_version_patch,
            patch_type, method, created_at, updated_at, status,
            file_match, content_match
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (DATASET, case.project, "Java", case.source_url, source_patch,
         case.target_url, target_patch, case.patch_type, base_method_label(model),
         now, now, "pending", case.file_change or None,
         case.content_change or None),
    )
    return int(cursor.fetchone()[0])


def base_method_label(model: str) -> str:
    return f"Mystique ({model})"


def mark_running(cursor: Any, database_id: int, model: str) -> None:
    cursor.execute(
        f"UPDATE {TABLE} SET status = %s, method = %s, updated_at = %s WHERE id = %s",
        ("running", base_method_label(model), datetime.now(timezone.utc), database_id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"database row {database_id} does not exist")


def save_success(cursor: Any, database_id: int, generated_patch: str,
                 expected_patch: str, elapsed: float, usage: Any,
                 cost: float | None, model: str) -> None:
    identical = expected_patch.strip() == generated_patch.strip()
    cursor.execute(
        f"""
        UPDATE {TABLE}
        SET method = %s, llm_generated_patch = %s,
            execution_time_seconds = %s, number_of_llm_api_calls = %s,
            api_calls = %s, input_tokens = %s, output_tokens = %s,
            reasoning_tokens = %s, total_tokens = %s, api_cost = %s,
            is_identical = %s, status = %s, updated_at = %s
        WHERE id = %s
        """,
        (base_method_label(model), generated_patch, elapsed, usage.calls,
         usage.calls, usage.input_tokens, usage.output_tokens,
         usage.reasoning_tokens, usage.total_tokens, cost, identical,
         "completed", datetime.now(timezone.utc), database_id),
    )


def save_failure(cursor: Any, database_id: int, elapsed: float, usage: Any,
                 cost: float | None, error: Exception, model: str) -> None:
    cursor.execute(
        f"""
        UPDATE {TABLE}
        SET method = %s, execution_time_seconds = %s,
            number_of_llm_api_calls = %s, api_calls = %s,
            input_tokens = %s, output_tokens = %s, reasoning_tokens = %s,
            total_tokens = %s, api_cost = %s, status = %s, updated_at = %s
        WHERE id = %s
        """,
        (base_method_label(model), elapsed, usage.calls, usage.calls,
         usage.input_tokens, usage.output_tokens, usage.reasoning_tokens,
         usage.total_tokens, cost, "failed", datetime.now(timezone.utc), database_id),
    )


def mark_no_usable_patch(cursor: Any, database_id: int) -> None:
    cursor.execute(
        f"UPDATE {TABLE} SET status = %s, updated_at = %s WHERE id = %s",
        ("no_usable_patch", datetime.now(timezone.utc), database_id),
    )


def parse_args(base: Any) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-input", type=Path, default=DEFAULT_FULL_INPUT)
    parser.add_argument("--repos-dir", type=Path, default=DEFAULT_REPOS_DIR)
    parser.add_argument("--build-input", action="store_true")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--case", help="database ID or full BackportBench-Java-<ID> key")
    parser.add_argument("--count", "--limit", dest="count", type=int)
    parser.add_argument(
        "--include-completed", action="store_true",
        help="also select completed rows (normally they are skipped for resume)",
    )
    parser.add_argument("--model", default=os.getenv("MYSTIQUE_MODEL", base.DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.getenv("MYSTIQUE_API_BASE", base.DEFAULT_BASE_URL))
    parser.add_argument("--slice-level", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--input-cost-per-million", type=float)
    parser.add_argument("--output-cost-per-million", type=float)
    parser.add_argument("--reasoning-cost-per-million", type=float)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    base = load_base_runner()
    load_environment()
    args = parse_args(base)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.count is not None and args.count < 1:
        raise SystemExit("--count must be greater than zero")
    database_url = os.getenv("NEON_DATABASE_URL")
    if not database_url:
        raise SystemExit("Set NEON_DATABASE_URL in the environment or .env")

    runtime = base.import_runtime()
    connection = runtime["psycopg2"].connect(database_url)
    cases = fetch_database_cases(
        connection, args.case, args.count, args.include_completed
    )
    logging.info("Selected %d database row(s) for generation", len(cases))
    if not cases:
        connection.close()
        logging.info("Nothing to do: all BackportBench Java rows are completed")
        return 0

    should_build = args.build_input or not args.full_input.exists()
    if not should_build:
        should_build = not base.read_json(args.full_input)
    if should_build:
        prepare_inputs(cases, args, base, runtime)
    else:
        data_keys = set(base.read_json(args.full_input))
        missing = [case.key for case in cases if case.key not in data_keys]
        if missing:
            raise SystemExit(
                f"{len(missing)} selected case(s) are absent from {args.full_input}; "
                "rerun with --build-input"
            )
    if args.build_only:
        connection.close()
        return 0

    base.require_commands(("astyle", "joern-parse", "joern-export"), "patch generation")
    base.configure_runtime_compatibility(runtime)
    full_input = base.read_json(args.full_input)
    selected = [(case, full_input[case.key]) for case in cases if case.key in full_input]
    skipped = len(cases) - len(selected)
    if skipped:
        logging.warning("Skipped %d cases with no usable Java method-level patch", skipped)
        if not args.dry_run:
            selected_keys = {case.key for case, _ in selected}
            with connection.cursor() as cursor:
                for case in cases:
                    if case.key not in selected_keys:
                        mark_no_usable_patch(cursor, case.database_id)
            connection.commit()

    client = base.create_api_client(runtime, args.base_url)
    failures = 0
    try:
        for case, case_data in selected:
            database_id = case.database_id
            if not args.dry_run:
                with connection.cursor() as cursor:
                    mark_running(cursor, database_id, args.model)
                connection.commit()
            started = time.monotonic()
            usage = base.Usage()
            try:
                generated_patch = base.process_case(
                    case.key, case_data, client, args, runtime, usage
                )
                elapsed = time.monotonic() - started
                cost = usage.cost(
                    args.input_cost_per_million,
                    args.output_cost_per_million,
                    args.reasoning_cost_per_million,
                )
                if not args.dry_run:
                    with connection.cursor() as cursor:
                        save_success(
                            cursor, database_id, generated_patch,
                            case.target_patch, elapsed, usage, cost, args.model,
                        )
                    connection.commit()
                logging.info(
                    "[%s] completed: %d calls, %d tokens, %.2fs",
                    case.key, usage.calls, usage.total_tokens, elapsed,
                )
            except Exception as error:
                failures += 1
                elapsed = time.monotonic() - started
                cost = usage.cost(
                    args.input_cost_per_million,
                    args.output_cost_per_million,
                    args.reasoning_cost_per_million,
                )
                logging.exception("[%s] failed", case.key)
                if not args.dry_run:
                    with connection.cursor() as cursor:
                        save_failure(
                            cursor, database_id, elapsed, usage, cost, error, args.model
                        )
                    connection.commit()
    finally:
        connection.close()
        client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
