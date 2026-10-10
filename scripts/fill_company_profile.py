#!/usr/bin/env python3
"""Run the company_profile_fill wrapper job and save one company JSON."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from jobs.common.codex_job import build_session_config
from jobs.common.output_paths import COMPANY_PROFILE_OUTPUT_PATH
from jobs.company_profile_fill import CompanyProfileFillJob


DEFAULT_TMP_DIR = WORKSPACE_ROOT / "tmp"
VENV_CANDIDATES = [
    WORKSPACE_ROOT / ".venv" / "bin" / "python3",
    WORKSPACE_ROOT / ".venv" / "bin" / "python",
    Path.home() / ".codex" / ".venv" / "bin" / "python3",
    Path.home() / ".codex" / ".venv" / "bin" / "python",
]


def bootstrap_venv() -> None:
    if importlib.util.find_spec("openai_codex") is not None:
        return
    current = Path(sys.executable)
    for candidate in VENV_CANDIDATES:
        if candidate.exists() and candidate != current:
            os.execv(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise RuntimeError(
        "openai_codex is not installed and no compatible virtual environment was found"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fill one company_profile JSON document with a Codex wrapper job."
    )
    parser.add_argument("company_id", help="13-digit corporate number used in <company_id>.json.")
    parser.add_argument("legal_name", help="Official legal company name.")
    parser.add_argument(
        "-o",
        "--output",
        dest="output_path",
        help="Optional output JSON path. Defaults to tmp/<timestamp>_<company_name>.json.",
    )
    parser.add_argument(
        "--model",
        help="Optional Codex model for wrapper jobs. Defaults to the job client's configured model.",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Optional reasoning effort for wrapper jobs. Defaults to the job client's configured effort.",
    )
    return parser


def build_default_output_path(legal_name: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_name = re.sub(r"[\\/:*?\"<>|\s]+", "_", legal_name).strip("_")
    if not safe_name:
        safe_name = "company"
    return DEFAULT_TMP_DIR / f"{timestamp}_{safe_name}.json"


def main(argv: list[str]) -> int:
    bootstrap_venv()
    parser = build_parser()
    args = parser.parse_args(argv)

    config = build_session_config(
        cwd=WORKSPACE_ROOT,
        **build_session_options(args),
    )
    response = CompanyProfileFillJob(
        config=config,
        company_id=args.company_id,
        legal_name=args.legal_name,
    ).run()

    try:
        collected_file = response.files[COMPANY_PROFILE_OUTPUT_PATH]
    except KeyError as exc:
        raise RuntimeError(
            f"wrapper job did not collect {COMPANY_PROFILE_OUTPUT_PATH}; "
            "check the company_profile_fill validator and job log"
        ) from exc
    parsed = json.loads(collected_file.text)
    rendered = json.dumps(parsed, ensure_ascii=False, indent=2) + "\n"

    output_path = Path(args.output_path).resolve() if args.output_path else build_default_output_path(args.legal_name)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    print(output_path)
    return 0


def build_session_options(args: argparse.Namespace) -> dict:
    options = {}
    model = getattr(args, "model", None)
    reasoning_effort = getattr(args, "reasoning_effort", None)
    if model:
        options["model"] = model
    if reasoning_effort:
        options["reasoning_effort"] = reasoning_effort
    return options


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
