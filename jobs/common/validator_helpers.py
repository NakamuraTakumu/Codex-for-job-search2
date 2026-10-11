"""Common helpers for wrapper job validators."""

from __future__ import annotations

import json
import os
from datetime import datetime
import sys
from pathlib import Path

from pydantic import ValidationError

from schemas.common_models import source_url_identity


def validate_date_retrieval(recruitment_case, *, fetched_urls=(), log_path=None) -> None:
    """Host-owned logs are authoritative; job-local fresh URLs exclude cache data."""
    from jobs.common.page_fetch import FETCH_ROOT, read_log

    log_path = Path(log_path) if log_path is not None else FETCH_ROOT / "log.jsonl"
    try:
        entries = read_log(log_path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot verify host-fetch log {log_path}: {exc}") from exc
    failure_path = log_path.with_name("failures.jsonl")
    failed_urls = None
    fresh = {source_url_identity(url) for url in fetched_urls}
    for si, slot in enumerate(recruitment_case.application_slots):
        for wi, window in enumerate(slot.application_windows.records):
            for field in ("application_open_date", "application_deadline_date"):
                metric = getattr(window, field)
                evidence = [(f"records[{ri}]", record) for ri, record in enumerate(metric.records)]
                if metric.announced_later is not None:
                    evidence.append(("announced_later", metric.announced_later))
                for suffix, record in evidence:
                    path = f"application_slots[{si}].application_windows.records[{wi}].{field}.{suffix}"
                    retrieval = record.retrieval
                    url = record.source.url
                    if retrieval.method == "web_tool":
                        if source_url_identity(url) in fresh:
                            raise ValueError(f"{path}: web_tool forbidden for {url}: fetch_page succeeded in this job. "
                                             "Inspect its raw_path (text_path as an aid) and use host_fetch fetched_at/snapshot_id; remove cached values (or mark Missing).")
                        if failed_urls is None:
                            try:
                                failures = read_log(failure_path)
                            except (OSError, ValueError) as exc:
                                raise ValueError(f"cannot verify host-fetch failure log {failure_path}: {exc}") from exc
                            failed_urls = set()
                            for failure in failures:
                                for key in ("requested_url", "final_url"):
                                    try:
                                        failed_url = failure.get(key)
                                        if isinstance(failed_url, str) and failed_url:
                                            failed_urls.add(source_url_identity(failed_url))
                                    except (TypeError, ValueError):
                                        continue
                        if source_url_identity(url) not in failed_urls:
                            raise ValueError(f"{path}: web_tool has no matching host-fetch failure for {url} in "
                                             f"{failure_path} (requested_url or final_url). "
                                             "Call fetch_page and use its provenance; use web_tool only after a recorded failure.")
                        continue
                    matches = []
                    for entry in entries:
                        try:
                            timestamp = datetime.fromisoformat(entry["fetched_at"])
                            status = entry["status"]
                            matches.append(entry["snapshot_id"] == retrieval.snapshot_id
                                           and timestamp == retrieval.fetched_at
                                           and entry.get("origin", "host") == retrieval.origin
                                           and source_url_identity(url) in {
                                               source_url_identity(entry["requested_url"]),
                                               source_url_identity(entry["final_url"]),
                                           }
                                           and 200 <= status < 300)
                        except (KeyError, TypeError, ValueError):
                            continue
                    if not any(matches):
                        raise ValueError(f"{path}: host_fetch has no matching successful host log entry for URL, "
                                         "snapshot_id, fetched_at and origin. Call fetch_page and copy the returned provenance "
                                         "and requested_url/final_url exactly; never invent host_fetch metadata.")


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
