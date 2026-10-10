import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHORT_NAMES = ROOT / "data" / "slot_short_names.json"
CASES = ROOT / "data" / "recruitment_cases"


class SlotShortNamesTest(unittest.TestCase):
    """表示用の短い応募枠名（data/slot_short_names.json）が、現在の募集データと整合していること。"""

    def setUp(self):
        self.mapping = json.loads(SHORT_NAMES.read_text(encoding="utf-8"))
        self.cases = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("*.json"))}

    def test_no_stale_keys(self):
        for stem, names in self.mapping.items():
            self.assertIn(stem, self.cases, f"{stem}: 対応する募集ファイルがない")
            slot_names = {s["slot_name"] for s in self.cases[stem].get("application_slots") or []}
            for name in names:
                self.assertIn(name, slot_names, f"{stem}: 存在しない枠名 {name!r}")

    def test_every_slot_has_short_name(self):
        for stem, case in self.cases.items():
            for s in case.get("application_slots") or []:
                self.assertIn(s["slot_name"], self.mapping.get(stem, {}), f"{stem}: 短縮名なし {s['slot_name']!r}")

    def test_short_names_unique_within_publisher_and_year(self):
        for stem, case in self.cases.items():
            names = self.mapping.get(stem, {})
            by_year: dict = {}
            for s in case.get("application_slots") or []:
                by_year.setdefault(s["application_year"], set()).add(s["slot_name"])
            for year, slot_names in by_year.items():
                shorts = [names[n] for n in slot_names if n in names]
                self.assertEqual(len(shorts), len(set(shorts)), f"{stem} {year}: 短縮名が重複 {sorted(shorts)}")

    def test_sorted_and_nonempty(self):
        self.assertEqual(list(self.mapping), sorted(self.mapping))
        for names in self.mapping.values():
            self.assertEqual(list(names), sorted(names))
            self.assertTrue(all(v.strip() for v in names.values()))


if __name__ == "__main__":
    unittest.main()
