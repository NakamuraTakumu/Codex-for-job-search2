"""TBA is sourced application-date evidence, not inferred absence."""
import base64
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from pydantic import ValidationError

from jobs.common.validator_helpers import validate_date_retrieval
from schemas.common_models import SourcedMetric
from schemas.recruitment_case_models import RecruitmentCase, SourcedDateMetric
from scripts import build_viewer as bv
from tests.test_date_retrieval import HOST, WEB, URL, log_entry, payload, record

ALIAS = "https://EXAMPLE.COM.:443/recruit#schedule"
FIELDS = ("application_open_date", "application_deadline_date")


def later(retrieval=HOST, url=URL):
    return {"records": [], "note": "日付は決まり次第ご案内。",
            "announced_later": {"source": {"kind": "official", "url": url},
                                "retrieval": copy.deepcopy(retrieval)}}


def tba_payload(retrieval=HOST, url=URL):
    data = payload()
    window = data["application_slots"][0]["application_windows"]["records"][0]
    for field in FIELDS:
        window[field] = later(retrieval, url)
    return data


class AnnouncedLaterSchemaTests(unittest.TestCase):
    def test_both_tba_and_partial_date_windows_are_recordable(self):
        for other in (later(), {"records": [record()]}, {"records": [], "note": "不明"}):
            data = tba_payload()
            data["application_slots"][0]["application_windows"]["records"][0][FIELDS[1]] = other
            with self.subTest(other=other):
                case = RecruitmentCase.model_validate(data)
                self.assertIsNotNone(case.application_slots[0].application_windows.records[0].application_open_date.announced_later)

    def test_legacy_missing_defaults_to_none(self):
        self.assertIsNone(SourcedDateMetric.model_validate({"records": [], "note": "不明"}).announced_later)
        RecruitmentCase.model_validate(payload())

    def test_tba_cannot_coexist_with_date_records(self):
        data = later()
        data["records"] = [record()]
        with self.assertRaisesRegex(ValidationError, "must be null"):
            SourcedDateMetric.model_validate(data)

    def test_tba_still_requires_note_source_and_retrieval(self):
        for key in ("note", "source", "retrieval"):
            data = later()
            del (data if key == "note" else data["announced_later"])[key]
            with self.subTest(key=key), self.assertRaises(ValidationError):
                SourcedDateMetric.model_validate(data)
        data = later()
        data["note"] = " "
        with self.assertRaises(ValidationError):
            SourcedDateMetric.model_validate(data)

    def test_retrieval_variants_have_same_constraints_as_dates(self):
        for retrieval in (HOST, {**HOST, "origin": "manual"}, WEB):
            SourcedDateMetric.model_validate(later(retrieval))
        for retrieval in ({**HOST, "fetched_at": "2026-10-11T00:00:00"},
                          {**HOST, "snapshot_id": "fake"}, {"method": "web_tool"}):
            with self.subTest(retrieval=retrieval), self.assertRaises(ValidationError):
                SourcedDateMetric.model_validate(later(retrieval))

    def test_other_metrics_do_not_allow_tba(self):
        with self.assertRaises(ValidationError):
            SourcedMetric[bool].model_validate(later())

    def test_case_rejects_mixed_methods_between_tba_and_date_alias(self):
        data = payload()
        window = data["application_slots"][0]["application_windows"]["records"][0]
        window[FIELDS[0]] = later(WEB, ALIAS)
        with self.assertRaisesRegex(ValidationError, "must not coexist"):
            RecruitmentCase.model_validate(data)

    def test_case_rejects_mixed_tba_methods_across_windows(self):
        data = tba_payload()
        windows = data["application_slots"][0]["application_windows"]["records"]
        second = copy.deepcopy(windows[0])
        second["window_name"] = "第2回"
        for field in FIELDS:
            second[field] = later(WEB, ALIAS)
        windows.append(second)
        with self.assertRaisesRegex(ValidationError, "must not coexist"):
            RecruitmentCase.model_validate(data)

    def test_schema_defines_meaning_and_prohibitions_only_on_date_metric(self):
        schema = RecruitmentCase.model_json_schema()
        field = schema["$defs"]["SourcedDateMetric"]["properties"]["announced_later"]
        self.assertIsNone(field["default"])
        self.assertIn("明示", field["description"])
        self.assertIn("推測しない", field["x-do_not"])
        self.assertIn("随時", field["x-do_not"])
        self.assertEqual(schema["$defs"]["AnnouncedLater"]["properties"]["retrieval"],
                         schema["$defs"]["DateMetricRecord"]["properties"]["retrieval"])


