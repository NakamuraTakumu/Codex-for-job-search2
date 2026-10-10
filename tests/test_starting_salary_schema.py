import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schemas.recruitment_case_models import ApplicationSlot  # noqa: E402

SCHEMA = ApplicationSlot.model_json_schema()
SALARY_FIELDS = ("starting_salary", "starting_salary_master", "starting_salary_doctor")


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


class StartingSalarySchemaTest(unittest.TestCase):
    def test_class_description_is_emitted(self):
        d = SCHEMA["$defs"]["StartingSalary"]["description"]
        self.assertTrue(d.startswith("1つの応募枠・1つの学歴についてsourceが示す月額初任給。"))

    def test_estimate_prohibition_only_at_slot_level(self):
        hits = texts_with("推定値")
        self.assertEqual(sorted(h[1] for h in hits), ["x-do_not"] * 3)
        self.assertEqual(sorted(h[0].split("/")[-1] for h in hits), sorted(SALARY_FIELDS))
        self.assertEqual(texts_with("年収換算"), hits)

    def test_each_slot_rule_appears_once_per_degree_field_and_nowhere_else(self):
        for needle in ("形式化するのは", "最低額を記録し", "学歴別の額があれば",
                       "他の学歴には記録しない", "院卒なら修士・博士"):
            hits = texts_with(needle)
            self.assertEqual(sorted(h[0].split("/")[-1] for h in hits), sorted(SALARY_FIELDS), needle)
            self.assertTrue(all(h[1] == "description" for h in hits), needle)
            for field in SALARY_FIELDS:
                self.assertEqual(SCHEMA["properties"][field]["description"].count(needle), 1, needle)

    def test_total_vs_base_wording(self):
        props = SCHEMA["$defs"]["StartingSalary"]["properties"]
        self.assertIn("固定残業代と固定手当を含む", props["monthly_total_yen"]["description"])
        self.assertEqual(props["monthly_total_yen"]["description"].count("基本給しか示されていなければ null"), 1)
        self.assertNotIn("年収換算", json.dumps(props, ensure_ascii=False))
        self.assertIn("月給額の内訳", props["base_salary_yen"]["description"])
        self.assertIn("月給額に含まれる", props["fixed_overtime_yen"]["description"])


if __name__ == "__main__":
    unittest.main()
