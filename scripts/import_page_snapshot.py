#!/usr/bin/env python3
"""Import an HTML/PDF/text page saved manually in a browser; no network calls."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from jobs.common.page_fetch import import_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("file", type=Path)
    parser.add_argument("--fetched-at", help="UTC ISO8601 time of manual acquisition; default: import time")
    args = parser.parse_args()
    try:
        result = import_snapshot(args.url, args.file, args.fetched_at)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
