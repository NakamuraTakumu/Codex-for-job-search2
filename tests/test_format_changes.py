"""Industry list, DateRange, viewer formatting/skipping, and --only-invalid."""

import base64
import contextlib
import io
import shutil
import json
import sys
import tempfile
import unittest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_viewer as bv  # noqa: E402
from schemas.company_profile_models import CompanyProfile  # noqa: E402
from schemas.recruitment_case_models import ApplicationWindow, DateRange  # noqa: E402
from scripts import survey_companies  # noqa: E402
from tests.test_validate_output import ValidateOutputTests  # noqa: E402


def src(url="https://example.com/a"):
    return {"kind": "official", "url": url}


def metric(*values, url="https://example.com/a"):
    return {"records": [{"value": v, "source": src(url)} for v in values]}


def profile_payload(codes=None):
    payload = ValidateOutputTests("_base_payload")._base_payload()
    if codes is not None:
        payload["basic_information"]["industry_classification_codes"] = metric(codes)
    return payload


class IndustryCodesSchemaTests(unittest.TestCase):
    def test_one_to_three_distinct_codes_pass(self):
        for codes in (["E_manufacturing"],
                      ["E_manufacturing", "G_information_communications"],
                      ["E_manufacturing", "G_information_communications", "J_finance_insurance"]):
            CompanyProfile.model_validate(profile_payload(codes))

    def test_bad_lists_are_rejected(self):
        for codes in ([], ["E_manufacturing", "E_manufacturing"],
                      ["E_manufacturing", "G_information_communications",
                       "J_finance_insurance", "K_real_estate_goods_rental_leasing"],
                      "E_manufacturing"):
            with self.assertRaises(ValidationError, msg=codes):
                CompanyProfile.model_validate(profile_payload(codes))


class DateRangeSchemaTests(unittest.TestCase):
    def date_metric(self, value, url):
        found = metric(value, url=url)
        found["records"][0]["retrieval"] = {"method": "web_tool", "crawled_label": None, "observed_on": None}
        return found

    def window(self, open_, deadline, open_url="u1", deadline_url="u1"):
        return {
            "window_name": None,
            "source": src("https://example.com/w"),
            "application_open_date": self.date_metric(open_, f"https://example.com/{open_url}"),
            "application_deadline_date": self.date_metric(deadline, f"https://example.com/{deadline_url}"),
        }

    def test_range_order(self):
        DateRange(earliest="2027-03-01", latest="2027-03-01")
        with self.assertRaises(ValidationError):
            DateRange(earliest="2027-03-02", latest="2027-03-01")

    def test_old_exact_date_format_is_rejected(self):
        with self.assertRaises(ValidationError):
            ApplicationWindow.model_validate(self.window("2027-03-01", "2027-03-02"))

    def test_same_source_order_uses_earliest_open_and_latest_deadline(self):
        ok_open = {"earliest": "2027-03-01", "latest": "2027-03-20"}
        ok_deadline = {"earliest": "2027-03-10", "latest": "2027-03-31"}
        # open.earliest <= deadline.latest passes even though ranges overlap.
        ApplicationWindow.model_validate(self.window(ok_open, ok_deadline))
        bad_deadline = {"earliest": "2027-02-01", "latest": "2027-02-28"}
        with self.assertRaises(ValidationError):
            ApplicationWindow.model_validate(self.window(ok_open, bad_deadline))
        # Different source URLs are not compared.
        ApplicationWindow.model_validate(
            self.window(ok_open, bad_deadline, deadline_url="u2")
        )


class ViewerFormatTests(unittest.TestCase):
    def fmt(self, e, l):
        return bv.fmt_date_range({"earliest": e, "latest": l})

    def test_date_range_forms(self):
        self.assertEqual(self.fmt("2026-11-15", "2026-11-15"), "26/11/15")
        self.assertEqual(self.fmt("2026-11-01", "2026-11-10"), "26/11上")
        self.assertEqual(self.fmt("2026-11-11", "2026-11-20"), "26/11中")
        self.assertEqual(self.fmt("2026-11-21", "2026-11-30"), "26/11下")
        self.assertEqual(self.fmt("2026-12-21", "2026-12-31"), "26/12下")
        self.assertEqual(self.fmt("2026-02-21", "2026-02-28"), "26/2下")
        self.assertEqual(self.fmt("2026-11-01", "2026-11-30"), "26/11")
        self.assertEqual(self.fmt("2026-11-05", "2026-11-20"), "26/11/5–11/20")
        self.assertEqual(self.fmt("2026-12-05", "2027-01-20"), "26/12/5–27/1/20")

    def test_industry_label_and_sort(self):
        f = {c[0]: c[2] for c in bv.PROFILE_COLUMNS}["industry_classification_codes"]
        self.assertEqual(f(["E_manufacturing", "G_information_communications"]), "製造/情報通信")
        p = {"company_id": "1", "basic_information": {"industry_classification_codes": {
            "records": [{"value": ["G_information_communications", "E_manufacturing"],
                         "source": src()}]}}}
        cells = dict(zip(bv.COLUMNS[bv.COL_INDEX["業種"]:], bv.profile_cells(p)))
        self.assertEqual(cells["業種"][:2], ["情報通信", "G_information_communications"])
        self.assertEqual(cells["業種"][3], "情報通信/製造")

    def test_first_window_date_is_minimum_by_earliest(self):
        def w(o):
            return {"source": src(), "application_open_date": metric(o)}
        slot = {"application_windows": {"records": [
            w({"earliest": "2026-11-11", "latest": "2026-11-20"}),
            w({"earliest": "2026-11-05", "latest": "2026-12-01"}),
        ]}}
        c = bv.window_date_cell([(slot, False)], "application_open_date")
        self.assertEqual(c[:2], ["26/11/5–12/1", "2026-11-05"])


