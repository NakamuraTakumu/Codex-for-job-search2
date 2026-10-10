import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "jobs" / "company_profile_fill" / "validate_output.py"


class ValidateOutputTests(unittest.TestCase):
    def _missing_metric(self, note="公開情報で確認できなかった。"):
        return {"records": [], "note": note}

    def _found_metric(self, value, url="https://example.com/profile", kind="official"):
        return {
            "records": [
                {
                    "value": value,
                    "source": {
                        "kind": kind,
                        "url": url,
                    },
                }
            ]
        }

    def _base_payload(self):
        return {
            "company_id": "1010001128061",
            "legal_name": "日本アイ・ビー・エム株式会社",
            "company_kind": "legal_entity",
            "basic_information": {
                "average_annual_income_yen": self._missing_metric(),
                "new_graduate_turnover_rate_within_3_years_percent": self._missing_metric(),
                "average_tenure_years": self._missing_metric(),
                "average_age_years": self._missing_metric(),
                "average_overtime_hours_per_month": self._missing_metric(),
                "annual_holidays_days": self._missing_metric(),
                "paid_leave_usage_days": self._missing_metric(),
                "remote_work_policy": self._missing_metric(),
                "eruboshi_certification_tier": self._missing_metric(),
                "established_year": self._missing_metric(),
                "employee_count": self._missing_metric(),
                "capital_yen": self._missing_metric(),
                "research_and_development_expenses_yen": self._missing_metric(),
                "research_output_publication_presence": self._missing_metric(),
                "industry_academia_collaboration_presence": self._missing_metric(),
                "industry_classification_codes": self._missing_metric(),
            },
        }

    def _run_validator(self, payload, *, subcwd=None):
        if subcwd is None:
            with tempfile.TemporaryDirectory() as tempdir:
                Path(tempdir, "company_profile.json").write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                return self._run_validator(payload, subcwd=tempdir)
        env = os.environ.copy()
        env["CODEX_WRAPPER_SUBCWD"] = str(subcwd)
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), "1010001128061", "日本アイ・ビー・エム株式会社"],
            input="completed",
            text=True,
            capture_output=True,
            cwd=str(ROOT),
            env=env,
            check=False,
        )
        return proc

    def test_matching_collected_output_passes(self):
        payload = self._base_payload()
        with tempfile.TemporaryDirectory() as subcwd:
            Path(subcwd, "company_profile.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            proc = self._run_validator(payload, subcwd=subcwd)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_collected_output_fails(self):
        payload = self._base_payload()
        with tempfile.TemporaryDirectory() as subcwd:
            proc = self._run_validator(payload, subcwd=subcwd)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("collected output file is missing", proc.stderr)

    def test_final_response_json_is_ignored(self):
        payload = self._base_payload()
        collected = self._base_payload()
        collected["basic_information"]["average_age_years"] = self._found_metric(36)
        with tempfile.TemporaryDirectory() as subcwd:
            Path(subcwd, "company_profile.json").write_text(
                json.dumps(collected, ensure_ascii=False),
                encoding="utf-8",
            )
            proc = self._run_validator(payload, subcwd=subcwd)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_number_metric_without_schema_annotations_passes(self):
        payload = self._base_payload()
        payload["basic_information"]["average_age_years"] = self._found_metric(36)

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_enum_metric_without_schema_annotations_passes(self):
        payload = self._base_payload()
        payload["basic_information"]["remote_work_policy"] = self._found_metric("hybrid")
        payload["basic_information"]["eruboshi_certification_tier"] = self._found_metric(
            "stage_3",
            url="https://www.mhlw.go.jp/example/eruboshi",
        )
        payload["basic_information"]["industry_classification_codes"] = self._found_metric(
            ["G_information_communications"]
        )

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_research_and_development_expenses_metric_passes(self):
        payload = self._base_payload()
        payload["basic_information"]["research_and_development_expenses_yen"] = self._found_metric(
            123456789,
            url="https://example.com/ir",
        )

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_legacy_reference_year_is_rejected(self):
        payload = self._base_payload()
        metric = self._found_metric(36)
        metric["records"][0]["reference_year"] = 2025
        payload["basic_information"]["average_age_years"] = metric

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_legacy_output_type_fields_are_rejected(self):
        payload = self._base_payload()
        payload["basic_information"]["average_age_years"] = self._found_metric(36)
        payload["basic_information"]["average_age_years"]["value_type"] = "number"

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_field_level_searched_sources_is_rejected(self):
        payload = self._base_payload()
        payload["basic_information"]["average_age_years"]["searched_sources"] = []

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("field-level searched_sources is not allowed", proc.stderr)

if __name__ == "__main__":
    unittest.main()
