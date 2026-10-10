"""Codex wrapper job for filling one recruitment_case document."""

from pathlib import Path


HERE = Path(__file__).resolve().parent

from jobs.common.codex_job import CollectedJsonCodexJob, build_recruitment_input
from jobs.common.output_paths import RECRUITMENT_CASE_OUTPUT_PATH
from jobs.recruitment_case_fill.validate_output import (
    validate_recruitment_case_output,
)
PROMPT_PATH = HERE / "prompt.md"
REVISE_PROMPT_PATH = HERE.parents[1] / "jobs" / "common" / "revise_prompt.md"
QUALITY_CONTRACT_PATH = HERE / "quality_contract.md"
MODEL_PATH = HERE.parents[1] / "schemas" / "recruitment_case_models.py"


class RecruitmentCaseFillJob(CollectedJsonCodexJob):
    """Fill one recruitment_case JSON document using web research."""

    def __init__(
        self,
        *,
        config,
        company_id,
        legal_name,
        recruitment_year,
        extra_input=None,
        read_paths=None,
        write_paths=None,
        review=None,
        existing_output_path=None,
    ):
        self.company_id = str(company_id)
        self.legal_name = str(legal_name)
        self.recruitment_year = int(recruitment_year)
        prompt = _build_input(
            company_id=self.company_id,
            legal_name=self.legal_name,
            recruitment_year=self.recruitment_year,
            extra_input=extra_input,
            existing_output_path=existing_output_path,
        )
        if existing_output_path is not None:
            read_paths = (*(read_paths or ()), str(existing_output_path))
        super().__init__(
            config=config,
            fixed_context=(
                HERE.parents[1] / "jobs" / "common" / "research_prompt.md",
                QUALITY_CONTRACT_PATH,
                PROMPT_PATH,
                HERE.parents[1] / "schemas" / "common_models.py",
                MODEL_PATH,
                *((REVISE_PROMPT_PATH,) if existing_output_path is not None else ()),
            ),
            prompt=prompt,
            collected_output_path=RECRUITMENT_CASE_OUTPUT_PATH,
            review_shared_context=(QUALITY_CONTRACT_PATH,),
            domain_schema_path=MODEL_PATH,
            read_paths=read_paths,
            write_paths=write_paths,
            review=review,
        )

    def validate_output(self, response):
        del response
        validate_recruitment_case_output(
            subcwd=self.subcwd_path,
            expected_company_id=self.company_id,
            expected_recruitment_year=self.recruitment_year,
        )


def _build_input(
    *, company_id, legal_name, recruitment_year, extra_input, existing_output_path
):
    return build_recruitment_input(
        company_id=company_id,
        legal_name=legal_name,
        recruitment_year=recruitment_year,
        collected_output_path=RECRUITMENT_CASE_OUTPUT_PATH,
        extra_sections=(
            None
            if existing_output_path is None
            else ["Existing result path:\n" + str(existing_output_path)]
        ),
        extra_input=extra_input,
    )
