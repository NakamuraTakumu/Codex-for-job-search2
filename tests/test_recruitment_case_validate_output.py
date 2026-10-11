import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from schemas.recruitment_case_models import ApplicationSlot


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "jobs" / "recruitment_case_fill" / "validate_output.py"
COMPANY_PROFILE_VALIDATOR = ROOT / "jobs" / "company_profile_fill" / "validate_output.py"
COLLECTED_OUTPUT = "recruitment_case.json"


class RecruitmentCaseValidateOutputTests(unittest.TestCase):
    def _missing_metric(self, note="公開情報で確認できなかった。"):
        return {"records": [], "note": note}

    def _found_metric(self, value, url="https://example.com/recruit", kind="official"):
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

    def _date_metric(self, value, url="https://example.com/recruit", kind="official"):
        if isinstance(value, str):
            value = {"earliest": value, "latest": value}
        metric = self._found_metric(value, url=url, kind=kind)
        metric["records"][0]["retrieval"] = {"method": "web_tool", "crawled_label": None, "observed_on": None}
        return metric

    def _found_slot_inventory(self, *identities):
        return {
            "records": [
                {
                    "application_year": application_year,
                    "slot_name": slot_name,
                    "source": {
                        "kind": "official",
                        "url": f"https://example.com/slots/{application_year}",
                    },
                }
                for application_year, slot_name in identities
            ]
        }

    def _run_validator(self, payload, company_id="1180301018771", recruitment_year="2028", env=None):
        if env is None:
            with tempfile.TemporaryDirectory() as tempdir:
                Path(tempdir, COLLECTED_OUTPUT).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                return self._run_validator(
                    payload,
                    company_id,
                    recruitment_year,
                    {**os.environ, "CODEX_WRAPPER_SUBCWD": tempdir},
                )
        # CLI fixtures own their fetch logs; never depend on or write workspace data/.
        def web_urls(value):
            if isinstance(value, dict):
                if value.get("retrieval", {}).get("method") == "web_tool":
                    yield value["source"]["url"]
                for child in value.values():
                    yield from web_urls(child)
            elif isinstance(value, list):
                for child in value:
                    yield from web_urls(child)

        with tempfile.TemporaryDirectory() as fetch_root:
            Path(fetch_root, "failures.jsonl").write_text("".join(
                json.dumps({"requested_url": url, "final_url": None, "reason": "fixture failure"}) + "\n"
                for url in web_urls(payload)
            ), encoding="utf-8")
            bootstrap = (
                "import runpy, sys; from pathlib import Path; "
                "from jobs.common import page_fetch; "
                "page_fetch.FETCH_ROOT = Path(sys.argv.pop(1)); "
                "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
            )
            return subprocess.run(
                [sys.executable, "-c", bootstrap, fetch_root, str(VALIDATOR), company_id, recruitment_year],
                input="completed", text=True, capture_output=True, cwd=str(ROOT), env=env, check=False,
            )

    def _steps(self, *kinds):
        return [{"kind": kind, "label": kind} for kind in kinds]

    def _base_selection_process(self):
        return {
            "process": {
                "records": [
                    {
                        "value": {
                            "stages": [
                                {
                                    "steps": self._steps(
                                        "web_test",
                                        "entry_sheet_submission",
                                        "written_assignment",
                                    )
                                },
                                {"steps": self._steps("group_interview", "interview")},
                            ]
                        },
                        "source": {
                            "kind": "official",
                            "url": "https://example.com/recruit",
                        },
                    }
                ]
            },
            "has_web_test": self._found_metric(True),
            "has_written_exam": self._missing_metric(),
            "has_entry_sheet_submission": self._found_metric(True),
            "has_written_assignment": self._found_metric(True),
            "has_group_discussion": self._missing_metric(),
            "has_group_interview": self._found_metric(True),
            "has_interview": self._found_metric(True),
            "has_case_interview": self._missing_metric(),
            "has_research_presentation": self._missing_metric(),
            "has_coding_task": self._missing_metric(),
        }

    def _base_payload(self):
        return {
            "recruitment_year": 2028,
            "publishing_company_id": "1180301018771",
            "application_slot_inventory": self._found_slot_inventory(
                (2028, "総合職")
            ),
            "application_slots": [
                {
                    "slot_name": "総合職",
                    "application_year": 2028,
                    "application_windows": self._missing_metric(),
                    "assignment_companies": {"company_ids": []},
                    "starting_salary": self._missing_metric(),
                    "starting_salary_master": self._missing_metric(),
                    "starting_salary_doctor": self._missing_metric(),
                    "has_remote_work": self._missing_metric(),
                    "selection_process": self._base_selection_process(),
                }
            ],
        }

    def test_selection_activity_recording_rules_exposed_in_json_schema(self):
        from schemas.recruitment_case_models import RecruitmentCase

        definitions = RecruitmentCase.model_json_schema()["$defs"]
        steps = definitions["SelectionProcessStage"]["properties"]["steps"]
        process = definitions["SelectionProcess"]["properties"]["process"]
        self.assertIn("必須の書類提出・登録", steps["description"])
        for phrase in ("所属 stage や順序", "活動名を process の note", "Missing の場合も"):
            self.assertIn(phrase, process["description"])
        for phrase in ("便宜的な stage", "末尾の stage", "単独の process record にしない"):
            self.assertIn(phrase, process["x-do_not"])
        data = self._base_payload()
        data["application_slots"][0]["selection_process"]["process"] = self._missing_metric(
            "順序不明。必須活動: 履修履歴登録、PRシート提出。"
        )
        RecruitmentCase.model_validate(data)

    def test_application_slots_x_do_not_contains_non_main_flow_exclusion(self):
        from schemas.recruitment_case_models import RecruitmentCase

        json_schema = RecruitmentCase.model_json_schema()
        application_slots = json_schema["properties"]["application_slots"]

        self.assertIn("x-do_not", application_slots)
        self.assertIn("インターン", application_slots["x-do_not"])
        self.assertIn("別の年度の応募枠を対象とするsourceの値を使わない", application_slots["x-do_not"])

    def test_slot_boundary_lives_in_application_slots(self):
        from schemas.recruitment_case_models import RecruitmentCase

        json_schema = RecruitmentCase.model_json_schema()
        application_slots = json_schema["properties"]["application_slots"]
        for phrase in ("応募（エントリー）", "選考ルート", "希望職種", "application_windows"):
            self.assertIn(phrase, application_slots["description"])

        for model_name in ("ApplicationSlot", "ApplicationSlotIdentityRecord"):
            slot_name = json_schema["$defs"][model_name]["properties"]["slot_name"]
            self.assertIn("application_slots", slot_name["description"])
            self.assertNotIn("希望職種", slot_name["description"])

    def test_application_year_forbids_cross_year_source_mixing(self):
        from schemas.recruitment_case_models import RecruitmentCase

        json_schema = RecruitmentCase.model_json_schema()
        application_slot = json_schema["$defs"]["ApplicationSlot"]
        application_year = application_slot["properties"]["application_year"]
        identity_year = json_schema["$defs"]["ApplicationSlotIdentityRecord"][
            "properties"
        ]["application_year"]

        self.assertEqual(application_year["description"], identity_year["description"])
        self.assertEqual(application_year["x-do_not"], identity_year["x-do_not"])
        self.assertIn("卒業予定年兼入社年", application_year["description"])
        self.assertIn("x-do_not", application_year)
        self.assertIn("別の年度の応募枠を対象とするsourceを使わない", application_year["x-do_not"])
        self.assertIn("応募受付", application_year["x-do_not"])
        self.assertIn("翌年", application_year["x-do_not"])

    def test_minimal_recruitment_case_passes(self):
        payload = self._base_payload()

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_application_slot_inventory_is_required(self):
        payload = self._base_payload()
        del payload["application_slot_inventory"]

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("application_slot_inventory", proc.stderr)
        self.assertIn("Field required", proc.stderr)

    def test_empty_application_slots_passes_as_completed_no_slots_result(self):
        payload = self._base_payload()
        payload["application_slot_inventory"] = self._missing_metric()
        payload["application_slots"] = []

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_target_and_previous_year_same_slot_name_pass(self):
        payload = self._base_payload()
        previous_slot = copy.deepcopy(payload["application_slots"][0])
        previous_slot["application_year"] = 2027
        payload["application_slots"].append(previous_slot)
        payload["application_slot_inventory"] = self._found_slot_inventory(
            (2028, "総合職"),
            (2027, "総合職"),
        )

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_inventory_identity_missing_from_details_is_rejected(self):
        payload = self._base_payload()
        payload["application_slot_inventory"] = self._found_slot_inventory(
            (2028, "総合職"),
            (2028, "技術職"),
        )

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing from application_slots", proc.stderr)

    def test_detail_identity_missing_from_inventory_is_rejected(self):
        payload = self._base_payload()
        second_slot = copy.deepcopy(payload["application_slots"][0])
        second_slot["slot_name"] = "技術職"
        payload["application_slots"].append(second_slot)

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing from application_slot_inventory", proc.stderr)

    def test_found_inventory_with_empty_details_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"] = []

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing from application_slots", proc.stderr)

    def test_missing_inventory_with_details_is_rejected(self):
        payload = self._base_payload()
        payload["application_slot_inventory"] = self._missing_metric()

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("identity sets must match", proc.stderr)

    def test_matching_identity_passes_when_all_detail_metrics_are_missing(self):
        payload = self._base_payload()
        selection_process = payload["application_slots"][0]["selection_process"]
        selection_process["process"] = self._missing_metric()
        for field_name in (
            "has_web_test",
            "has_written_exam",
            "has_entry_sheet_submission",
            "has_written_assignment",
            "has_group_discussion",
            "has_group_interview",
            "has_interview",
            "has_case_interview",
            "has_research_presentation",
            "has_coding_task",
        ):
            selection_process[field_name] = self._missing_metric()

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_duplicate_inventory_identity_is_rejected(self):
        payload = self._base_payload()
        payload["application_slot_inventory"] = self._found_slot_inventory(
            (2028, "総合職"),
            (2028, "総合職"),
        )

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("inventory contains duplicate identities", proc.stderr)

    def test_duplicate_detail_identity_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"].append(
            copy.deepcopy(payload["application_slots"][0])
        )

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("application_slots contains duplicate identities", proc.stderr)

    def test_application_slots_schema_allows_empty_array(self):
        from schemas.recruitment_case_models import RecruitmentCase

        application_slots = RecruitmentCase.model_json_schema()["properties"][
            "application_slots"
        ]

        self.assertNotIn("minItems", application_slots)
        self.assertIn("前年度", application_slots["description"])
        self.assertIn("調査未実施", application_slots["description"])

    def test_present_application_slot_missing_required_fields_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"] = [
            {
                "slot_name": "総合職",
                "application_year": 2028,
            }
        ]

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Field required", proc.stderr)

    def test_entry_sheet_and_written_assignment_steps_pass(self):
        payload = self._base_payload()
        steps = payload["application_slots"][0]["selection_process"]["process"]["records"][0]["value"]["stages"][0]["steps"]

        kinds = [step["kind"] for step in steps]
        self.assertIn("entry_sheet_submission", kinds)
        self.assertIn("written_assignment", kinds)

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_multiple_application_windows_preserve_date_pairs(self):
        payload = self._base_payload()
        payload["application_slots"][0]["application_windows"] = {
            "records": [
                {
                    "window_name": "早期選考",
                    "source": {
                        "kind": "official",
                        "url": "https://example.com/early",
                    },
                    "application_open_date": self._date_metric(
                        "2027-10-01", url="https://example.com/early"
                    ),
                    "application_deadline_date": self._date_metric(
                        "2027-10-31", url="https://example.com/early"
                    ),
                },
                {
                    "window_name": "通常選考 第1回",
                    "source": {
                        "kind": "official",
                        "url": "https://example.com/regular-1",
                    },
                    "application_open_date": self._date_metric(
                        "2028-01-05", url="https://example.com/regular-1"
                    ),
                    "application_deadline_date": self._date_metric(
                        "2028-01-31", url="https://example.com/regular-1"
                    ),
                },
            ]
        }

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_exact_duplicate_application_window_is_rejected(self):
        payload = self._base_payload()
        window = {
            "window_name": "通常選考",
            "source": {
                "kind": "official",
                "url": "https://example.com/window",
            },
            "application_open_date": self._date_metric(
                "2028-01-01", url="https://example.com/window"
            ),
            "application_deadline_date": self._date_metric(
                "2028-01-31", url="https://example.com/window"
            ),
        }
        payload["application_slots"][0]["application_windows"] = {
            "records": [window, copy.deepcopy(window)]
        }

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("exact duplicate object", proc.stderr)

    def test_same_name_nonduplicate_application_windows_pass(self):
        payload = self._base_payload()
        windows = []
        for suffix, deadline in (("a", "2028-01-31"), ("b", "2028-02-29")):
            url = f"https://example.com/window-{suffix}"
            windows.append(
                {
                    "window_name": "通常選考",
                    "source": {"kind": "official", "url": url},
                    "application_open_date": self._date_metric(
                        "2028-01-01", url=url
                    ),
                    "application_deadline_date": self._date_metric(
                        deadline, url=url
                    ),
                }
            )
        payload["application_slots"][0]["application_windows"] = {
            "records": windows
        }

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_same_source_reversed_iso_application_dates_are_rejected(self):
        payload = self._base_payload()
        url = "https://example.com/window"
        payload["application_slots"][0]["application_windows"] = {
            "records": [
                {
                    "window_name": None,
                    "source": {"kind": "official", "url": url},
                    "application_open_date": self._date_metric(
                        "2028-02-01", url=url
                    ),
                    "application_deadline_date": self._date_metric(
                        "2028-01-31", url=url
                    ),
                }
            ]
        }

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("open date is after deadline", proc.stderr)

    def test_date_order_skips_different_sources(self):
        payload = self._base_payload()
        payload["application_slots"][0]["application_windows"] = {
            "records": [
                {
                    "window_name": None,
                    "source": {
                        "kind": "official",
                        "url": "https://example.com/window",
                    },
                    "application_open_date": self._date_metric(
                        "2028-02-01", url="https://example.com/open"
                    ),
                    "application_deadline_date": self._date_metric(
                        "2028-01-31", url="https://example.com/deadline"
                    ),
                }
            ]
        }
        proc = self._run_validator(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_window_dates_must_be_normalized(self):
        for value in ("2028年2月1日", "2月1日", "2028-02-01 12:00"):
            with self.subTest(value=value):
                payload = self._base_payload()
                payload["application_slots"][0]["application_windows"] = {
                    "records": [
                        {
                            "window_name": None,
                            "source": {
                                "kind": "official",
                                "url": "https://example.com/window",
                            },
                            "application_open_date": self._date_metric(
                                value, url="https://example.com/window"
                            ),
                            "application_deadline_date": self._missing_metric(),
                        }
                    ]
                }

                proc = self._run_validator(payload)

                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("schema validation failed", proc.stderr)

    def test_legacy_standalone_application_dates_are_rejected(self):
        payload = self._base_payload()
        slot = payload["application_slots"][0]
        del slot["application_windows"]
        slot["application_open_date"] = self._missing_metric()
        slot["application_deadline_date"] = self._missing_metric()

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_application_window_allows_unnamed_and_partial_dates(self):
        payload = self._base_payload()
        payload["application_slots"][0]["application_windows"] = {
            "records": [
                {
                    "window_name": None,
                    "source": {
                        "kind": "official",
                        "url": "https://example.com/deadline-only",
                    },
                    "application_open_date": self._missing_metric(),
                    "application_deadline_date": self._date_metric(
                        "2028-02-29",
                        url="https://example.com/deadline-only",
                    ),
                }
            ]
        }

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_empty_application_windows_are_rejected(self):
        payload = self._base_payload()
        payload["application_slots"][0]["application_windows"] = {
            "records": []
        }

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_starting_salary_breakdown_requires_monthly_total_or_base(self):
        payload = self._base_payload()
        slot = payload["application_slots"][0]
        slot["starting_salary"] = self._found_metric(
            {
                "monthly_total_yen": 300000,
                "fixed_overtime_yen": 60000,
                "fixed_overtime_hours": 30,
            }
        )
        slot["starting_salary"]["note"] = "別途住宅手当あり。"

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

        slot["starting_salary"] = self._found_metric({"fixed_overtime_yen": 60000})

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("monthly_total_yen or base_salary_yen", proc.stderr)

    def test_field_level_searched_sources_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"][0]["starting_salary"][
            "searched_sources"
        ] = []

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("field-level searched_sources is not allowed", proc.stderr)

    def test_subcwd_collected_output_passes_with_prose_response(self):
        payload = self._base_payload()
        with tempfile.TemporaryDirectory() as tempdir:
            Path(tempdir, COLLECTED_OUTPUT).write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            env = {**os.environ, "CODEX_WRAPPER_SUBCWD": tempdir}

            proc = self._run_validator(payload, env=env)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_subcwd_collected_output_missing_is_rejected(self):
        payload = self._base_payload()
        with tempfile.TemporaryDirectory() as tempdir:
            env = {**os.environ, "CODEX_WRAPPER_SUBCWD": tempdir}

            proc = self._run_validator(payload, env=env)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("collected output file is missing", proc.stderr)

    def test_final_response_json_is_ignored(self):
        payload = self._base_payload()
        collected_payload = self._base_payload()
        collected_payload["application_slots"][0]["slot_name"] = "別枠"
        collected_payload["application_slot_inventory"] = self._found_slot_inventory(
            (2028, "別枠")
        )
        with tempfile.TemporaryDirectory() as tempdir:
            Path(tempdir, COLLECTED_OUTPUT).write_text(
                json.dumps(collected_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            env = {**os.environ, "CODEX_WRAPPER_SUBCWD": tempdir}

            proc = self._run_validator(payload, env=env)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_recruitment_year_mismatch_is_rejected(self):
        payload = self._base_payload()

        proc = self._run_validator(payload, recruitment_year="2029")

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("recruitment_year must preserve input value", proc.stderr)

    def test_application_year_may_differ_from_recruitment_year(self):
        payload = self._base_payload()
        payload["application_slots"][0]["application_year"] = 2027
        payload["application_slot_inventory"] = self._found_slot_inventory(
            (2027, "総合職")
        )

        proc = self._run_validator(payload, recruitment_year="2028")

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_graduation_year_slot_may_have_previous_calendar_year_window(self):
        payload = self._base_payload()
        payload["application_slots"][0]["application_windows"] = {
            "records": [
                {
                    "window_name": None,
                    "source": {
                        "kind": "official",
                        "url": "https://example.com/2028-entry",
                    },
                    "application_open_date": self._date_metric(
                        "2027-03-01",
                        url="https://example.com/2028-entry",
                    ),
                    "application_deadline_date": self._missing_metric(),
                }
            ]
        }

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_application_year_rule_lives_only_in_schema(self):
        root = Path(__file__).resolve().parents[1]
        quality_contract = (
            root / "jobs/recruitment_case_fill/quality_contract.md"
        ).read_text(encoding="utf-8")
        description = ApplicationSlot.model_fields["application_year"].description

        self.assertIn("年度の記載がないsource", description)
        self.assertIn("recruitment_yearの枠にし", description)
        self.assertNotIn("2028年卒業予定", quality_contract)

    def test_process_missing_metric_passes(self):
        payload = self._base_payload()
        payload["application_slots"][0]["selection_process"]["process"] = self._missing_metric()

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_process_multiple_records_passes(self):
        payload = self._base_payload()
        payload["application_slots"][0]["selection_process"]["process"]["records"].append(
            {
                "value": {
                    "stages": [
                        {"steps": self._steps("web_test")},
                        {
                            "steps": self._steps(
                                "entry_sheet_submission",
                                "written_assignment",
                                "interview",
                            )
                        },
                    ]
                },
                "source": {
                    "kind": "unofficial",
                    "url": "https://example.com/review",
                },
            }
        )

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_selection_step_rejects_all_false_corresponding_metrics(self):
        mappings = {
            "web_test": ("has_web_test",),
            "written_exam": ("has_written_exam",),
            "entry_sheet_submission": ("has_entry_sheet_submission",),
            "written_assignment": ("has_written_assignment",),
            "group_discussion": ("has_group_discussion",),
            "group_interview": ("has_group_interview", "has_interview"),
            "interview": ("has_interview",),
            "case_interview": ("has_case_interview", "has_interview"),
            "research_presentation": ("has_research_presentation",),
            "coding_task": ("has_coding_task",),
        }
        for step, field_names in mappings.items():
            for field_name in field_names:
                with self.subTest(step=step, field_name=field_name):
                    payload = self._base_payload()
                    selection = payload["application_slots"][0]["selection_process"]
                    selection["process"]["records"][0]["value"][
                        "stages"
                    ] = [{"steps": self._steps(step)}]
                    for bool_field in (
                        "has_web_test",
                        "has_written_exam",
                        "has_entry_sheet_submission",
                        "has_written_assignment",
                        "has_group_discussion",
                        "has_group_interview",
                        "has_interview",
                        "has_case_interview",
                        "has_research_presentation",
                        "has_coding_task",
                    ):
                        selection[bool_field] = self._missing_metric()
                    selection[field_name] = self._found_metric(False)

                    proc = self._run_validator(payload)

                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn(f"{step} -> {field_name}", proc.stderr)

    def test_selection_step_allows_missing_or_any_true_corresponding_metric(self):
        for metric in (
            self._missing_metric(),
            {
                "records": [
                    self._found_metric(False)["records"][0],
                    self._found_metric(
                        True, url="https://example.com/other"
                    )["records"][0],
                ]
            },
        ):
            with self.subTest(metric=metric):
                payload = self._base_payload()
                selection = payload["application_slots"][0]["selection_process"]
                selection["has_web_test"] = metric

                proc = self._run_validator(payload)

                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_selection_process_step_absence_does_not_require_false(self):
        payload = self._base_payload()
        selection = payload["application_slots"][0]["selection_process"]
        selection["process"]["records"][0]["value"]["stages"] = [
            {"steps": self._steps("web_test")}
        ]
        selection["has_coding_task"] = self._found_metric(True)

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_legacy_reference_year_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"][0]["selection_process"]["process"][
            "records"
        ][0]["reference_year"] = 2028

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_old_process_array_shape_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"][0]["selection_process"]["process"] = [
            ["web_test", "entry_sheet_submission"],
            ["interview"],
        ]

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_legacy_essay_step_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"][0]["selection_process"]["process"][
            "records"
        ][0]["value"]["stages"][0]["steps"] = [{"kind": "essay", "label": "小論文"}]

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_legacy_has_essay_field_is_rejected(self):
        payload = self._base_payload()
        selection_process = payload["application_slots"][0]["selection_process"]
        selection_process["has_essay"] = selection_process.pop(
            "has_entry_sheet_submission"
        )

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_process_object_without_stages_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"][0]["selection_process"]["process"] = {
            "records": [
                {
                    "value": {},
                    "source": {
                        "kind": "official",
                        "url": "https://example.com/recruit",
                    },
                }
            ]
        }

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_process_stage_extra_field_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"][0]["selection_process"]["process"]["records"][0]["value"]["stages"][0] = {
            "steps": self._steps("web_test", "entry_sheet_submission"),
            "label": "書類選考",
        }

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_process_empty_steps_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"][0]["selection_process"]["process"]["records"][0]["value"]["stages"][0] = {
            "steps": []
        }

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_process_invalid_step_is_rejected(self):
        payload = self._base_payload()
        payload["application_slots"][0]["selection_process"]["process"]["records"][0]["value"]["stages"][0] = {
            "steps": [{"kind": "unknown_step", "label": "unknown"}]
        }

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_process_other_step_keeps_source_label(self):
        payload = self._base_payload()
        stages = payload["application_slots"][0]["selection_process"]["process"]["records"][0]["value"]["stages"]
        stages.insert(1, {"steps": [{"kind": "other", "label": "リクルーター面談"}]})

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

        stages[1] = {"steps": [{"kind": "other"}]}

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_group_interview_step_and_field_are_accepted(self):
        payload = self._base_payload()

        proc = self._run_validator(payload)

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_group_interview_field_is_rejected(self):
        payload = self._base_payload()
        del payload["application_slots"][0]["selection_process"]["has_group_interview"]

        proc = self._run_validator(payload)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema validation failed", proc.stderr)

    def test_company_profile_scalar_metrics_still_pass(self):
        payload = {
            "company_id": "1180301018771",
            "legal_name": "トヨタ自動車株式会社",
            "company_kind": "legal_entity",
            "basic_information": {
                "average_annual_income_yen": self._found_metric(10000000, url="https://example.com/ir"),
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
                "research_output_publication_presence": self._found_metric(True, url="https://example.com/rd"),
                "industry_academia_collaboration_presence": self._missing_metric(),
                "industry_classification_codes": self._found_metric(["E_manufacturing"], url="https://example.com/ir"),
            },
        }

        with tempfile.TemporaryDirectory() as tempdir:
            Path(tempdir, "company_profile.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            proc = subprocess.run(
                [sys.executable, str(COMPANY_PROFILE_VALIDATOR), "1180301018771", "トヨタ自動車株式会社"],
                input="completed",
                text=True,
                capture_output=True,
                cwd=str(ROOT),
                env={**os.environ, "CODEX_WRAPPER_SUBCWD": tempdir},
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
