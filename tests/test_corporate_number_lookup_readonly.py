import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOOKUP = ROOT / "scripts" / "corporate_number_lookup.py"


class CorporateNumberLookupReadonlyTests(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(LOOKUP), *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_stats_works_with_readonly_db_access(self):
        proc = self._run("stats")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertGreater(payload["row_count"], 0)

    def test_lookup_works_with_readonly_db_access(self):
        proc = self._run("lookup", "1010001128061")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["legal_name"], "日本アイ・ビー・エム株式会社")


if __name__ == "__main__":
    unittest.main()
