#!/usr/bin/env python3
"""Validate company_profile_fill output with Pydantic models."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE_ROOT = HERE.parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from jobs.common.output_paths import COMPANY_PROFILE_OUTPUT_PATH
from jobs.common.validator_helpers import load_collected_output_file
from schemas.company_profile_models import CompanyProfile


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def validate_company_profile_output(
    *,
    subcwd,
    expected_company_id,
    expected_legal_name,
):
    """Validate the collected file and raise ValueError with a repair diagnostic."""
    _, company_profile, error = load_collected_output_file(
        model_class=CompanyProfile,
        collected_output_path=COMPANY_PROFILE_OUTPUT_PATH,
        subcwd=subcwd,
    )
    if error:
        raise ValueError(error)

    if company_profile.company_id != expected_company_id:
        raise ValueError(
            "company_id must preserve input value: "
            f"expected {expected_company_id!r}, got {company_profile.company_id!r}"
        )

    if not company_profile.legal_name.strip():
        raise ValueError("legal_name must be a non-empty string")
    if company_profile.legal_name != expected_legal_name:
        raise ValueError(
            "legal_name must preserve input value: "
            f"expected {expected_legal_name!r}, got {company_profile.legal_name!r}"
        )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return _fail("usage: validate_output.py <company_id> <legal_name>")

    expected_company_id = argv[1]
    expected_legal_name = argv[2]

    try:
        validate_company_profile_output(
            subcwd=None,
            expected_company_id=expected_company_id,
            expected_legal_name=expected_legal_name,
        )
    except ValueError as exc:
        return _fail(str(exc))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
