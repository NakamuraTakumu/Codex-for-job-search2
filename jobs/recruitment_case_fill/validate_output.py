#!/usr/bin/env python3
"""Validate recruitment_case_fill output with Pydantic models."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE_ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from jobs.common.validator_helpers import (
    fail,
    load_collected_output_file,
)
from jobs.common.output_paths import RECRUITMENT_CASE_OUTPUT_PATH
from schemas.recruitment_case_models import RecruitmentCase


def validate_recruitment_case_output(
    *,
    subcwd,
    expected_company_id,
    expected_recruitment_year,
):
    """Validate the collected file and raise ValueError with a repair diagnostic."""
    _, recruitment_case, error = load_collected_output_file(
        model_class=RecruitmentCase,
        collected_output_path=RECRUITMENT_CASE_OUTPUT_PATH,
        subcwd=subcwd,
    )
    if error:
        raise ValueError(error)

    if recruitment_case.publishing_company_id != expected_company_id:
        raise ValueError(
            "publishing_company_id must preserve input value: "
            f"expected {expected_company_id!r}, "
            f"got {recruitment_case.publishing_company_id!r}"
        )

    if recruitment_case.recruitment_year != expected_recruitment_year:
        raise ValueError(
            "recruitment_year must preserve input value: "
            f"expected {expected_recruitment_year!r}, "
            f"got {recruitment_case.recruitment_year!r}"
        )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return fail("usage: validate_output.py <company_id> <recruitment_year>")

    expected_company_id = argv[1]
    expected_recruitment_year = int(argv[2])

    try:
        validate_recruitment_case_output(
            subcwd=None,
            expected_company_id=expected_company_id,
            expected_recruitment_year=expected_recruitment_year,
        )
    except ValueError as exc:
        return fail(str(exc))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
