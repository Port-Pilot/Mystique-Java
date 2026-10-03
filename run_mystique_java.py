#!/usr/bin/env python3
"""Generate Mystique Java backports and persist them to Neon.

This runner deliberately lives outside the upstream Mystique checkout. It uses
Mystique for input extraction, semantic slicing, output cleanup, and placeholder
recovery while owning all API and database integration here.

Typical usage:
    # Build the method-level input, generate every patch, and update Neon.
    python3 run_mystique_java.py --build-input

    # Generate one selected case. The key must exactly match custom-java.json.
    python3 run_mystique_java.py --build-input --case JavaBackports-crate-3

    # Reuse an existing cve-java-custom-full.json and generate one case.
    python3 run_mystique_java.py --case JavaBackports-crate-3

    # Exercise one case without writing its result to Neon.
    python3 run_mystique_java.py --case JavaBackports-crate-3 --dry-run

Arguments:
    --sha-file PATH
        Commit-pair JSON input. Its shape is {case_key: [old_sha, new_sha]}.
        Default: custom-java.json.

    --repo-file PATH
        Repository mapping JSON input. Its shape is {case_key: "owner/repo"}.
        Default: custom-java-repos.json.

    --full-input PATH
        Method-level input consumed by Mystique. It is created when
        --build-input is supplied or when the file does not exist.
        Default: cve-java-custom-full.json.

    --repos-dir PATH
        Directory used to clone and cache source repositories while building
        the method-level input. Default: .mystique-java-repos.

    --build-input
        Rebuild --full-input from --sha-file and --repo-file before generation.
        When combined with --case or --count, the rebuilt file contains only
        the selected cases. Re-run with only --build-input before a later full
        dataset run.

    --build-only
        Build --full-input and exit without model calls or database updates.
        If the full-input file already exists, also pass --build-input to force
        it to be rebuilt.

    --case CASE_KEY
        Process one exact dataset key, for example JavaBackports-crate-3.
        Without --build-input, the key must already exist in --full-input.

    --count NUMBER, --limit NUMBER
        Process the first NUMBER dataset cases without naming them individually.
        During --build-input, exactly the first NUMBER selected entries are
        attempted. --limit remains an alias for backward compatibility.

    --model MODEL
        Model name sent to the API. Default: MYSTIQUE_MODEL or gpt-5.5.

    --base-url URL
        Base URL of the OpenAI-compatible API. Default: MYSTIQUE_API_BASE or
        http://localhost:8317/v1.

    --slice-level NUMBER
        Mystique backward/forward semantic slicing level. Default: 1.

    --overwrite
        Rebuild Mystique's cached Joern analysis artifacts for each method.

    --dry-run
        Generate patches and make model calls, but do not connect to or update
        Neon. This does not make model calls free; API usage can still occur.

    --input-cost-per-million RATE
        Input-token price used to calculate api_cost when the API response does
        not report cost.

    --output-cost-per-million RATE
        Non-reasoning output-token price used for fallback cost calculation.

    --reasoning-cost-per-million RATE
        Reasoning-token price used for fallback cost calculation. When omitted,
        --output-cost-per-million is used for reasoning tokens too.

    --verbose
        Enable debug-level logging.

Environment variables:
    NEON_DATABASE_URL
        Required unless --dry-run is used.
    MYSTIQUE_API_KEY or OPENAI_API_KEY
        Optional API credential. When neither is set, authorization is omitted
        for localhost endpoints. Non-local endpoints still require a key.
    MYSTIQUE_MODEL
        Overrides the default model when --model is omitted.
    MYSTIQUE_API_BASE
        Overrides the default API URL when --base-url is omitted.

External commands:
    git and astyle
        Required to construct the method-level input.
    joern-parse and joern-export
        Required for Mystique's semantic slicing during patch generation.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
MYSTIQUE_SRC = ROOT / "mystique-opensource.github.io" / "src"
DEFAULT_SHA_FILE = ROOT / "custom-java.json"
DEFAULT_REPO_FILE = ROOT / "custom-java-repos.json"
DEFAULT_FULL_INPUT = ROOT / "cve-java-custom-full.json"
DEFAULT_REPOS_DIR = ROOT / ".mystique-java-repos"
TABLE = "backport_benchmark_results_mystique_java"
DEFAULT_BASE_URL = "http://localhost:8317/v1"
DEFAULT_MODEL = "gpt-5.5"
PLACEHOLDER = "    /* PLACEHOLDER: DO NOT DELETE THIS COMMENT */"
KEY_ID_RE = re.compile(r"-(\d+)$")


SYSTEM_PROMPT = (
    "You're a professional and cautious Java programmer, and you're very good "
    "at patching programs. Now I'm going to give you a patch and a piece of "
    "code to fix, but it's worth noting that the patch you've been given won't "
    "necessarily work directly with this code; you'll need to adapt it. You "
    "only need to adapt and fix the patch part, do not make any other fixes or "
    "improvements. Maintain the original style of the code as much as possible. "
    "Do not delete or add any comments in the code. You may notice that there "
    "are some missing parts in the code I gave you, but it's okay, don't fill "
    "in the missing parts. You just need to output the fixed code!"
)


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    reported_cost: float | None = None

    def add_response(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "completion_tokens_details", None)
        self.reasoning_tokens += int(getattr(details, "reasoning_tokens", 0) or 0)
        reported_total = int(getattr(usage, "total_tokens", 0) or 0)
        self.total_tokens += reported_total or (
            int(getattr(usage, "prompt_tokens", 0) or 0)
            + int(getattr(usage, "completion_tokens", 0) or 0)
        )
        raw = usage.model_dump() if hasattr(usage, "model_dump") else {}
        cost = raw.get("cost") or raw.get("total_cost")
        if cost is not None:
            self.reported_cost = (self.reported_cost or 0.0) + float(cost)

    def cost(self, input_rate: float | None, output_rate: float | None,
             reasoning_rate: float | None) -> float | None:
        if self.reported_cost is not None:
            return self.reported_cost
        if input_rate is None or output_rate is None:
            return None
        reasoning_rate = output_rate if reasoning_rate is None else reasoning_rate
        non_reasoning_output = max(0, self.output_tokens - self.reasoning_tokens)
        return (
            self.input_tokens * input_rate
            + non_reasoning_output * output_rate
            + self.reasoning_tokens * reasoning_rate
        ) / 1_000_000


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


def import_runtime() -> dict[str, Any]:
    try:
        import httpx
        import psycopg2
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit(
            f"Missing dependency {exc.name!r}. Install runner requirements with: "
            "python3 -m pip install -r requirements-mystique-java.txt"
        ) from exc

    sys.path.insert(0, str(MYSTIQUE_SRC))
    try:
        import config
        import difftools
        import llm
        import patchbp
        from codefile import CodeFile
        from common import ErrorCode, Language
        from patch import Patch
        from project import Project
    except ImportError as exc:
        raise SystemExit(
            f"Mystique dependency {exc.name!r} is unavailable. Install "
            "mystique-opensource.github.io/src/requirements.txt first."
        ) from exc

    return locals()


def create_api_client(runtime: dict[str, Any], base_url: str) -> Any:
    api_key = os.getenv("MYSTIQUE_API_KEY") or os.getenv("OPENAI_API_KEY")
    if api_key:
        return runtime["OpenAI"](base_url=base_url, api_key=api_key)

    hostname = (urlparse(base_url).hostname or "").lower()
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit(
            "Set MYSTIQUE_API_KEY or OPENAI_API_KEY for a non-local API endpoint."
        )

    def strip_authentication(request: Any) -> None:
        request.headers.pop("authorization", None)
        request.headers.pop("api-key", None)

    http_client = runtime["httpx"].Client(
        event_hooks={"request": [strip_authentication]}
    )
    return runtime["OpenAI"](
        base_url=base_url,
        api_key="unused-local-placeholder",
        http_client=http_client,
    )


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def require_commands(commands: tuple[str, ...], purpose: str) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if not missing:
        return
    names = ", ".join(missing)
    hint = ""
    if "astyle" in missing:
        hint = " On Debian/Ubuntu, install it with: sudo apt install astyle."
    raise SystemExit(
        f"Missing required executable(s) for {purpose}: {names}.{hint} "
        "Ensure each command is installed and available on PATH."
    )


def configure_runtime_compatibility(runtime: dict[str, Any]) -> None:
    """Adapt supported local tool versions without editing upstream Mystique."""
    Language = runtime["Language"]
    parser_class = runtime["llm"].ASTParser
    try:
        parser = parser_class("class Probe {}", Language.JAVA)
        probe_captures = parser.query("(class_declaration) @class")
    except AttributeError as exc:
        if "query" in str(exc):
            raise SystemExit(
                "Incompatible tree-sitter version: Mystique requires the "
                "Language.query API. Reinstall the corrected dependencies with: "
                "python3 -m pip install --force-reinstall -r "
                "requirements-mystique-java.txt"
            ) from exc
        raise

    if isinstance(probe_captures, dict) and not getattr(
        parser_class, "_mystique_capture_compat", False
    ):
        original_query = parser_class.query
        original_query_from_node = parser_class.query_from_node

        def normalize_captures(captures: Any) -> Any:
            if not isinstance(captures, dict):
                return captures
            return [
                (node, capture_name)
                for capture_name, nodes in captures.items()
                for node in nodes
            ]

        def compatible_query(self: Any, query_str: str) -> Any:
            return normalize_captures(original_query(self, query_str))

        def compatible_query_from_node(
            self: Any, node: Any, query_str: str
        ) -> Any:
            return normalize_captures(
                original_query_from_node(self, node, query_str)
            )

        def compatible_query_oneshot(self: Any, query_str: str) -> Any:
            captures = self.query(query_str)
            return captures[0][0] if captures else None

        parser_class.query = compatible_query
        parser_class.query_from_node = compatible_query_from_node
        parser_class.query_oneshot = compatible_query_oneshot
        parser_class._mystique_capture_compat = True
        logging.info(
            "Normalizing Tree-sitter capture mappings for Mystique compatibility"
        )

    probe = subprocess.run(
        ["astyle", "--squeeze-ws"], input=b"class Probe {}\n",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if probe.returncode != 0:
        if b"squeeze-ws" not in probe.stderr:
            raise SystemExit(
                "AStyle failed its compatibility check: "
                + probe.stderr.decode(errors="replace").strip()
            )

        mystique_format = runtime["patchbp"].format

        def compatible_astyle(code: str) -> str:
            completed = subprocess.run(
                [
                    "astyle", "--style=java", "--keep-one-line-statements",
                    "--max-code-length=200", "--delete-empty-lines",
                ],
                input=code.encode(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "AStyle failed: "
                    + completed.stderr.decode(errors="replace").strip()
                )
            return completed.stdout.decode().strip()

        mystique_format.astyle = compatible_astyle
        logging.info(
            "AStyle does not support --squeeze-ws; using the compatible option set"
        )

    if shutil.which("diff2html") is None:
        runtime["patchbp"].utils.method_diff2html = lambda *_args, **_kwargs: None
        logging.info("diff2html is unavailable; skipping optional HTML diffs")


def ensure_repo(owner_repo: str, repos_dir: Path) -> Path:
    local_path = repos_dir / owner_repo.replace("/", "__")
    if (local_path / ".git").is_dir():
        subprocess.run(
            ["git", "-C", str(local_path), "fetch", "--quiet", "--all"],
            check=True,
        )
        return local_path
    repos_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", f"https://github.com/{owner_repo}.git", str(local_path)],
        check=True,
    )
    return local_path


def build_full_input(args: argparse.Namespace, runtime: dict[str, Any]) -> None:
    """Reproduce the existing exporter contract without changing upstream."""
    require_commands(("git", "astyle"), "building Mystique input")
    configure_runtime_compatibility(runtime)
    sha_data = read_json(args.sha_file)
    repo_map = read_json(args.repo_file)
    Patch = runtime["Patch"]
    Language = runtime["Language"]
    output: dict[str, Any] = {}

    selected_items = [
        item for item in sha_data.items()
        if not args.case or item[0] == args.case
    ]
    if args.count is not None:
        selected_items = selected_items[:args.count]

    for index, (case_key, commits) in enumerate(selected_items, start=1):
        if not isinstance(commits, list) or len(commits) != 2:
            logging.error("[%s] expected [old_sha, new_sha]", case_key)
            continue
        owner_repo = repo_map.get(case_key)
        if not owner_repo:
            logging.error("[%s] repository mapping is missing", case_key)
            continue
        logging.info("[%d/%d] extracting %s", index, len(selected_items), case_key)
        try:
            repo_path = ensure_repo(owner_repo, args.repos_dir)
            origin_patch = Patch(str(repo_path), commits[0], Language.JAVA)
            target_patch = Patch(str(repo_path), commits[1], Language.JAVA)
        except Exception:
            logging.exception("[%s] unable to construct patch pair", case_key)
            continue

        methods: dict[str, Any] = {}
        for signature in origin_patch.changed_methods:
            pre = origin_patch.pre_project.get_method(signature)
            post = origin_patch.post_project.get_method(signature)
            target_pre = target_patch.pre_project.get_method(signature)
            target_post = target_patch.post_project.get_method(signature)
            if None in (pre, post, target_pre, target_post):
                logging.warning("[%s] no matching target method: %s", case_key, signature)
                continue
            method_key = f"{pre.file.path}#{pre.name}"
            methods[method_key] = {
                "origin_before_func_code": pre.code,
                "origin_after_func_code": post.code,
                "target_before_func_code": target_pre.code,
                "target_after_func_code": target_post.code,
                "origin_before_file_code": pre.file.code,
                "origin_after_file_code": post.file.code,
                "target_before_file_code": target_pre.file.code,
                "target_after_file_code": target_post.file.code,
                "origin_before_func_signature": pre.signature,
                "origin_after_func_signature": post.signature,
                "target_before_func_signature": target_pre.signature,
                "target_after_func_signature": target_post.signature,
            }
        if methods:
            output[case_key] = {"patch": methods}
        else:
            logging.error("[%s] no usable method-level patches", case_key)

    if not output:
        raise RuntimeError(
            "No method-level cases were extracted; the existing full-input "
            "file was left unchanged. Review the extraction errors above."
        )
    args.full_input.parent.mkdir(parents=True, exist_ok=True)
    with args.full_input.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
    logging.info("Wrote %d cases to %s", len(output), args.full_input)


def row_id(case_key: str) -> int:
    match = KEY_ID_RE.search(case_key)
    if not match:
        raise ValueError(f"Dataset key does not end in a database id: {case_key!r}")
    return int(match.group(1))


def generate_method(client: Any, model: str, patch: str, target: str,
                    cleaner: Any, language: Any, usage: Usage) -> str:
    content = f"\nPatch:\n{patch}\n\nCode to be fixed:\n{target}\n"
    usage.calls += 1
    response = client.chat.completions.create(
        model=model,
        temperature=0.5,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    )
    usage.add_response(response)
    generated = response.choices[0].message.content
    if not generated:
        raise RuntimeError("the model returned an empty response")
    return cleaner(generated, language)


def recover_method(method_data: dict[str, Any], sliced_code: str,
                   slice_lines: list[int], runtime: dict[str, Any]) -> str:
    CodeFile = runtime["CodeFile"]
    Project = runtime["Project"]
    Language = runtime["Language"]
    code_file = CodeFile("target.java", method_data["target_before_file_code"])
    project = Project("target-recovery", [code_file], Language.JAVA)
    method = project.get_method(method_data["target_before_func_signature"])
    if method is None:
        raise RuntimeError("target method could not be reconstructed for recovery")
    recovered = method.recover_placeholder(sliced_code, set(slice_lines), PLACEHOLDER)
    if recovered is None:
        raise RuntimeError("model output changed the number of Mystique placeholders")
    return recovered


def make_file_patch(path: str, before: str, after: str) -> str:
    if before == after:
        return ""
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
        )
    )


def process_case(case_key: str, case_data: dict[str, Any], client: Any,
                 args: argparse.Namespace, runtime: dict[str, Any],
                 usage: Usage) -> str:
    patchbp = runtime["patchbp"]
    llm = runtime["llm"]
    Language = runtime["Language"]
    ErrorCode = runtime["ErrorCode"]
    files: dict[str, tuple[str, str]] = {}

    for method_key, method_data in case_data["patch"].items():
        path, method_name = method_key.rsplit("#", 1)
        result = patchbp.bp_java(
            case_key, method_data, path, method_name, Language.JAVA,
            overwrite=args.overwrite, slice_level=args.slice_level,
        )
        if result.get("error") != ErrorCode.SUCCESS.value:
            raise RuntimeError(f"Mystique failed for {method_key}: {result.get('error')}")
        sliced = generate_method(
            client, args.model, result["patch"], result["target"],
            llm.clean_llm_output, Language.JAVA, usage,
        )
        recovered = recover_method(
            method_data, sliced, result["target_slice_lines"], runtime,
        )
        original, current = files.get(
            path,
            (method_data["target_before_file_code"], method_data["target_before_file_code"]),
        )
        old_method = method_data["target_before_func_code"]
        if old_method not in current:
            raise RuntimeError(f"target method text was not found in {path}")
        files[path] = (original, current.replace(old_method, recovered.rstrip("\n"), 1))

    patches = [make_file_patch(path, before, after) for path, (before, after) in files.items()]
    return "\n".join(patch for patch in patches if patch)


def method_label(model: str) -> str:
    return f"Mystique ({model})"


def mark_running(cursor: Any, database_id: int, model: str) -> None:
    cursor.execute(
        f"UPDATE {TABLE} SET status = %s, method = %s, updated_at = %s WHERE id = %s",
        ("running", method_label(model), datetime.now(timezone.utc), database_id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"database row {database_id} does not exist")


def save_success(cursor: Any, database_id: int, generated_patch: str,
                 elapsed: float, usage: Usage, cost: float | None,
                 model: str) -> None:
    cursor.execute(f"SELECT new_version_patch FROM {TABLE} WHERE id = %s", (database_id,))
    row = cursor.fetchone()
    expected = row[0] if row else None
    identical = expected is not None and expected.strip() == generated_patch.strip()
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
        (
            method_label(model), generated_patch, elapsed, usage.calls,
            usage.calls, usage.input_tokens, usage.output_tokens,
            usage.reasoning_tokens, usage.total_tokens, cost, identical,
            "completed", datetime.now(timezone.utc), database_id,
        ),
    )


def save_failure(cursor: Any, database_id: int, elapsed: float,
                 usage: Usage, cost: float | None, error: Exception,
                 model: str) -> None:
    message = f"failed: {type(error).__name__}: {error}"[:1000]
    cursor.execute(
        f"""
        UPDATE {TABLE}
        SET method = %s, execution_time_seconds = %s,
            number_of_llm_api_calls = %s, api_calls = %s,
            input_tokens = %s, output_tokens = %s, reasoning_tokens = %s,
            total_tokens = %s, api_cost = %s, status = %s, updated_at = %s
        WHERE id = %s
        """,
        (
            method_label(model), elapsed, usage.calls, usage.calls,
            usage.input_tokens, usage.output_tokens, usage.reasoning_tokens,
            usage.total_tokens, cost, message, datetime.now(timezone.utc), database_id,
        ),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha-file", type=Path, default=DEFAULT_SHA_FILE)
    parser.add_argument("--repo-file", type=Path, default=DEFAULT_REPO_FILE)
    parser.add_argument("--full-input", type=Path, default=DEFAULT_FULL_INPUT)
    parser.add_argument("--repos-dir", type=Path, default=DEFAULT_REPOS_DIR)
    parser.add_argument("--build-input", action="store_true", help="rebuild method-level input")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--case", help="run one exact key from custom-java.json")
    parser.add_argument(
        "--count", "--limit", dest="count", type=int,
        help="process the first N dataset cases",
    )
    parser.add_argument("--model", default=os.getenv("MYSTIQUE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.getenv("MYSTIQUE_API_BASE", DEFAULT_BASE_URL))
    parser.add_argument("--slice-level", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true", help="rebuild Mystique cache")
    parser.add_argument("--dry-run", action="store_true", help="generate but do not update Neon")
    parser.add_argument("--input-cost-per-million", type=float, default=None)
    parser.add_argument("--output-cost-per-million", type=float, default=None)
    parser.add_argument("--reasoning-cost-per-million", type=float, default=None)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv_if_available()
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.count is not None and args.count < 1:
        raise SystemExit("--count must be greater than zero")

    runtime = import_runtime()
    should_build = args.build_input or not args.full_input.exists()
    if not should_build:
        should_build = not read_json(args.full_input)
        if should_build:
            logging.info("%s is empty; rebuilding it", args.full_input)
    if should_build:
        build_full_input(args, runtime)
    if args.build_only:
        return 0

    require_commands(("astyle", "joern-parse", "joern-export"), "patch generation")
    configure_runtime_compatibility(runtime)

    data = read_json(args.full_input)
    selected = [item for item in data.items() if not args.case or item[0] == args.case]
    if args.count is not None:
        selected = selected[:args.count]
    if args.case and not selected:
        raise SystemExit(f"Case {args.case!r} is not present in {args.full_input}")

    client = create_api_client(runtime, args.base_url)
    database_url = os.getenv("NEON_DATABASE_URL")
    if not args.dry_run and not database_url:
        raise SystemExit("Set NEON_DATABASE_URL in the environment or .env")
    connection = None if args.dry_run else runtime["psycopg2"].connect(database_url)

    failures = 0
    try:
        for case_key, case_data in selected:
            database_id = row_id(case_key)
            if connection:
                with connection.cursor() as cursor:
                    mark_running(cursor, database_id, args.model)
                connection.commit()
            started = time.monotonic()
            usage = Usage()
            try:
                generated_patch = process_case(
                    case_key, case_data, client, args, runtime, usage,
                )
                elapsed = time.monotonic() - started
                cost = usage.cost(
                    args.input_cost_per_million,
                    args.output_cost_per_million,
                    args.reasoning_cost_per_million,
                )
                if connection:
                    with connection.cursor() as cursor:
                        save_success(
                            cursor, database_id, generated_patch, elapsed,
                            usage, cost, args.model,
                        )
                    connection.commit()
                logging.info(
                    "[%s] completed: %d calls, %d tokens, %.2fs",
                    case_key, usage.calls, usage.total_tokens, elapsed,
                )
            except Exception as exc:
                failures += 1
                elapsed = time.monotonic() - started
                cost = usage.cost(
                    args.input_cost_per_million,
                    args.output_cost_per_million,
                    args.reasoning_cost_per_million,
                )
                logging.exception("[%s] failed", case_key)
                if connection:
                    with connection.cursor() as cursor:
                        save_failure(
                            cursor, database_id, elapsed, usage, cost, exc,
                            args.model,
                        )
                    connection.commit()
    finally:
        if connection:
            connection.close()
        client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
