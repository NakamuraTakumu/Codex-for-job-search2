"""Codex wrapper job for filling one company_profile document."""

from pathlib import Path


HERE = Path(__file__).resolve().parent
WORKSPACE_ROOT = HERE.parents[1]

from jobs.common.codex_job import CollectedJsonCodexJob
from jobs.common.output_paths import COMPANY_PROFILE_OUTPUT_PATH
from jobs.company_profile_fill.validate_output import (
    validate_company_profile_output,
)
COMMON_PROMPT_PATH = WORKSPACE_ROOT / "jobs" / "common" / "research_prompt.md"
COMMON_REVIEW_PROMPT_PATH = WORKSPACE_ROOT / "jobs" / "common" / "review_prompt.md"
PROMPT_PATH = HERE / "prompt.md"
REVISE_PROMPT_PATH = WORKSPACE_ROOT / "jobs" / "common" / "revise_prompt.md"
MODEL_PATH = WORKSPACE_ROOT / "schemas" / "company_profile_models.py"


class CompanyProfileFillJob(CollectedJsonCodexJob):
    """Fill one company_profile JSON document using web research."""

    def __init__(
        self,
        *,
        config,
        company_id,
        legal_name,
        extra_input=None,
        read_paths=None,
        write_paths=None,
        review=None,
        existing_output_path=None,
    ):
        self.company_id = str(company_id)
        self.legal_name = str(legal_name)
        prompt = _build_input(
            company_id=self.company_id,
            legal_name=self.legal_name,
            collected_output_path=COMPANY_PROFILE_OUTPUT_PATH,
            extra_input=extra_input,
            existing_output_path=existing_output_path,
        )
        if existing_output_path is not None:
            read_paths = (*(read_paths or ()), str(existing_output_path))
        super().__init__(
            config=config,
            fixed_context=(
                COMMON_PROMPT_PATH,
                PROMPT_PATH,
                WORKSPACE_ROOT / "schemas" / "common_models.py",
                MODEL_PATH,
                *((REVISE_PROMPT_PATH,) if existing_output_path is not None else ()),
            ),
            prompt=prompt,
            collected_output_path=COMPANY_PROFILE_OUTPUT_PATH,
            domain_schema_path=MODEL_PATH,
            read_paths=read_paths,
            write_paths=write_paths,
            review=review,
        )

    def validate_output(self, response):
        del response
        validate_company_profile_output(
            subcwd=self.subcwd_path,
            expected_company_id=self.company_id,
            expected_legal_name=self.legal_name,
        )


def _build_input(
    *, company_id, legal_name, collected_output_path, extra_input, existing_output_path
):
    sections = [
        "Target company_id:\n" + company_id,
        "Target legal_name:\n" + legal_name,
        "Collected output path:\n" + f"${{SUBCWD}}/{collected_output_path}",
    ]
    if existing_output_path is not None:
        sections.append("Existing result path:\n" + str(existing_output_path))
    if extra_input:
        sections.append("Additional instructions:\n" + extra_input)
    return "\n\n".join(sections)
