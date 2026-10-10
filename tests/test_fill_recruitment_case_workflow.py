import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jobs.common.output_paths import RECRUITMENT_CASE_OUTPUT_PATH
from scripts import fill_recruitment_case


def _response_file(payload):
    return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))


class FillRecruitmentCaseWorkflowTests(unittest.TestCase):
    def test_single_runner_passes_model_options_to_client(self):
        captured = {}
        payload = {
            "recruitment_year": 2028,
            "publishing_company_id": "1010001128065",
            "application_slot_inventory": {
                "records": [], "note": "新卒本選考枠を確認できなかった。"
            },
            "application_slots": [],
        }

        class FakeJob:
            def __init__(self, **kwargs):
                captured["init"] = kwargs

            def run(self):
                captured["run"] = True
                return SimpleNamespace(
                    files={
                        RECRUITMENT_CASE_OUTPUT_PATH: _response_file(payload),
                    }
                )

        args = SimpleNamespace(
            company_id="1010001128065",
            legal_name="日本アイ・ビー・エム株式会社",
            recruitment_year=2028,
            model="gpt-6-luna",
            reasoning_effort="high",
            codex_bin="/opt/codex/bin/codex",
        )
        with patch.object(fill_recruitment_case, "RecruitmentCaseFillJob", FakeJob):
            result = fill_recruitment_case.run_recruitment_case(args)

        self.assertEqual(captured["init"]["config"].model, "gpt-6-luna")
        self.assertEqual(captured["init"]["config"].reasoning_effort, "high")
        self.assertEqual(
            captured["init"]["config"].codex_bin,
            "/opt/codex/bin/codex",
        )
        self.assertEqual(captured["init"]["company_id"], "1010001128065")
        self.assertTrue(captured["run"])
        self.assertEqual(result["recruitment_year"], 2028)

    def test_missing_collected_file_is_rejected(self):
        class FakeJob:
            def __init__(self, **kwargs):
                pass

            def run(self):
                return SimpleNamespace(files={})

        args = SimpleNamespace(
            company_id="1010001128065",
            legal_name="日本アイ・ビー・エム株式会社",
            recruitment_year=2028,
            model=None,
            reasoning_effort=None,
        )
        with patch.object(fill_recruitment_case, "RecruitmentCaseFillJob", FakeJob):
            with self.assertRaisesRegex(RuntimeError, "recruitment_case.json"):
                fill_recruitment_case.run_recruitment_case(args)


if __name__ == "__main__":
    unittest.main()
