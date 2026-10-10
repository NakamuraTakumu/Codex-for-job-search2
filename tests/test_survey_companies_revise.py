import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobs.common.codex_job import build_session_config
from jobs.company_profile_fill import CompanyProfileFillJob
from jobs.recruitment_case_fill import RecruitmentCaseFillJob
from scripts import survey_companies

REVISE_PROMPT = str(Path(survey_companies.WORKSPACE_ROOT) / "jobs/common/revise_prompt.md")


def summary():
    return {"failure_count": 0, "success_count": 0}


class ReviseJobTests(unittest.TestCase):
    def setUp(self):
        self.config = build_session_config(cwd=Path.cwd())

    def test_recruitment_job_revise_mode(self):
        job = RecruitmentCaseFillJob(
            config=self.config,
            company_id="1180301018771",
            legal_name="A",
            recruitment_year=2028,
            existing_output_path="/x/old.json",
        )
        self.assertIn(REVISE_PROMPT, job.config.context)
        self.assertIn("/x/old.json", job.config.workspace.read_paths)
        self.assertIn("Existing result path:\n/x/old.json", job._definition["input"])

    def test_fresh_jobs_have_no_revise_prompt(self):
        for job in (
            RecruitmentCaseFillJob(
                config=self.config, company_id="1", legal_name="A", recruitment_year=2028
            ),
            CompanyProfileFillJob(config=self.config, company_id="1", legal_name="A"),
        ):
            self.assertNotIn(REVISE_PROMPT, job.config.context)
            self.assertNotIn("Existing result", job._definition["input"])

    def test_profile_job_revise_mode(self):
        job = CompanyProfileFillJob(
            config=self.config,
            company_id="1",
            legal_name="A",
            extra_input="focus",
            existing_output_path="/x/p.json",
        )
        self.assertIn(REVISE_PROMPT, job.config.context)
        self.assertIn("/x/p.json", job.config.workspace.read_paths)
        self.assertIn("Existing result path:\n/x/p.json", job._definition["input"])
        self.assertIn("Additional instructions:\nfocus", job._definition["input"])


class SurveyReviseTests(unittest.TestCase):
    def run_main(self, extra_args, existing):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            case_dir = tmp / "cases"
            company_dir = tmp / "companies"
            case_dir.mkdir()
            company_dir.mkdir()
            listed = tmp / "companies.json"
            listed.write_text(
                json.dumps(
                    {"companies": [{"company_id": "1000000000001", "legal_name": "A"}]}
                ),
                encoding="utf-8",
            )
            (case_dir / "1000000000001_2028.json").write_text(
                json.dumps(
                    {
                        "publishing_company_id": "1000000000001",
                        "application_slots": [
                            {"assignment_companies": {"company_ids": ["2000000000002"]}}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            # An unrelated case that revise mode must not reach.
            (case_dir / "9999999999999_2028.json").write_text(
                json.dumps(
                    {"publishing_company_id": "9999999999999", "application_slots": []}
                ),
                encoding="utf-8",
            )
            for company_id in existing:
                (company_dir / f"{company_id}.json").write_text("{}", encoding="utf-8")
            calls = []

            def fake_run_jobs(jobs, **kwargs):
                calls.append(list(jobs))
                return summary()

            with (
                patch.object(survey_companies, "RECRUITMENT_CASE_DIR", case_dir),
                patch.object(survey_companies, "COMPANY_DIR", company_dir),
                patch.object(survey_companies, "DEFAULT_TMP_DIR", tmp / "run"),
                patch.object(survey_companies, "run_jobs", fake_run_jobs),
                patch.object(
                    survey_companies,
                    "registered_legal_names",
                    lambda ids: {i: f"N{i}" for i in ids},
                ),
            ):
                survey_companies.main([str(listed), "2028", *extra_args])
            return calls, case_dir, company_dir

    def test_default_skips_existing_outputs(self):
        calls, _, _ = self.run_main([], existing=["1000000000001", "2000000000002", "9999999999999"])
        # Case exists for company 1, so phase 1 is empty; profiles 1,2,9 exist.
        self.assertEqual(calls, [[], []])

    def test_revise_includes_existing_targets(self):
        calls, case_dir, company_dir = self.run_main(
            ["--revise", "--instructions", "focus"], existing=["2000000000002"]
        )
        case_jobs, profile_jobs = calls
        self.assertEqual([j.company_id for j in case_jobs], ["1000000000001"])
        self.assertEqual(
            [str(p) for p in case_jobs[0].config.workspace.read_paths if "1000000000001_2028" in str(p)],
            [str(case_dir / "1000000000001_2028.json")],
        )
        self.assertIn(REVISE_PROMPT, case_jobs[0].config.context)
        self.assertIn("focus", case_jobs[0]._definition["input"])
        # Companies 1 and 2 only (not 9); 2 exists -> revise, 1 missing -> fresh.
        by_id = {j.company_id: j for j in profile_jobs}
        self.assertEqual(sorted(by_id), ["1000000000001", "2000000000002"])
        self.assertIn(REVISE_PROMPT, by_id["2000000000002"].config.context)
        self.assertNotIn(REVISE_PROMPT, by_id["1000000000001"].config.context)

    def test_phase_profiles_takes_ids_directly(self):
        # 9999999999999 is referenced by no listed case but is targeted directly.
        calls, _, _ = self.run_main(
            ["--revise", "--phase", "profiles", "--company-ids", "9999999999999"],
            existing=["9999999999999"],
        )
        case_jobs, profile_jobs = calls
        self.assertEqual(case_jobs, [])
        self.assertEqual([j.company_id for j in profile_jobs], ["9999999999999"])

    def test_phase_cases_only_selected_ids(self):
        calls, _, _ = self.run_main(
            ["--revise", "--phase", "cases", "--company-ids", "1000000000001"],
            existing=["1000000000001", "2000000000002"],
        )
        case_jobs, profile_jobs = calls
        self.assertEqual([j.company_id for j in case_jobs], ["1000000000001"])
        self.assertEqual(profile_jobs, [])

    def test_company_ids_validation(self):
        for extra, existing in (
            (["--phase", "cases", "--company-ids", "3000000000003"], []),
            (["--phase", "profiles", "--company-ids", "3000000000003"], []),
            (["--phase", "profiles", "--company-ids", "123"], []),
        ):
            with self.subTest(extra=extra), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.run_main(["--revise", *extra], existing=existing)


if __name__ == "__main__":
    unittest.main()
