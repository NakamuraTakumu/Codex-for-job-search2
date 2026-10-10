"""Run collected-JSON wrapper jobs on a bounded worker pool."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from jobs.common.codex_job import CodexWorkerPool


def run_jobs(
    jobs,
    *,
    output_key: str,
    output_path,
    failed_output_dir: Path,
    max_workers: int,
    warmup_jobs: int,
) -> dict:
    """Run jobs and save each collected output to ``output_path(job)``.

    Each job has ``company_id`` and ``legal_name``. A failed job leaves its
    collected output, if any, as ``<company_id>.failed.json`` in
    ``failed_output_dir`` so that canonical output paths hold only successes.
    """
    jobs = list(jobs)
    results_by_company_id = {}
    result_lock = threading.Lock()

    def on_task_completed(event):
        item = collect_response(
            event.response, event.job, output_key, output_path(event.job)
        )
        with result_lock:
            results_by_company_id[event.job.company_id] = item
        return []

    pool = CodexWorkerPool(
        max_workers=max_workers,
        warmup_jobs=warmup_jobs,
    )
    batch = pool.start_many(
        jobs,
        on_task_completed=on_task_completed,
        # The pool rejects max_tasks=0; an empty run finishes immediately.
        max_tasks=max(len(jobs), 1),
    )
    batch_error = None
    batch_result = None
    try:
        batch_result = batch.result()
    except Exception as exc:
        batch_error = {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "log_path": getattr(exc, "log_path", None),
        }
    if batch_result is not None:
        for job, error in zip(jobs, batch_result.errors, strict=True):
            if error is None:
                continue
            failed_output_path = collect_failed_output(
                error, job, output_key, failed_output_dir
            )
            results_by_company_id[job.company_id] = {
                "company_id": job.company_id,
                "legal_name": job.legal_name,
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "log_path": getattr(error, "log_path", None),
                "diagnostic_output_path": (
                    None if failed_output_path is None else str(failed_output_path)
                ),
            }

    results = []
    for job in jobs:
        item = results_by_company_id.get(job.company_id)
        if item is None:
            item = {
                "company_id": job.company_id,
                "legal_name": job.legal_name,
                "status": "batch_failed_or_cancelled",
            }
        results.append(item)

    return {
        "requested_count": len(jobs),
        "success_count": sum(item["status"] == "success" for item in results),
        "failure_count": sum(item["status"] != "success" for item in results),
        "max_workers": max_workers,
        "warmup_jobs": warmup_jobs,
        "batch_usage": batch_result.usage if batch_result is not None else None,
        "batch_usage_complete": (
            batch_result.usage_complete if batch_result is not None else False
        ),
        "batch_usage_incomplete_reasons": (
            list(batch_result.usage_incomplete_reasons)
            if batch_result is not None
            else ["batch did not return a result"]
        ),
        "batch_successful_usage": (
            batch_result.successful_usage if batch_result is not None else {}
        ),
        "batch_failed_usage": (
            batch_result.failed_usage if batch_result is not None else {}
        ),
        "batch_usage_by_model": (
            batch_result.usage_by_model if batch_result is not None else {}
        ),
        "batch_successful_usage_by_model": (
            batch_result.successful_usage_by_model
            if batch_result is not None
            else {}
        ),
        "batch_failed_usage_by_model": (
            batch_result.failed_usage_by_model
            if batch_result is not None
            else {}
        ),
        "batch_estimated_cost_usd": (
            batch_result.estimated_cost_usd if batch_result is not None else None
        ),
        "batch_estimated_cost_usd_by_model": (
            batch_result.estimated_cost_usd_by_model
            if batch_result is not None
            else None
        ),
        "batch_error": batch_error,
        "results": results,
    }


def collect_response(response, job, output_key: str, output_path: Path) -> dict:
    parsed = json.loads(response.files[output_key].text)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "company_id": job.company_id,
        "legal_name": job.legal_name,
        "status": "success",
        "output_path": str(output_path),
        "log_path": response.log_path,
        "usage": response.usage,
        "estimated_cost_usd": response.estimated_cost_usd,
    }


def collect_failed_output(error, job, output_key: str, output_dir: Path) -> Path | None:
    files = getattr(error, "files", None) or {}
    collected = files.get(output_key)
    if collected is None:
        response = getattr(error, "response", None)
        files = {} if response is None else getattr(response, "files", None) or {}
        collected = files.get(output_key)
    if collected is None:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{job.company_id}.failed.json"
    output_path.write_text(collected.text, encoding="utf-8")
    return output_path
