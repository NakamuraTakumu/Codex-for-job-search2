#!/usr/bin/env python3
"""Run recruitment_case_fill jobs concurrently from a company-list JSON file."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from jobs.common.batch import run_jobs
from jobs.common.codex_job import DEFAULT_JOB_TIMEOUT_SECONDS, build_session_config
from jobs.common.output_paths import RECRUITMENT_CASE_OUTPUT_PATH
from jobs.recruitment_case_fill import RecruitmentCaseFillJob


DEFAULT_TMP_DIR = WORKSPACE_ROOT / "tmp"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run recruitment_case_fill jobs with bounded concurrency and save "
            "one JSON file per publishing company."
        )
    )
    parser.add_argument(
        "companies_json",
        type=Path,
        help="JSON containing a companies array with company_id and legal_name.",
    )
    parser.add_argument("recruitment_year", type=int, help="Target entry year.")
    parser.add_argument(
        "--limit",
        type=positive_int,
        help="Run only the first N unique company IDs.",
    )
    parser.add_argument(
        "--max-workers",
        type=positive_int,
        default=4,
        help="Maximum concurrently running jobs. Defaults to 4.",
    )
    parser.add_argument(
        "--warmup-jobs",
        type=non_negative_int,
        default=1,
        help="Jobs completed before full concurrency starts. Defaults to 1.",
    )
    parser.add_argument(
        "--job-timeout",
        type=positive_int,
        default=DEFAULT_JOB_TIMEOUT_SECONDS,
        help=f"Maximum elapsed seconds per job before cancellation. Defaults to {DEFAULT_JOB_TIMEOUT_SECONDS}.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory. Defaults to a timestamped directory under tmp/.",
    )
    parser.add_argument("--model", help="Optional Codex model override.")
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Optional reasoning effort override.",
    )
    return parser


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def default_output_dir(recruitment_year: int) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return DEFAULT_TMP_DIR / f"recruitment_cases_{recruitment_year}_{timestamp}"


def load_targets(path: Path, limit: int | None) -> list[dict[str, str]]:
    payload = json.loads(path.resolve().read_text(encoding="utf-8"))
    companies = payload.get("companies")
    if not isinstance(companies, list):
        raise ValueError("companies_json must contain a companies array")

    targets = []
    seen = set()
    for index, item in enumerate(companies):
        if not isinstance(item, dict):
            raise ValueError(f"companies[{index}] must be an object")
        company_id = item.get("company_id")
        legal_name = item.get("legal_name")
        if not isinstance(company_id, str) or not re.fullmatch(r"[0-9]{13}", company_id):
            raise ValueError(f"companies[{index}].company_id must be a 13-digit string")
        if not isinstance(legal_name, str) or not legal_name.strip():
            raise ValueError(f"companies[{index}].legal_name must be a non-empty string")
        if company_id in seen:
            continue
        seen.add(company_id)
        targets.append({"company_id": company_id, "legal_name": legal_name.strip()})
        if limit is not None and len(targets) >= limit:
            break
    return targets


def build_config(args: argparse.Namespace):
    options = {
        "cwd": WORKSPACE_ROOT,
        "timeout": args.job_timeout,
    }
    if args.model:
        options["model"] = args.model
    if args.reasoning_effort:
        options["reasoning_effort"] = args.reasoning_effort
    return build_session_config(**options)


def run_batch(
    *,
    config,
    targets: list[dict[str, str]],
    recruitment_year: int,
    max_workers: int,
    warmup_jobs: int,
    output_dir: Path,
) -> dict:
    jobs = [
        RecruitmentCaseFillJob(
            config=config,
            company_id=target["company_id"],
            legal_name=target["legal_name"],
            recruitment_year=recruitment_year,
        )
        for target in targets
    ]
    summary = run_jobs(
        jobs,
        output_key=RECRUITMENT_CASE_OUTPUT_PATH,
        output_path=lambda job: output_dir / f"{job.company_id}.json",
        failed_output_dir=output_dir,
        max_workers=max_workers,
        warmup_jobs=warmup_jobs,
    )
    return {"recruitment_year": recruitment_year, **summary}


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    targets = load_targets(args.companies_json, args.limit)
    if not targets:
        parser.error("companies_json did not yield any target companies")

    output_dir = (args.output_dir or default_output_dir(args.recruitment_year)).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        parser.error(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = run_batch(
        config=build_config(args),
        targets=targets,
        recruitment_year=args.recruitment_year,
        max_workers=args.max_workers,
        warmup_jobs=args.warmup_jobs,
        output_dir=output_dir,
    )
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(summary_path)
    return 0 if summary["failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
