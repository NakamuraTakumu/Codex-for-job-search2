"""Common helpers for wrapper job validators."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def validate_stdin_json(raw_input: str, model_class):
    try:
        output_json = json.loads(raw_input)
    except Exception as exc:
        return None, None, f"output is not valid JSON: {exc}"

    try:
        model = model_class.model_validate_json(raw_input)
    except ValidationError as exc:
        return output_json, None, f"schema validation failed: {exc}"

    return output_json, model, None


def find_key_paths(value, forbidden_key: str, path: str = "$") -> list[str]:
    if isinstance(value, dict):
        paths = []
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == forbidden_key:
                paths.append(child_path)
            paths.extend(find_key_paths(child, forbidden_key, child_path))
        return paths
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(find_key_paths(child, forbidden_key, f"{path}[{index}]"))
        return paths
    return []


def validate_no_searched_sources(output_json) -> str | None:
    paths = find_key_paths(output_json, "searched_sources")
    if paths:
        return "field-level searched_sources is not allowed: " + ", ".join(paths)
    return None


def load_collected_output_file(
    *,
    model_class,
    collected_output_path,
    subcwd=None,
):
    subcwd = subcwd or os.environ.get("CODEX_WRAPPER_SUBCWD")
    if not subcwd:
        return None, None, "CODEX_WRAPPER_SUBCWD is required"

    output_path = Path(subcwd) / collected_output_path
    if not output_path.is_file():
        return None, None, (
            "collected output file is missing: "
            f"write the final JSON to ${{SUBCWD}}/{collected_output_path}"
        )
    try:
        collected_raw = output_path.read_text(encoding="utf-8")
        collected_json = json.loads(collected_raw)
    except Exception as exc:
        return None, None, f"collected output file is not valid JSON: {exc}"
    searched_sources_error = validate_no_searched_sources(collected_json)
    if searched_sources_error:
        return collected_json, None, searched_sources_error
    try:
        model = model_class.model_validate_json(collected_raw)
    except ValidationError as exc:
        return collected_json, None, f"collected output file schema validation failed: {exc}"
    return collected_json, model, None


def validate_collected_file(
    *,
    model_class,
    collected_output_path,
    subcwd=None,
):
    output_json, model, error = load_collected_output_file(
        model_class=model_class,
        collected_output_path=collected_output_path,
        subcwd=subcwd,
    )
    if error:
        return None, None, fail(error)

    return output_json, model, 0
