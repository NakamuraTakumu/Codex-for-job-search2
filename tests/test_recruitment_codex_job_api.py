import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

from jobs.common.codex_job import (
    COMMON_MODELS_PATH,
    COMMON_PROMPT_PATH,
    COMMON_REVIEW_PROMPT_PATH,
    ReviewConfig,
    SubagentConfig,
    build_session_config,
    build_review_instructions,
)

from jobs.corporate_number_readonly_probe.client import (
    CorporateNumberReadonlyProbeJob,
)
from jobs.common.output_paths import COMPANY_PROFILE_OUTPUT_PATH, RECRUITMENT_CASE_OUTPUT_PATH
from jobs.company_profile_fill.client import (
    MODEL_PATH as COMPANY_PROFILE_MODEL_PATH,
    PROMPT_PATH as COMPANY_PROFILE_PROMPT_PATH,
    CompanyProfileFillJob,
)
from jobs.recruitment_case_fill.client import (
    MODEL_PATH as RECRUITMENT_CASE_MODEL_PATH,
    PROMPT_PATH,
    QUALITY_CONTRACT_PATH,
    RecruitmentCaseFillJob,
)


class RecruitmentCodexJobApiTests(unittest.TestCase):
    def test_main_session_defaults_to_luna_high_and_preserves_overrides(self):
        default_config = build_session_config(cwd=Path.cwd())
        override_config = build_session_config(
            cwd=Path.cwd(),
            model="gpt-6-luna",
            reasoning_effort="medium",
        )

        self.assertEqual(default_config.model, "gpt-6-luna")
        self.assertEqual(default_config.reasoning_effort, "high")
        self.assertEqual(default_config.timeout, 1800)
        self.assertEqual(override_config.model, "gpt-6-luna")
        self.assertEqual(override_config.reasoning_effort, "medium")

    def test_reviewer_information_has_one_owner_per_section(self):
        context = build_review_instructions(
            RECRUITMENT_CASE_OUTPUT_PATH,
            RECRUITMENT_CASE_MODEL_PATH,
            shared_context=(QUALITY_CONTRACT_PATH,),
        )
        joined = "\n\n".join(context)

        self.assertEqual(len(context), 5)
        self.assertEqual(joined.count("Fixed review contract:"), 1)
        self.assertEqual(joined.count("Shared contract ("), 2)
        self.assertEqual(joined.count("Canonical schema/model ("), 2)
        self.assertEqual(joined.count("共通レビュー契約:"), 1)
        self.assertEqual(joined.count("共通調査契約:"), 1)
        self.assertEqual(joined.count("Recruitment Case 共通品質契約"), 1)
        self.assertNotIn("${SUBCWD}/" + RECRUITMENT_CASE_OUTPUT_PATH, joined)

    def test_common_review_prompt_leaves_output_format_to_runner(self):
        review_prompt = COMMON_REVIEW_PROMPT_PATH.read_text(encoding="utf-8")

        for runner_field in ("passed", "findings", "summary", "decisions", "new_findings"):
            self.assertNotIn(runner_field, review_prompt)

    def test_target_and_previous_year_are_always_researched(self):
        quality_contract = QUALITY_CONTRACT_PATH.read_text(encoding="utf-8")

        self.assertIn("前年度（`recruitment_year - 1`）の両方を調査する", quality_contract)
        self.assertIn("さらに古い年度は調査しない", quality_contract)
        for text in (quality_contract, PROMPT_PATH.read_text(encoding="utf-8")):
            self.assertNotIn("2群以上", text)

    def test_company_profile_fill_uses_current_runner_api(self):
        config = build_session_config(
            cwd=Path.cwd(),
            context=["data/companies/1010001128065.json"],
        )
        job = CompanyProfileFillJob(
            config=config,
            company_id="1010001128065",
            legal_name="日本アイ・ビー・エム株式会社",
            extra_input="公式情報を優先する。",
        )

        self.assertEqual(job.status, "pending")
        self.assertIsNone(job.config.codex_bin)
        self.assertEqual(
            job.config.context,
            (
                str(COMMON_PROMPT_PATH),
                str(COMPANY_PROFILE_PROMPT_PATH),
                str(COMMON_MODELS_PATH),
                str(Path.cwd() / "schemas" / "company_profile_models.py"),
                "data/companies/1010001128065.json",
            ),
        )
        self.assertIn("Target company_id:\n1010001128065", job.input)
        self.assertIn("Target legal_name:\n日本アイ・ビー・エム株式会社", job.input)
        self.assertIn(
            "Collected output path:\n${SUBCWD}/" + COMPANY_PROFILE_OUTPUT_PATH,
            job.input,
        )
        self.assertEqual(
            job.input.count("${SUBCWD}/" + COMPANY_PROFILE_OUTPUT_PATH),
            1,
        )
        self.assertIn("Additional instructions:\n公式情報を優先する。", job.input)
        self.assertNotIn(
            COMMON_PROMPT_PATH.read_text(encoding="utf-8").strip(),
            job.input,
        )
        self.assertNotIn(
            COMPANY_PROFILE_PROMPT_PATH.read_text(encoding="utf-8").strip(),
            job.input,
        )
        review = job._definition["review"]
        self.assertIsInstance(review, ReviewConfig)
        review_context = review.context
        self.assertEqual(len(review_context), 4)
        self.assertEqual(
            review_context[1].removeprefix(
                "Shared contract (jobs/common/research_prompt.md):\n"
            ),
            COMMON_PROMPT_PATH.read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(
            review_context[2].removeprefix(
                "Canonical schema/model (schemas/common_models.py):\n"
            ),
            COMMON_MODELS_PATH.read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(
            review_context[3].removeprefix(
                "Canonical schema/model (schemas/company_profile_models.py):\n"
            ),
            COMPANY_PROFILE_MODEL_PATH.read_text(encoding="utf-8").strip(),
        )
        self.assertNotIn(
            RECRUITMENT_CASE_MODEL_PATH.read_text(encoding="utf-8").strip(),
            review_context,
        )
        self.assertFalse(review.include_main_context)
        self.assertEqual(review.session.model, "gpt-6-luna")
        self.assertEqual(review.session.reasoning_effort, "high")
        self._assert_research_subagents(job.subagents, job.config)
        self._assert_research_subagents(review.subagents, review.session)
        self.assertTrue(review.session.allow_shell_commands)
        self.assertEqual(review.session.workspace.read_paths, ("${SUBCWD}/.",))
        self.assertEqual(review.session.workspace.write_paths, ())
        self.assertTrue(review.reuse_session)
        self.assertTrue(review.enable_peer_exchange)
        self.assertEqual(
            review.artifact_paths,
            ("${SUBCWD}/" + COMPANY_PROFILE_OUTPUT_PATH,),
        )
        self.assertIsNone(review.max_repairs)
        self.assertEqual(tuple(job.output_validators), (job.validate_output,))
        self.assertEqual(job.config.workspace.write_paths, ("${SUBCWD}/.",))
        self.assertTrue(job._definition["collect_files"])

    def test_recruitment_case_fill_uses_current_runner_api(self):
        config = build_session_config(
            cwd=Path.cwd(),
            context=["data/companies/1010001128065.json"],
        )
        job = RecruitmentCaseFillJob(
            config=config,
            company_id="1010001128065",
            legal_name="日本アイ・ビー・エム株式会社",
            recruitment_year=2028,
        )

        self.assertEqual(job.status, "pending")
        self.assertIsNone(job.config.codex_bin)
        self._assert_research_subagents(job.subagents, job.config)
        self.assertEqual(
            job.config.context,
            (
                str(COMMON_PROMPT_PATH),
                str(QUALITY_CONTRACT_PATH),
                str(PROMPT_PATH),
                str(COMMON_MODELS_PATH),
                str(Path.cwd() / "schemas" / "recruitment_case_models.py"),
                "data/companies/1010001128065.json",
            ),
        )
        self.assertIn("Target company_id:\n1010001128065", job.input)
        self.assertIn("Target legal_name:\n日本アイ・ビー・エム株式会社", job.input)
        self.assertIn("Target recruitment_year:\n2028", job.input)
        self.assertIn(
            "Collected output path:\n${SUBCWD}/" + RECRUITMENT_CASE_OUTPUT_PATH,
            job.input,
        )
        self.assertEqual(
            job.input.count("${SUBCWD}/" + RECRUITMENT_CASE_OUTPUT_PATH),
            1,
        )
        self.assertNotIn(
            COMMON_PROMPT_PATH.read_text(encoding="utf-8").strip(),
            job.input,
        )
        self.assertNotIn(
            PROMPT_PATH.read_text(encoding="utf-8").strip(),
            job.input,
        )
        review = job._definition["review"]
        self.assertIsInstance(review, ReviewConfig)
        review_context = review.context
        self.assertEqual(len(review_context), 5)
        self.assertEqual(
            review_context[1].removeprefix(
                "Shared contract (jobs/common/research_prompt.md):\n"
            ),
            COMMON_PROMPT_PATH.read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(
            review_context[2].removeprefix(
                "Shared contract (jobs/recruitment_case_fill/quality_contract.md):\n"
            ),
            QUALITY_CONTRACT_PATH.read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(
            review_context[3].removeprefix(
                "Canonical schema/model (schemas/common_models.py):\n"
            ),
            COMMON_MODELS_PATH.read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(
            review_context[4].removeprefix(
                "Canonical schema/model (schemas/recruitment_case_models.py):\n"
            ),
            RECRUITMENT_CASE_MODEL_PATH.read_text(encoding="utf-8").strip(),
        )
        self.assertNotIn(
            COMPANY_PROFILE_MODEL_PATH.read_text(encoding="utf-8").strip(),
            review_context,
        )
        self.assertFalse(review.include_main_context)
        self.assertEqual(review.session.model, "gpt-6-luna")
        self.assertEqual(review.session.reasoning_effort, "high")
        self._assert_research_subagents(review.subagents, review.session)
        self.assertTrue(review.session.allow_shell_commands)
        self.assertEqual(review.session.workspace.read_paths, ("${SUBCWD}/.",))
        self.assertEqual(review.session.workspace.write_paths, ())
        self.assertTrue(review.reuse_session)
        self.assertTrue(review.enable_peer_exchange)
        self.assertEqual(
            review.artifact_paths,
            ("${SUBCWD}/" + RECRUITMENT_CASE_OUTPUT_PATH,),
        )
        self.assertIsNone(review.max_repairs)
        self.assertEqual(tuple(job.output_validators), (job.validate_output,))
        self.assertEqual(job.config.workspace.write_paths, ("${SUBCWD}/.",))
        self.assertTrue(job._definition["collect_files"])

    def test_recruitment_quality_contract_is_injected_for_main_and_reviewer(self):
        job = RecruitmentCaseFillJob(
            config=build_session_config(cwd=Path.cwd()),
            company_id="1010001128065",
            legal_name="日本アイ・ビー・エム株式会社",
            recruitment_year=2028,
        )

        self.assertIn(str(QUALITY_CONTRACT_PATH), job.config.context)
        quality_contract = QUALITY_CONTRACT_PATH.read_text(encoding="utf-8").strip()
        self.assertIn("application_slot_inventory", quality_contract)
        self.assertIn("完全一致", quality_contract)
        review_context = "\n\n".join(job._definition["review"].context)
        self.assertEqual(review_context.count(quality_contract), 1)

    def test_slot_boundary_is_not_restated_outside_schema(self):
        quality_contract = QUALITY_CONTRACT_PATH.read_text(encoding="utf-8")

        self.assertIn("`application_slots`の分け方", quality_contract)
        self.assertNotIn("希望職種", quality_contract)
        self.assertNotIn("希望職種", PROMPT_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("希望職種", COMMON_REVIEW_PROMPT_PATH.read_text(encoding="utf-8"))

    def test_recruitment_case_fill_configures_main_and_reviewer_subagents(self):
        job = RecruitmentCaseFillJob(
            config=build_session_config(cwd=Path.cwd()),
            company_id="1010001128065",
            legal_name="日本アイ・ビー・エム株式会社",
            recruitment_year=2028,
        )

        self._assert_research_subagents(job.subagents, job.config)
        self._assert_research_subagents(
            job._definition["review"].subagents,
            job._definition["review"].session,
        )

    def test_collected_json_job_uses_neutral_repair_prompt(self):
        job = RecruitmentCaseFillJob(
            config=build_session_config(cwd=Path.cwd()),
            company_id="1010001128065",
            legal_name="日本アイ・ビー・エム株式会社",
            recruitment_year=2028,
        )

        repair_prompt = Path(job._definition["repair_prompt_path"]).read_text(encoding="utf-8")
        for placeholder in ("{validation_diagnostic}", "{review_findings}"):
            self.assertIn(placeholder, repair_prompt)

    def test_prompts_leave_subagent_usage_to_model(self):
        for path in (
            COMMON_PROMPT_PATH,
            COMMON_REVIEW_PROMPT_PATH,
            PROMPT_PATH,
        ):
            prompt = path.read_text(encoding="utf-8")
            self.assertNotIn("subagent", prompt)
            self.assertNotIn("spawn_agent", prompt)
            self.assertNotIn("トークン削減", prompt)
            self.assertNotIn("ask_peer", prompt)

    def test_missing_is_free_text_without_outcome_vocabulary(self):
        common_research_prompt = COMMON_PROMPT_PATH.read_text(encoding="utf-8")
        common_review_prompt = COMMON_REVIEW_PROMPT_PATH.read_text(encoding="utf-8")

        self.assertIn("自由記述", common_research_prompt)
        self.assertIn("調査が十分だったかは判定しない", common_review_prompt)
        for prompt in (common_research_prompt, common_review_prompt):
            for vocabulary in ("source_checks", "outcome", "not_found", "insufficient_specificity"):
                self.assertNotIn(vocabulary, prompt)

    def _assert_research_subagents(self, subagents, parent):
        self.assertIsInstance(subagents, SubagentConfig)
        self.assertEqual(
            subagents.allowed_models,
            {"gpt-6-luna": ("high",)},
        )
        self.assertEqual(subagents.max_concurrent, 3)
        self.assertFalse(hasattr(subagents, "max_children"))
        self.assertFalse(hasattr(subagents, "max_turns"))
        self.assertFalse(hasattr(subagents, "max_turns_per_child"))
        self.assertEqual(subagents.session.model, "gpt-6-luna")
        self.assertEqual(subagents.session.reasoning_effort, "high")
        self.assertEqual(subagents.session.context, ())
        self.assertEqual(subagents.session.workspace.cwd, parent.workspace.cwd)
        self.assertEqual(subagents.session.workspace.write_paths, ())

    def test_method_validators_read_the_job_subcwd(self):
        company_job = CompanyProfileFillJob(
            config=build_session_config(cwd=Path.cwd()),
            company_id="1010001128065",
            legal_name="日本アイ・ビー・エム株式会社",
        )
        recruitment_job = RecruitmentCaseFillJob(
            config=build_session_config(cwd=Path.cwd()),
            company_id="1010001128065",
            legal_name="日本アイ・ビー・エム株式会社",
            recruitment_year=2028,
        )

        with tempfile.TemporaryDirectory() as directory:
            subcwd = Path(directory)
            for job in (company_job, recruitment_job):
                with (
                    patch.object(
                        type(job),
                        "subcwd_path",
                        new_callable=PropertyMock,
                        return_value=subcwd,
                    ),
                    self.assertRaisesRegex(
                        ValueError,
                        r"collected output file is missing.*\$\{SUBCWD\}",
                    ),
                ):
                    job.validate_output(SimpleNamespace(content="completed"))

    def test_clients_preserve_explicit_codex_bin_override(self):
        codex_bin = "/opt/codex/bin/codex"

        config = build_session_config(
            cwd=Path.cwd(),
            codex_bin=codex_bin,
        )
        company_job = CompanyProfileFillJob(
            config=config,
            company_id="1010001128065",
            legal_name="日本アイ・ビー・エム株式会社",
        )
        probe_job = CorporateNumberReadonlyProbeJob(config=config)

        self.assertEqual(company_job.config.codex_bin, codex_bin)
        self.assertEqual(probe_job.config.codex_bin, codex_bin)


if __name__ == "__main__":
    unittest.main()
