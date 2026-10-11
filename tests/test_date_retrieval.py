import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from jobs.common.codex_job import build_session_config, ReviewConfig
from jobs.common.page_fetch import FETCH_ROOT, read_log
from jobs.common.validator_helpers import validate_date_retrieval
from jobs.company_profile_fill.client import CompanyProfileFillJob
from jobs.recruitment_case_fill.client import RecruitmentCaseFillJob
from jobs.recruitment_case_fill.validate_output import validate_recruitment_case_output
from schemas.common_models import SourcedMetric
from schemas.recruitment_case_models import DateMetricRecord, RecruitmentCase, SourcedDateMetric
from scripts.migrate_date_retrieval import migrate_document, migrate_files
from scripts import build_viewer as bv
from tests import test_recruitment_case_validate_output as existing_tests

STAMP = "2026-10-11T00:00:00+00:00"
SID = "a" * 64
URL = "https://example.com/recruit"
WEB = {"method": "web_tool", "crawled_label": None, "observed_on": None}
HOST = {"method": "host_fetch", "fetched_at": STAMP, "snapshot_id": SID}


def record(retrieval=HOST, url=URL):
    return {"value": {"earliest": "2028-01-31", "latest": "2028-01-31"},
            "source": {"kind": "official", "url": url}, "retrieval": copy.deepcopy(retrieval)}


def payload(retrieval=HOST, url=URL):
    data = existing_tests.RecruitmentCaseValidateOutputTests()._base_payload()
    data["application_slots"][0]["application_windows"] = {"records": [{
        "window_name": "第1回", "source": {"kind": "official", "url": URL},
        "application_open_date": {"records": [], "note": "不明"},
        "application_deadline_date": {"records": [record(retrieval, url)]},
    }]}
    return data


def log_entry(**overrides):
    return {"requested_url": URL, "final_url": URL + "/final", "fetched_at": STAMP,
            "snapshot_id": SID, "sha256": SID, "status": 200, "content_type": "text/html",
            "origin": "host", **overrides}


class DateRetrievalSchemaTests(unittest.TestCase):
    def test_accepts_host_manual_and_nullable_web_variants(self):
        for retrieval in (HOST, {**HOST, "origin": "manual"}, WEB,
                          {"method": "web_tool", "crawled_label": "3 weeks ago", "observed_on": "2026-10-11"}):
            with self.subTest(retrieval=retrieval):
                DateMetricRecord.model_validate(record(retrieval))

    def test_retrieval_required_and_variants_discriminated(self):
        for retrieval in ({}, {"method": "unknown"}, {"method": "host_fetch", "snapshot_id": SID},
                          {**HOST, "snapshot_id": "fake"}, {**HOST, "crawled_label": None},
                          {**WEB, "snapshot_id": SID}, {"method": "web_tool"}, {**HOST, "origin": "web"}):
            with self.subTest(retrieval=retrieval), self.assertRaises(ValidationError):
                DateMetricRecord.model_validate(record(retrieval))
        missing = record()
        del missing["retrieval"]
        with self.assertRaises(ValidationError):
            DateMetricRecord.model_validate(missing)

    def test_utc_required(self):
        for stamp in ("2026-10-11T00:00:00", "2026-10-11T00:00:00+09:00"):
            with self.subTest(stamp=stamp), self.assertRaisesRegex(ValidationError, "UTC"):
                DateMetricRecord.model_validate(record({**HOST, "fetched_at": stamp}))
        DateMetricRecord.model_validate(record({**HOST, "fetched_at": "2026-10-11T00:00:00Z"}))

    def test_no_retrieval_added_to_other_metrics(self):
        metric = {"records": [{"value": True, "source": {"kind": "official", "url": URL}}]}
        SourcedMetric[bool].model_validate(metric)
        metric["records"][0]["retrieval"] = WEB
        with self.assertRaises(ValidationError):
            SourcedMetric[bool].model_validate(metric)

    def test_date_metric_keeps_missing_and_same_url_value_rules(self):
        SourcedDateMetric.model_validate({"records": [], "note": "不明"})
        with self.assertRaises(ValidationError):
            SourcedDateMetric.model_validate({"records": []})
        different = record()
        different["value"] = {"earliest": "2028-02-01", "latest": "2028-02-01"}
        with self.assertRaisesRegex(ValidationError, "same source URL"):
            SourcedDateMetric.model_validate({"records": [record(), different]})

    def test_case_rejects_fresh_and_cached_dates_even_across_windows(self):
        data = payload()
        second = copy.deepcopy(data["application_slots"][0]["application_windows"]["records"][0])
        second["window_name"] = "第2回"
        second["application_deadline_date"]["records"][0]["retrieval"] = WEB
        data["application_slots"][0]["application_windows"]["records"].append(second)
        with self.assertRaisesRegex(ValidationError, "must not coexist"):
            RecruitmentCase.model_validate(data)

    def test_case_rejects_coexistence_through_normalized_url_alias(self):
        data = payload()
        window = data["application_slots"][0]["application_windows"]["records"][0]
        window["application_open_date"] = {"records": [record(WEB, "https://EXAMPLE.COM.:443/recruit#open")]}
        with self.assertRaisesRegex(ValidationError, "must not coexist"):
            RecruitmentCase.model_validate(data)

    def test_schema_exposes_discriminator_and_canonical_prohibitions(self):
        schema = RecruitmentCase.model_json_schema()
        retrieval = schema["$defs"]["DateMetricRecord"]["properties"]["retrieval"]
        self.assertEqual(retrieval["discriminator"]["propertyName"], "method")
        self.assertIn("fetch_page", retrieval["x-do_not"])
        self.assertIn("retrieval", schema["$defs"]["DateMetricRecord"]["required"])


class DateRetrievalValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "log.jsonl"

    def write_log(self, **overrides):
        self.path.write_text(json.dumps(log_entry(**overrides)) + "\n")

    def write_failure(self, **overrides):
        (self.path.parent / "failures.jsonl").write_text(json.dumps({
            "requested_url": URL, "final_url": None, "reason": "bot challenge", **overrides,
        }) + "\n")

    def validate(self, data=None, fetched_urls=()):
        validate_date_retrieval(RecruitmentCase.model_validate(data or payload()),
                               fetched_urls=fetched_urls, log_path=self.path)

    def test_host_record_requires_log(self):
        with self.assertRaisesRegex(ValueError, "Call fetch_page"):
            self.validate()

    def test_matches_requested_or_final_url_and_timestamp(self):
        self.write_log()
        self.validate()
        self.validate(payload(url=URL + "/final"))
        self.validate(payload({**HOST, "fetched_at": "2026-10-11T00:00:00Z"}))

    def test_mismatched_timestamp_snapshot_url_status_or_origin_rejected(self):
        for overrides in ({"fetched_at": "2026-10-10T00:00:00+00:00"},
                          {"snapshot_id": "b" * 64}, {"requested_url": URL + "/other", "final_url": URL + "/else"},
                          {"status": 403}, {"origin": "manual"}):
            with self.subTest(overrides=overrides):
                self.write_log(**overrides)
                with self.assertRaisesRegex(ValueError, "no matching"):
                    self.validate()

    def test_old_text_hash_log_validates_without_snapshot_paths(self):
        self.write_log(truncated=True)
        self.assertNotIn("raw_path", read_log(self.path)[0])
        self.validate()
        self.assertFalse((self.path.parent / "snapshots").exists())

    def test_new_raw_hash_log_validates_with_snapshot_metadata(self):
        self.write_log(raw_path="/snapshots/raw.html", text_path="/snapshots/raw.extracted.txt",
                       bytes=1024, text_chars=30, robots_allowed=True)
        self.validate()

    def test_manual_host_record_matches_manual_log(self):
        self.write_log(origin="manual")
        self.validate(payload({**HOST, "origin": "manual"}))

    def test_web_tool_requires_matching_failure_log(self):
        with self.assertRaisesRegex(ValueError, "web_tool.*no matching.*" + URL):
            self.validate(payload(WEB))
        self.write_failure(requested_url=URL + "/other")
        with self.assertRaisesRegex(ValueError, "no matching host-fetch failure"):
            self.validate(payload(WEB))
        self.write_failure()
        self.validate(payload(WEB))
        self.write_failure(requested_url=URL + "/other", final_url=URL)
        self.validate(payload(WEB))
        self.write_failure(requested_url="https://EXAMPLE.COM.:443/recruit#schedule")
        self.validate(payload(WEB))

    def test_web_tool_rejected_for_this_job_success_and_allowed_for_old_log(self):
        self.write_log()
        self.write_failure()
        self.validate(payload(WEB))
        with self.assertRaisesRegex(ValueError, "web_tool forbidden.*fetch_page succeeded"):
            self.validate(payload(WEB), fetched_urls={URL})
        # URLs fetched by concurrent/earlier jobs do not redefine this survey.
        self.validate(payload(WEB), fetched_urls={URL + "/other"})

    def test_web_tool_alias_of_job_success_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "web_tool forbidden"):
            self.validate(payload(WEB, "https://EXAMPLE.COM.:443/recruit#deadline"), fetched_urls={URL})
        # Path and query differences must not merge genuinely different pages.
        for url in (URL + "?year=2027", "https://example.com/Recruit"):
            self.write_failure(requested_url=url)
            self.validate(payload(WEB, url), fetched_urls={URL})

    def test_recruitment_output_validator_integrates_failure_log(self):
        root = Path(self.temp.name)
        (root / "recruitment_case.json").write_text(json.dumps(payload(WEB)))
        kwargs = dict(subcwd=root, expected_company_id="1180301018771",
                      expected_recruitment_year=2028, fetch_log_path=self.path)
        with self.assertRaisesRegex(ValueError, "web_tool.*no matching.*" + URL):
            validate_recruitment_case_output(**kwargs)
        self.write_failure()
        validate_recruitment_case_output(**kwargs)

    def test_recruitment_output_validator_integrates_job_fresh_urls(self):
        root = Path(self.temp.name)
        (root / "recruitment_case.json").write_text(json.dumps(payload(WEB)))
        with self.assertRaisesRegex(ValueError, "web_tool forbidden"):
            validate_recruitment_case_output(subcwd=root, expected_company_id="1180301018771",
                expected_recruitment_year=2028, fetched_urls={URL}, fetch_log_path=self.path)


