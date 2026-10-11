#!/usr/bin/env python3
"""One-off legacy date provenance migration; no survey/network calls."""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from schemas.recruitment_case_models import RecruitmentCase

LEGACY_RETRIEVAL = {"method": "web_tool", "crawled_label": None, "observed_on": None}


def migrate_document(document: dict) -> int:
    """Add only missing date-record retrieval; return changed record count."""
    count = 0
    for slot in document.get("application_slots", []):
        for window in (slot.get("application_windows") or {}).get("records", []):
            for field in ("application_open_date", "application_deadline_date"):
                for record in (window.get(field) or {}).get("records", []):
                    if "retrieval" not in record:
                        record["retrieval"] = LEGACY_RETRIEVAL.copy()
                        count += 1
    return count


def migrate_files(root: Path) -> dict[str, int]:
    # Validate all candidates before writing any, and compare against a version
    # with only the newly added keys removed (no unrelated field mutation).
    pending = []
    total_records = 0
    validated = 0
    for path in sorted(root.rglob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not {"publishing_company_id", "application_slots"} <= document.keys():
            continue
        before = copy.deepcopy(document)
        count = migrate_document(document)
        RecruitmentCase.model_validate(document)
        validated += 1
        expected = copy.deepcopy(before)
        migrate_document(expected)
        assert expected == document, f"unrelated data changed: {path}"
        if count:
            pending.append((path, document))
            total_records += count
    for path, document in pending:
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        RecruitmentCase.model_validate_json(path.read_text(encoding="utf-8"))
    return {"files_changed": len(pending), "records_changed": total_records, "files_validated": validated}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    print(json.dumps(migrate_files(args.root), ensure_ascii=False))


if __name__ == "__main__":
    main()
