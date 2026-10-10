#!/usr/bin/env python3
"""Survey listed companies: recruitment cases first, then referenced company profiles.

Phase 1 fills ``data/recruitment_cases/<company_id>_<year>.json`` for each listed
publishing company. Phase 2 collects ``publishing_company_id`` and
``assignment_companies.company_ids`` from every saved recruitment case of the
year and fills ``data/companies/<company_id>.json`` for each of them. Existing
outputs are skipped, so rerunning the same command continues after failures.

With ``--revise``, existing outputs are not skipped: each is re-surveyed starting
from the existing file (adding missing items and correcting errors) and is
overwritten only on success. Phase 2 then covers the companies referenced by the
listed companies' cases only. Missing outputs are still filled fresh.
``--only-invalid`` (requires ``--revise``) narrows the run to existing outputs
that fail current-schema validation (e.g. old-format files whose conversion has
not succeeded yet); missing or valid outputs are not touched.
``--instructions TEXT`` passes free-text guidance to every job.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from jobs.common.batch import run_jobs
from jobs.common.codex_job import DEFAULT_JOB_TIMEOUT_SECONDS, build_session_config
from jobs.common.output_paths import (
    COMPANY_PROFILE_OUTPUT_PATH,
    RECRUITMENT_CASE_OUTPUT_PATH,
)
from jobs.company_profile_fill import CompanyProfileFillJob
from jobs.recruitment_case_fill import RecruitmentCaseFillJob
from schemas.company_profile_models import CompanyProfile
from schemas.recruitment_case_models import RecruitmentCase
from scripts.corporate_number_resolver_lib import DEFAULT_CACHE_DIR, connect, default_db_path
from scripts.fill_recruitment_cases import load_targets, non_negative_int, positive_int


COMPANY_DIR = WORKSPACE_ROOT / "data" / "companies"
RECRUITMENT_CASE_DIR = WORKSPACE_ROOT / "data" / "recruitment_cases"
DEFAULT_TMP_DIR = WORKSPACE_ROOT / "tmp"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "companies_json",
        type=Path,
        help="JSON containing a companies array with company_id and legal_name.",
    )
    parser.add_argument("recruitment_year", type=int, help="Target entry year.")
    parser.add_argument(
        "--limit",
        type=positive_int,
        help="Survey only the first N unique listed company IDs.",
    )
    parser.add_argument("--max-workers", type=positive_int, default=4)
    parser.add_argument("--warmup-jobs", type=non_negative_int, default=1)
    parser.add_argument(
        "--job-timeout",
        type=positive_int,
        default=DEFAULT_JOB_TIMEOUT_SECONDS,
    )
    parser.add_argument("--model", help="Optional Codex model override.")
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Optional reasoning effort override.",
    )
    parser.add_argument(
        "--revise",
        action="store_true",
        help="Re-survey existing outputs starting from them instead of skipping.",
    )
    parser.add_argument(
        "--only-invalid",
        action="store_true",
        help=(
            "With --revise, re-survey only companies whose existing recruitment "
            "case or company profile fails current-schema validation."
        ),
    )
    parser.add_argument(
        "--instructions",
        help="Free text passed to every job as additional instructions.",
    )
    parser.add_argument(
        "--phase",
        choices=["all", "cases", "profiles"],
        default="all",
        help="Run only the recruitment case phase or the company profile phase.",
    )
    parser.add_argument(
        "--company-ids",
        nargs="+",
        metavar="ID",
        help=(
            "Restrict the phase(s) to these 13-digit company IDs. Cases: IDs must "
            "be in companies_json. Profiles (--phase profiles): IDs are surveyed "
            "directly and must have data/companies/<id>.json."
        ),
    )
    return parser


def select_targets(targets: list[dict], company_ids: list[str] | None) -> list[dict]:
    """Return the listed targets named by ``company_ids`` (all when None)."""
    if company_ids is None:
        return targets
    listed = {target["company_id"] for target in targets}
    unknown = [company_id for company_id in company_ids if company_id not in listed]
    if unknown:
        raise ValueError(f"--company-ids not in companies_json: {', '.join(unknown)}")
    wanted = set(company_ids)
    return [target for target in targets if target["company_id"] in wanted]


def validate_profile_ids(company_ids: list[str]) -> list[str]:
    """Return unique ``company_ids`` after checking each has a saved profile."""
    unique = list(dict.fromkeys(company_ids))
    for company_id in unique:
        if not re.fullmatch(r"[0-9]{13}", company_id):
            raise ValueError(f"company id must be 13 digits: {company_id}")
        if not company_profile_path(company_id).exists():
            raise ValueError(f"no data/companies/{company_id}.json: {company_id}")
    return unique


def recruitment_case_path(company_id: str, recruitment_year: int) -> Path:
    return RECRUITMENT_CASE_DIR / f"{company_id}_{recruitment_year}.json"


def company_profile_path(company_id: str) -> Path:
    return COMPANY_DIR / f"{company_id}.json"


def referenced_company_ids(
    recruitment_year: int, publishing_company_ids=None
) -> list[str]:
    """Return companies referenced by the year's saved cases.

    ``publishing_company_ids`` restricts the cases to those publishers.
    """
    company_ids = set()
    for path in RECRUITMENT_CASE_DIR.glob(f"*_{recruitment_year}.json"):
        if publishing_company_ids is not None and path.name not in {
            f"{company_id}_{recruitment_year}.json"
            for company_id in publishing_company_ids
        }:
            continue
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
            ids = {case["publishing_company_id"]}
            for slot in case["application_slots"]:
                ids.update(slot["assignment_companies"]["company_ids"])
        except (ValueError, KeyError, TypeError, OSError):
            continue  # unreadable or old-format file: nothing to reference
        company_ids.update(ids)
    return sorted(company_ids)


def registered_legal_names(company_ids: list[str]) -> dict[str, str]:
    """Return NFKC-normalized registered names for IDs found in the local DB."""
    conn = connect(default_db_path(DEFAULT_CACHE_DIR), readonly=True)
    try:
        names = {}
        for company_id in company_ids:
            row = conn.execute(
                "SELECT legal_name FROM corporate_entities WHERE corporate_number = ?",
                (company_id,),
            ).fetchone()
            if row is not None:
                names[company_id] = unicodedata.normalize("NFKC", row["legal_name"])
        return names
    finally:
        conn.close()


def existing_or_none(path: Path, revise: bool):
    """Return ``path`` when it should seed a revise-mode job, else None."""
    return path if revise and path.exists() else None


def is_invalid_output(path: Path, model_class) -> bool:
    """True when ``path`` exists but cannot be loaded as ``model_class``."""
    if not path.exists():
        return False
    try:
        model_class.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return True
    return False


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.only_invalid and not args.revise:
        parser.error("--only-invalid requires --revise")
    targets = load_targets(args.companies_json, args.limit)
    if not targets:
        raise SystemExit("companies_json did not yield any target companies")
    profile_only = args.phase == "profiles" and args.company_ids is not None
    try:
        if profile_only:
            direct_profile_ids = validate_profile_ids(args.company_ids)
        else:
            targets = select_targets(targets, args.company_ids)
    except ValueError as error:
        parser.error(str(error))

    listed_targets = targets
    if args.only_invalid:
        targets = [
            target
            for target in targets
            if is_invalid_output(
                recruitment_case_path(target["company_id"], args.recruitment_year),
                RecruitmentCase,
            )
        ]
        print(
            f"--only-invalid: {len(targets)} recruitment case(s) selected",
            file=sys.stderr,
        )

    options = {"cwd": WORKSPACE_ROOT, "timeout": args.job_timeout}
    if args.model:
        options["model"] = args.model
    if args.reasoning_effort:
        options["reasoning_effort"] = args.reasoning_effort
    config = build_session_config(**options)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = DEFAULT_TMP_DIR / f"survey_{args.recruitment_year}_{timestamp}"
    batch_options = {
        "max_workers": args.max_workers,
        "warmup_jobs": args.warmup_jobs,
        "failed_output_dir": run_dir,
    }

    run_cases = args.phase in ("all", "cases")
    run_profiles = args.phase in ("all", "profiles")
    empty_summary = {"failure_count": 0, "success_count": 0}
    recruitment_summary = run_jobs(
        [
            RecruitmentCaseFillJob(
                config=config,
                company_id=target["company_id"],
                legal_name=target["legal_name"],
                recruitment_year=args.recruitment_year,
                extra_input=args.instructions,
                existing_output_path=existing_or_none(
                    recruitment_case_path(
                        target["company_id"], args.recruitment_year
                    ),
                    args.revise,
                ),
            )
            for target in targets
            if run_cases
            and (
                args.revise
                or not recruitment_case_path(
                    target["company_id"], args.recruitment_year
                ).exists()
            )
        ],
        output_key=RECRUITMENT_CASE_OUTPUT_PATH,
        output_path=lambda job: recruitment_case_path(
            job.company_id, job.recruitment_year
        ),
        **batch_options,
    )

    if not run_profiles:
        company_ids = []
    elif profile_only:
        company_ids = direct_profile_ids
    else:
        company_ids = [
            company_id
            for company_id in referenced_company_ids(
                args.recruitment_year,
                [target["company_id"] for target in listed_targets]
                if args.revise
                else None,
            )
            if args.revise or not company_profile_path(company_id).exists()
        ]
    if args.only_invalid:
        company_ids = [
            company_id
            for company_id in company_ids
            if is_invalid_output(company_profile_path(company_id), CompanyProfile)
        ]
        print(
            f"--only-invalid: {len(company_ids)} company profile(s) selected",
            file=sys.stderr,
        )
    legal_names = registered_legal_names(company_ids)
    profile_summary = run_jobs(
        [
            CompanyProfileFillJob(
                config=config,
                company_id=company_id,
                legal_name=legal_names[company_id],
                extra_input=args.instructions,
                existing_output_path=existing_or_none(
                    company_profile_path(company_id), args.revise
                ),
            )
            for company_id in company_ids
            if company_id in legal_names
        ],
        output_key=COMPANY_PROFILE_OUTPUT_PATH,
        output_path=lambda job: company_profile_path(job.company_id),
        **batch_options,
    )

    summary = {
        "recruitment_year": args.recruitment_year,
        "recruitment_case": recruitment_summary,
        "company_profile": profile_summary,
        "unregistered_company_ids": [
            company_id for company_id in company_ids if company_id not in legal_names
        ],
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(summary_path)
    failed = (
        recruitment_summary["failure_count"]
        + profile_summary["failure_count"]
        + len(summary["unregistered_company_ids"])
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
