import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schemas.recruitment_case_models import ApplicationWindows, RecruitmentCase  # noqa: E402

SCHEMA = RecruitmentCase.model_json_schema()
PARTIAL_DATE_RULE = (
    "開始日と締切日の一方だけを確認できる場合も、その募集回を記録し、"
    "確認できない側だけを Missing にする。"
)


def texts_with(needle):
    """schema 全体で needle を含む description / x-do_not の (場所, 種別) 一覧。"""
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("description", "x-do_not") and isinstance(v, str) and needle in v:
                    hits.append((path, k))
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}/{i}")

    walk(SCHEMA, "")
    return hits


class ApplicationWindowsSchemaTest(unittest.TestCase):
    def test_partial_date_rule_appears_once_at_records(self):
        self.assertEqual(
            texts_with(PARTIAL_DATE_RULE),
            [("/$defs/ApplicationWindows/properties/records", "description")],
        )
        self.assertEqual(json.dumps(SCHEMA, ensure_ascii=False).count(PARTIAL_DATE_RULE), 1)

    def test_empty_records_wording_refers_to_missing_window_not_dates(self):
        description = SCHEMA["$defs"]["ApplicationWindows"]["properties"]["records"]["description"]
        self.assertIn("募集回自体を確認できない（Missing）場合は空配列にする。", description)
        self.assertNotIn("募集期間を確認できない", description)

    def test_windows_accept_either_date_missing_and_name_only(self):
        source = {"kind": "official", "url": "https://example.com/recruit"}
        found = {
            "records": [
                {
                    "value": {"earliest": "2028-03-01", "latest": "2028-03-01"},
                    "source": source,
                    "retrieval": {"method": "web_tool", "crawled_label": None, "observed_on": None},
                }
            ]
        }
        missing = {"records": [], "note": "公開情報で確認できなかった。"}
        for open_found, deadline_found in ((True, False), (False, True), (False, False)):
            with self.subTest(open_found=open_found, deadline_found=deadline_found):
                windows = ApplicationWindows.model_validate({
                    "records": [{
                        "window_name": "第1回",
                        "source": source,
                        "application_open_date": found if open_found else missing,
                        "application_deadline_date": found if deadline_found else missing,
                    }],
                })
                self.assertEqual(len(windows.records), 1)
                window = windows.records[0]
                self.assertEqual(window.window_name, "第1回")
                self.assertEqual(bool(window.application_open_date.records), open_found)
                self.assertEqual(bool(window.application_deadline_date.records), deadline_found)

    def test_window_dates_share_one_definition_and_exclusions(self):
        window = SCHEMA["$defs"]["ApplicationWindow"]["properties"]
        open_date, deadline = window["application_open_date"], window["application_deadline_date"]
        # 開始日と締切日は同じ「最初に提出するもの」を基準にし、後の工程の期限は note に回す
        for field in (open_date, deadline):
            self.assertIn("応募で最初に提出するもの", field["description"])
            self.assertIn("プレエントリー", field["description"])
            self.assertIn("掲載期間", field["x-do_not"])
            self.assertIn("体験記", field["x-do_not"])
        self.assertIn("後の工程の日付はnoteに書く", deadline["description"])
        self.assertEqual(open_date["x-do_not"], deadline["x-do_not"])
        # 本選考でない募集の除外は、枠と募集回で同じ文言を使う
        records = SCHEMA["$defs"]["ApplicationWindows"]["properties"]["records"]
        slots = SCHEMA["properties"]["application_slots"]
        shared = "インターン、インターンシップ、仕事体験、オープンカンパニーなど、新卒本選考でない"
        self.assertIn(shared + "募集や、説明会、プレエントリーの日程を募集回にしない。", records["x-do_not"])
        self.assertIn(shared + "枠を含めない。", slots["x-do_not"])
        self.assertIn("その条件をnoteに書く", records["description"])


if __name__ == "__main__":
    unittest.main()