class DateRetrievalMigrationTests(unittest.TestCase):
    def test_idempotent_and_no_other_fields_changed(self):
        before = payload(WEB)
        r = before["application_slots"][0]["application_windows"]["records"][0]["application_deadline_date"]["records"][0]
        del r["retrieval"]
        data = copy.deepcopy(before)
        self.assertEqual(migrate_document(data), 1)
        self.assertEqual(migrate_document(data), 0)
        self.assertEqual(data["application_slots"][0]["application_windows"]["records"][0]["application_deadline_date"]["records"][0]["retrieval"], WEB)
        stripped = copy.deepcopy(data)
        del stripped["application_slots"][0]["application_windows"]["records"][0]["application_deadline_date"]["records"][0]["retrieval"]
        self.assertEqual(stripped, before)
        RecruitmentCase.model_validate(data)

    def test_existing_host_retrieval_untouched(self):
        data = payload()
        before = copy.deepcopy(data)
        self.assertEqual(migrate_document(data), 0)
        self.assertEqual(data, before)

    def test_file_migration_idempotent_and_validates_all(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.json"
            data = payload(WEB)
            del data["application_slots"][0]["application_windows"]["records"][0]["application_deadline_date"]["records"][0]["retrieval"]
            path.write_text(json.dumps(data))
            self.assertEqual(migrate_files(Path(directory)), {"files_changed": 1, "records_changed": 1, "files_validated": 1})
            content = path.read_bytes()
            self.assertEqual(migrate_files(Path(directory)), {"files_changed": 0, "records_changed": 0, "files_validated": 1})
            self.assertEqual(path.read_bytes(), content)


class DateRetrievalViewerAndJobTests(unittest.TestCase):
    def test_viewer_labels_host_manual_and_cached_records(self):
        for retrieval, label in ((HOST, "取得 2026-10-11"), ({**HOST, "origin": "manual"}, "手動取得 2026-10-11"),
                                 (WEB, "検索キャッシュ"), ({**WEB, "crawled_label": "3 weeks ago"}, "検索キャッシュ (3 weeks ago)")):
            with self.subTest(retrieval=retrieval):
                data = payload(retrieval)
                slots = [(data["application_slots"][0], False)]
                self.assertIn(label, bv.window_date_detail(slots, "application_deadline_date", 2028)[0][0][0])
                self.assertIn(label, bv.detail("case", slots)["windows"][0]["deadline"])

    def test_tool_registered_for_fill_and_revise_not_company_or_reviewer(self):
        config = build_session_config()
        for existing in (None, "/tmp/existing.json"):
            job = RecruitmentCaseFillJob(config=config, company_id="1234567890123", legal_name="企業", recruitment_year=2028,
                                         existing_output_path=existing)
            self.assertEqual(len(job.tools), 1)
            self.assertEqual(job.page_fetcher.cap, 40)
            self.assertEqual(job.page_fetcher.context["company_id"], "1234567890123")
            self.assertEqual(job.page_fetcher.context["recruitment_year"], 2028)
            review = job._definition["review"]
            self.assertIsNone(review.session.tools)
            self.assertIn(str(FETCH_ROOT), review.session.workspace.read_paths)
            snapshots = str(FETCH_ROOT / "snapshots")
            self.assertIn(snapshots, job.config.workspace.read_paths)
            self.assertIn(snapshots, job._definition["read_paths"])
            self.assertTrue(job._definition["repair_validation_failures"])
            self.assertIn(snapshots, job.subagents.session.workspace.read_paths)
            self.assertNotIn(str(FETCH_ROOT), job.config.workspace.write_paths)
            self.assertNotIn(snapshots, job.config.workspace.write_paths)
            self.assertIn("raw_path", "\n".join(review.context))
            self.assertIn("snapshot_id", "\n".join(review.context))
            self.assertIn(str(FETCH_ROOT / "failures.jsonl"), "\n".join(review.context))
            self.assertIn("requested_url", "\n".join(review.context))
            self.assertIn("final_url", "\n".join(review.context))
        company = CompanyProfileFillJob(config=config, company_id="1234567890123", legal_name="企業")
        self.assertEqual(list(company.tools), [])
        self.assertNotIn(str(FETCH_ROOT), company._definition["review"].session.workspace.read_paths)
        self.assertNotIn(str(FETCH_ROOT / "snapshots"), company.config.workspace.read_paths)

    def test_snapshot_read_path_preserves_caller_read_paths(self):
        config = build_session_config()
        config = config.with_overrides(workspace=config.workspace.with_overrides(
            read_paths=("/tmp/config-input",)))
        job = RecruitmentCaseFillJob(config=config, company_id="1234567890123", legal_name="企業",
                                     recruitment_year=2028, read_paths=("/tmp/caller-input",),
                                     existing_output_path="/tmp/existing.json")
        self.assertEqual(job.config.workspace.read_paths,
                         ("/tmp/config-input", "/tmp/caller-input", str(FETCH_ROOT / "snapshots"),
                          "/tmp/existing.json"))
        self.assertEqual(job.config.workspace.write_paths, ("${SUBCWD}/.",))

    def test_custom_reviewer_gets_snapshot_read_access_and_contract(self):
        config = build_session_config()
        custom = ReviewConfig(context=("custom contract",), session=config,
                              reuse_session=True, enable_peer_exchange=True)
        job = RecruitmentCaseFillJob(config=config, company_id="1234567890123", legal_name="企業", recruitment_year=2028, review=custom)
        review = job._definition["review"]
        self.assertIn("custom contract", review.context)
        self.assertIn(str(FETCH_ROOT), review.session.workspace.read_paths)
        self.assertIn("snapshots", review.context[-1])


if __name__ == "__main__":
    unittest.main()
