import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.fill_recruitment_cases import run_batch


class FakeBatch:
    def __init__(self, responses=None, errors=None, error=None):
        self.responses = responses or []
        self.errors = errors or [None] * len(self.responses)
        self.error = error

    def result(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            responses=tuple(self.responses),
            errors=tuple(self.errors),
            usage={"input_tokens": 10},
            usage_complete=True,
            usage_incomplete_reasons=(),
            successful_usage={"input_tokens": 10},
            failed_usage={},
            estimated_cost_usd=0.01,
            usage_by_model={"main-model": {"input_tokens": 10}},
            successful_usage_by_model={
                "main-model": {"input_tokens": 10}
            },
            failed_usage_by_model={},
            estimated_cost_usd_by_model={"main-model": 0.01},
        )


class FakePool:
    def __init__(self, responses=None, errors=None, error=None):
        self.responses = responses or []
        self.errors = errors or [None] * len(self.responses)
        self.error = error
        self.jobs = None

    def start_many(self, jobs, *, on_task_completed, max_tasks):
        self.jobs = list(jobs)
        if self.error is None:
            for job, response in zip(self.jobs, self.responses, strict=True):
                if response is not None:
                    on_task_completed(SimpleNamespace(job=job, response=response))
        return FakeBatch(self.responses, self.errors, self.error)


class FakeJob:
    def __init__(
        self,
        *,
        config,
        company_id,
        legal_name,
        recruitment_year,
    ):
        self.config = config
        self.company_id = company_id
        self.legal_name = legal_name
        self.recruitment_year = recruitment_year


def response_for(company_id):
    payload = {
        "recruitment_year": 2028,
        "publishing_company_id": company_id,
        "application_slot_inventory": {
            "records": [], "note": "新卒本選考枠を確認できなかった。"
        },
        "application_slots": [],
    }
    return SimpleNamespace(
        files={
            "recruitment_case.json": SimpleNamespace(
                text=json.dumps(payload, ensure_ascii=False)
            )
        },
        log_path=f"/logs/{company_id}.log",
        usage={"input_tokens": 5},
        estimated_cost_usd=0.005,
    )


class FillRecruitmentCasesBatchTests(unittest.TestCase):
    def test_run_batch_uses_worker_pool_and_saves_outputs(self):
        targets = [
            {"company_id": "1111111111111", "legal_name": "一社"},
            {"company_id": "2222222222222", "legal_name": "二社"},
        ]
        pool = FakePool([response_for(target["company_id"]) for target in targets])

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "jobs.common.batch.CodexWorkerPool",
                return_value=pool,
            ) as pool_class,
            patch(
                "scripts.fill_recruitment_cases.RecruitmentCaseFillJob",
                FakeJob,
            ),
        ):
            summary = run_batch(
                config=object(),
                targets=targets,
                recruitment_year=2028,
                max_workers=2,
                warmup_jobs=1,
                output_dir=Path(directory),
            )
            self.assertEqual(summary["success_count"], 2)
            self.assertEqual(summary["failure_count"], 0)
            self.assertTrue(summary["batch_usage_complete"])
            self.assertEqual(summary["batch_usage_incomplete_reasons"], [])
            self.assertEqual(summary["batch_successful_usage"], {"input_tokens": 10})
            self.assertEqual(summary["batch_failed_usage"], {})
            self.assertEqual(
                summary["batch_usage_by_model"],
                {"main-model": {"input_tokens": 10}},
            )
            self.assertEqual(
                summary["batch_successful_usage_by_model"],
                {"main-model": {"input_tokens": 10}},
            )
            self.assertEqual(summary["batch_failed_usage_by_model"], {})
            self.assertEqual(
                summary["batch_estimated_cost_usd_by_model"],
                {"main-model": 0.01},
            )
            self.assertTrue((Path(directory) / "1111111111111.json").is_file())
            self.assertTrue((Path(directory) / "2222222222222.json").is_file())

        pool_class.assert_called_once_with(
            max_workers=2,
            warmup_jobs=1,
        )
        self.assertEqual(len(pool.jobs), 2)
        self.assertEqual(pool.jobs[0].recruitment_year, 2028)

    def test_batch_failure_marks_unfinished_targets(self):
        targets = [
            {"company_id": "1111111111111", "legal_name": "一社"},
            {"company_id": "2222222222222", "legal_name": "二社"},
        ]
        pool = FakePool(error=RuntimeError("batch failed"))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "jobs.common.batch.CodexWorkerPool",
                return_value=pool,
            ),
            patch(
                "scripts.fill_recruitment_cases.RecruitmentCaseFillJob",
                FakeJob,
            ),
        ):
            summary = run_batch(
                config=object(),
                targets=targets,
                recruitment_year=2028,
                max_workers=2,
                warmup_jobs=0,
                output_dir=Path(directory),
            )

        self.assertEqual(summary["success_count"], 0)
        self.assertEqual(summary["failure_count"], 2)
        self.assertFalse(summary["batch_usage_complete"])
        self.assertEqual(
            summary["batch_usage_incomplete_reasons"],
            ["batch did not return a result"],
        )
        self.assertEqual(summary["batch_error"]["error_type"], "RuntimeError")
        self.assertTrue(
            all(
                item["status"] == "batch_failed_or_cancelled"
                for item in summary["results"]
            )
        )

    def test_task_failure_does_not_cancel_other_targets(self):
        targets = [
            {"company_id": "1111111111111", "legal_name": "一社"},
            {"company_id": "2222222222222", "legal_name": "二社"},
        ]
        task_error = RuntimeError("one company failed")
        pool = FakePool(
            responses=[None, response_for("2222222222222")],
            errors=[task_error, None],
        )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "jobs.common.batch.CodexWorkerPool",
                return_value=pool,
            ),
            patch(
                "scripts.fill_recruitment_cases.RecruitmentCaseFillJob",
                FakeJob,
            ),
        ):
            summary = run_batch(
                config=object(),
                targets=targets,
                recruitment_year=2028,
                max_workers=2,
                warmup_jobs=0,
                output_dir=Path(directory),
            )

            self.assertEqual(summary["success_count"], 1)
            self.assertEqual(summary["failure_count"], 1)
            self.assertEqual(summary["results"][0]["status"], "failed")
            self.assertEqual(summary["results"][0]["error_type"], "RuntimeError")
            self.assertEqual(summary["results"][1]["status"], "success")
            self.assertTrue((Path(directory) / "2222222222222.json").is_file())

    def test_task_failure_preserves_collected_diagnostic_output(self):
        target = {"company_id": "1111111111111", "legal_name": "一社"}
        failed_response = response_for(target["company_id"])
        task_error = RuntimeError("review failed")
        task_error.files = failed_response.files
        task_error.log_path = "/logs/failed.log"
        pool = FakePool(responses=[None], errors=[task_error])

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "jobs.common.batch.CodexWorkerPool",
                return_value=pool,
            ),
            patch(
                "scripts.fill_recruitment_cases.RecruitmentCaseFillJob",
                FakeJob,
            ),
        ):
            summary = run_batch(
                config=object(),
                targets=[target],
                recruitment_year=2028,
                max_workers=1,
                warmup_jobs=0,
                output_dir=Path(directory),
            )

            diagnostic = Path(directory) / "1111111111111.failed.json"
            self.assertTrue(diagnostic.is_file())
            self.assertEqual(
                summary["results"][0]["diagnostic_output_path"],
                str(diagnostic),
            )


if __name__ == "__main__":
    unittest.main()