class ViewerSkipTests(unittest.TestCase):
    def test_invalid_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            good = profile_payload(["E_manufacturing"])
            (d / "1.json").write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
            old = profile_payload()
            old["basic_information"]["industry_classification_codes"] = metric("E_manufacturing")
            (d / "2.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
            (d / "3.json").write_text("not json", encoding="utf-8")
            loaded, skipped = bv.load_dir(d, CompanyProfile)
            self.assertEqual([p["company_id"] for p in loaded], [good["company_id"]])
            self.assertEqual([f.name for f in skipped], ["2.json", "3.json"])

    def test_main_reports_skipped_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "companies").mkdir()
            (tmp / "recruitment_cases").mkdir()
            (tmp / "companies" / "x.json").write_text("{}", encoding="utf-8")
            out = tmp / "out.html"
            plain = bv.WORKSPACE_ROOT / "tmp" / "test_viewer_skip" / "index.html"
            self.addCleanup(shutil.rmtree, plain.parent, True)
            err = io.StringIO()
            with patch.object(bv, "DATA_DIR", tmp), patch.dict("os.environ", {bv.PASSWORD_ENV: "pw"}), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bv.main(["--output", str(out), "--plain-output", str(plain)]), 0)
            self.assertIn("x.json", err.getvalue())
            e = json.loads(out.read_text(encoding="utf-8").split('id="data" type="application/json">')[1].split("</script>")[0])
            b = base64.b64decode
            key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b(e["salt"]), iterations=e["iterations"]).derive(b"pw")
            self.assertEqual(json.loads(AESGCM(key).decrypt(b(e["iv"]), b(e["ct"]), None))["skipped"], 1)
            self.assertEqual(json.loads(plain.read_text(encoding="utf-8").split('id="data" type="application/json">')[1].split("</script>")[0])["skipped"], 1)


class OnlyInvalidTests(unittest.TestCase):
    def run_main(self, extra_args, valid_profile_ids=()):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            case_dir, company_dir = tmp / "cases", tmp / "companies"
            case_dir.mkdir()
            company_dir.mkdir()
            listed = tmp / "companies.json"
            listed.write_text(json.dumps({"companies": [
                {"company_id": "1000000000001", "legal_name": "A"}]}), encoding="utf-8")
            (case_dir / "1000000000001_2028.json").write_text(json.dumps({
                "publishing_company_id": "1000000000001",
                "application_slots": [{"assignment_companies": {
                    "company_ids": ["2000000000002", "3000000000003"]}}]}), encoding="utf-8")
            payload = profile_payload(["E_manufacturing"])
            for cid in ("2000000000002", "3000000000003"):
                body = dict(payload, company_id=cid) if cid in valid_profile_ids else {}
                (company_dir / f"{cid}.json").write_text(json.dumps(body), encoding="utf-8")
            calls = []

            def fake_run_jobs(jobs, **kwargs):
                calls.append(list(jobs))
                return {"failure_count": 0, "success_count": 0}

            err = io.StringIO()
            with (
                patch.object(survey_companies, "RECRUITMENT_CASE_DIR", case_dir),
                patch.object(survey_companies, "COMPANY_DIR", company_dir),
                patch.object(survey_companies, "DEFAULT_TMP_DIR", tmp / "run"),
                patch.object(survey_companies, "run_jobs", fake_run_jobs),
                patch.object(survey_companies, "registered_legal_names",
                             lambda ids: {i: f"N{i}" for i in ids}),
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()),
            ):
                survey_companies.main([str(listed), "2028", *extra_args])
            return calls, err.getvalue()

    def test_requires_revise(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_main(["--only-invalid"])

    def test_selects_only_invalid_outputs(self):
        calls, err = self.run_main(
            ["--revise", "--only-invalid"], valid_profile_ids=("3000000000003",)
        )
        case_jobs, profile_jobs = calls
        # The stored case is not a valid RecruitmentCase, so it is selected.
        self.assertEqual([j.company_id for j in case_jobs], ["1000000000001"])
        # Only the profile that fails validation is selected; the valid one is not.
        self.assertEqual([j.company_id for j in profile_jobs], ["2000000000002"])
        self.assertIn("1 recruitment case(s) selected", err)
        self.assertIn("1 company profile(s) selected", err)


class RevisePromptTests(unittest.TestCase):
    def test_revise_prompt_covers_old_format_conversion(self):
        text = (Path(survey_companies.WORKSPACE_ROOT) / "jobs/common/revise_prompt.md").read_text(encoding="utf-8")
        self.assertIn("古い schema", text)
        self.assertIn("DateRange", text)
        self.assertIn("検証を通", text)


if __name__ == "__main__":
    unittest.main()
