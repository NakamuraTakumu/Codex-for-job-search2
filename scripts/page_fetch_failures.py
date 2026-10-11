#!/usr/bin/env python3
"""List latest unresolved fetch failures for manual browser acquisition."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from jobs.common.page_fetch import FETCH_ROOT, unresolved_failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=FETCH_ROOT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--include-rejected", action="store_true",
                        help="Include unsafe URL/SSRF rejections (not browser-fetch candidates)")
    args = parser.parse_args()
    entries = [e for e in unresolved_failures(args.root)
               if args.include_rejected or e.get("category") != "url_rejection"]
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    else:
        for entry in entries:
            print("\t".join(str(entry.get(k) or "") for k in
                            ("attempted_at", "requested_url", "reason", "company_id")))


if __name__ == "__main__":
    main()