class AnnouncedLaterValidatorTests(unittest.TestCase):
    def validate(self, retrieval=HOST, url=URL, entries=(), fetched_urls=(), failures=()):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log.jsonl"
            log.write_text("".join(json.dumps(e) + "\n" for e in entries))
            log.with_name("failures.jsonl").write_text("".join(json.dumps(e) + "\n" for e in failures))
            validate_date_retrieval(RecruitmentCase.model_validate(tba_payload(retrieval, url)),
                                   log_path=log, fetched_urls=fetched_urls)

    def test_host_tba_requires_matching_log_and_accepts_url_identity(self):
        with self.assertRaisesRegex(ValueError, "announced_later.*no matching"):
            self.validate()
        self.validate(entries=[log_entry()])
        self.validate(url=ALIAS, entries=[log_entry()])
        self.validate(url=URL + "/final", entries=[log_entry()])

    def test_host_tba_rejects_stale_or_mismatched_provenance(self):
        for overrides in ({"fetched_at": "2026-10-10T00:00:00+00:00"},
                          {"snapshot_id": "b" * 64}, {"origin": "manual"}, {"status": 403},
                          {"requested_url": URL + "/other", "final_url": URL + "/else"}):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, "no matching"):
                self.validate(entries=[log_entry(**overrides)])
        self.validate({**HOST, "origin": "manual"}, entries=[log_entry(origin="manual")])

    def test_web_tba_requires_matching_failure_log(self):
        with self.assertRaisesRegex(ValueError, "announced_later.*no matching.*" + URL):
            self.validate(WEB)
        with self.assertRaisesRegex(ValueError, "announced_later.*no matching"):
            self.validate(WEB, failures=[{"requested_url": URL + "/other"}])
        self.validate(WEB, failures=[{"requested_url": ALIAS, "final_url": None}])
        self.validate(WEB, failures=[{"requested_url": URL + "/other", "final_url": URL}])

    def test_web_tba_rejected_for_fetched_url_alias_but_not_old_job(self):
        self.validate(WEB, entries=[log_entry()], failures=[{"requested_url": URL}])
        with self.assertRaisesRegex(ValueError, "announced_later.*web_tool forbidden"):
            self.validate(WEB, ALIAS, fetched_urls={URL})
        self.validate(WEB, URL + "?year=2027", fetched_urls={URL},
                      failures=[{"requested_url": URL + "?year=2027"}])


class AnnouncedLaterViewerTests(unittest.TestCase):
    def slots(self):
        return [(tba_payload()["application_slots"][0], False)]

    def test_cell_detail_source_and_panel_render_tba(self):
        slots = self.slots()
        for field in FIELDS:
            self.assertEqual(bv.window_date_cell(slots, field), ["後日案内", None, ""])
            candidates = bv.window_date_detail(slots, field, 2028)[0]
            self.assertEqual(candidates[0][3:5], [URL, 1])
            self.assertEqual(candidates[0][0], "後日案内（取得 2026-10-11）")
        panel = bv.detail("募集回", slots)["windows"][0]
        self.assertIn("後日案内", panel["open"])
        self.assertIn("後日案内", panel["deadline"])

    def test_missing_and_known_dates_remain_distinct(self):
        slots = self.slots()
        window = slots[0][0]["application_windows"]["records"][0]
        window[FIELDS[0]] = {"records": [], "note": "不明"}
        window[FIELDS[1]] = {"records": [record()]}
        self.assertEqual(bv.window_date_cell(slots, FIELDS[0])[0], bv.MISSING)
        self.assertNotEqual(bv.window_date_cell(slots, FIELDS[1])[0], "後日案内")

    def test_previous_tba_is_not_shifted_and_current_tba_does_not_fall_back(self):
        slots = self.slots()
        previous = copy.deepcopy(slots[0][0])
        previous["application_year"] = 2027
        self.assertEqual(bv.window_date_cell([(previous, True)], FIELDS[0]), ["後日案内", None, "前"])
        panel = bv.detail("募集回", [(previous, True)])["windows"][0]
        self.assertEqual(panel["open"], panel["open1"])
        previous["application_windows"]["records"][0][FIELDS[0]] = {"records": [record()]}
        self.assertEqual(bv.window_date_cell([*slots, (previous, True)], FIELDS[0])[0], "後日案内")

    def test_plaintext_and_encrypted_payload_render_same_tba_and_source_links(self):
        rows, groups, companies = bv.build_rows({}, [tba_payload()])
        def extract(html):
            return json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)[1])
        plain = extract(bv.build_html(rows, groups, companies))
        enc = extract(bv.build_html(rows, groups, companies, password="test-only"))
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(enc["salt"]),
                         iterations=enc["iterations"]).derive(b"test-only")
        decrypted = json.loads(AESGCM(key).decrypt(base64.b64decode(enc["iv"]), base64.b64decode(enc["ct"]), None))
        for data in (plain, decrypted):
            self.assertEqual(data["rows"][0][bv.COL_INDEX["最初の募集開始"]][0], "後日案内")
            candidates = data["groups"][0][str(bv.COL_INDEX["最初の募集開始"])][0]
            self.assertEqual(data["urls"][candidates[0][3]], URL)
            self.assertIn("後日案内", candidates[0][0])
        self.assertEqual({k: v for k, v in plain.items() if k != "built_at"},
                         {k: v for k, v in decrypted.items() if k != "built_at"})


if __name__ == "__main__":
    unittest.main()
