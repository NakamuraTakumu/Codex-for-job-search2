#!/usr/bin/env python3
"""Run the fixed-artifact reviewer model/effort experiment."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import random
import shutil
import sys
import threading
import time
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
RUNNER_ROOT = Path.home() / ".codex" / "skills" / "codex-job-runner"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from codex_job_runner import (  # noqa: E402
    CodexJob,
    ReviewConfig,
    SessionConfig,
    SubagentConfig,
    ValidationConfig,
    WorkspaceConfig,
)
from jobs.common.codex_job import (  # noqa: E402
    build_recruitment_input,
    build_review_instructions,
)
from jobs.recruitment_case_fill.client import (  # noqa: E402
    MODEL_PATH,
    PROMPT_PATH,
    QUALITY_CONTRACT_PATH,
)
from jobs.recruitment_case_fill.validate_output import (  # noqa: E402
    validate_recruitment_case_output,
)


CONDITIONS = {
    "S-M": ("gpt-5.6-sol", "medium"),
    "T-H": ("gpt-5.6-terra", "high"),
    "L-XH": ("gpt-5.6-luna", "xhigh"),
}
COMPANY_ID = "7130001011258"
LEGAL_NAME = "TOWA株式会社"
RECRUITMENT_YEAR = 2028
OUTPUT_NAME = "recruitment_case.json"
DEFAULT_SOURCE = (
    WORKSPACE_ROOT
    / "tmp"
    / "recruitment_case_towa_peer_exchange_20260729"
    / f"{COMPANY_ID}.json"
)
DEFAULT_OUTPUT_ROOT = (
    WORKSPACE_ROOT / "tmp" / "reviewer_model_effort_experiment_20260729"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixed_context() -> tuple[str, ...]:
    return tuple(
        str(path)
        for path in (
            WORKSPACE_ROOT / "jobs" / "common" / "research_prompt.md",
            QUALITY_CONTRACT_PATH,
            PROMPT_PATH,
            WORKSPACE_ROOT / "schemas" / "common_models.py",
            MODEL_PATH,
        )
    )


def _main_config(work_dir: Path) -> SessionConfig:
    return SessionConfig(
        context=_fixed_context(),
        model="gpt-5.6-luna",
        reasoning_effort="xhigh",
        web_search="live",
        timeout=3600,
        ignore_global_context=True,
        allowed_skills=(),
        allow_shell_commands=True,
        workspace=WorkspaceConfig(
            cwd=WORKSPACE_ROOT,
            read_paths=(str(work_dir),),
            write_paths=(str(work_dir),),
        ),
    )


def _review_config(work_dir: Path, model: str, effort: str) -> ReviewConfig:
    session = SessionConfig(
        context=(),
        model=model,
        reasoning_effort=effort,
        web_search="live",
        timeout=3600,
        ignore_global_context=True,
        allowed_skills=(),
        allow_shell_commands=True,
        workspace=WorkspaceConfig(
            cwd=WORKSPACE_ROOT,
            read_paths=(str(work_dir),),
            write_paths=(),
        ),
    )
    return ReviewConfig(
        context=build_review_instructions(
            OUTPUT_NAME,
            MODEL_PATH,
            shared_context=(QUALITY_CONTRACT_PATH,),
        ),
        session=session,
        reuse_session=True,
        enable_peer_exchange=True,
        max_repairs=5,
        subagents=_research_subagents(session),
    )


def _research_subagents(parent: SessionConfig) -> SubagentConfig:
    return SubagentConfig(
        session=parent.with_overrides(
            context=(),
            model="gpt-5.6-luna",
            reasoning_effort="medium",
            tools=None,
            workspace=parent.workspace.with_overrides(write_paths=()),
        ),
        allowed_models={"gpt-5.6-luna": ("medium",)},
        max_concurrent=3,
    )


def _original_input(artifact_path: Path) -> str:
    prompt = build_recruitment_input(
        company_id=COMPANY_ID,
        legal_name=LEGAL_NAME,
        recruitment_year=RECRUITMENT_YEAR,
        collected_output_path=OUTPUT_NAME,
    )
    return prompt.replace(f"${{SUBCWD}}/{OUTPUT_NAME}", str(artifact_path))


class ForkedReviewExperimentJob(CodexJob):
    def __init__(
        self,
        *,
        config: SessionConfig,
        review: ReviewConfig,
        artifact_path: Path,
        expected_initial_hash: str,
        original_input: str,
        subagents: SubagentConfig,
    ) -> None:
        self.artifact_path = artifact_path
        self.expected_initial_hash = expected_initial_hash
        self._initial_review_checked = False
        continuation = (
            original_input
            + "\n\nExperiment checkpoint:\n"
            + "The canonical artifact already contains the completed main result. "
            + "Do not edit it before the initial automatic review. "
            + "Return a brief completion response so the normal review cycle starts."
        )
        super().__init__(
            config=config,
            input=continuation,
            review=review,
            validation=ValidationConfig(repair=True, max_repairs=5),
            subagents=subagents,
            max_total_repairs=3,
        )

    def validate_output(self, response):
        del response
        validate_recruitment_case_output(
            subcwd=self.artifact_path.parent,
            expected_company_id=COMPANY_ID,
            expected_recruitment_year=RECRUITMENT_YEAR,
        )

    def before_review(self):
        if self._initial_review_checked:
            return
        actual = _sha256(self.artifact_path.read_bytes())
        if actual != self.expected_initial_hash:
            raise RuntimeError(
                "initial artifact changed before review: "
                f"expected={self.expected_initial_hash} actual={actual}"
            )
        self._initial_review_checked = True


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_experiment(
    *,
    source: Path,
    output_root: Path,
    repeats: int,
    seed: int,
    max_parallel: int,
):
    output_root.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = output_root / "checkpoint"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = checkpoint_dir / OUTPUT_NAME
    source_bytes = source.read_bytes()
    artifact_path.write_bytes(source_bytes)
    fixed_hash = _sha256(source_bytes)
    original_input = _original_input(artifact_path)
    checkpoint_config = _main_config(checkpoint_dir)

    # Establish one completed main conversation without changing the fixed artifact.
    from codex_job_runner import CodexSession

    checkpoint = CodexSession(
        config=checkpoint_config,
        input=(
            original_input
            + "\n\nCheckpoint preparation:\n"
            + "Read and understand the existing canonical artifact. "
            + "Do not modify it. Return a brief completion response."
        ),
    )
    checkpoint_response = checkpoint.run()
    if _sha256(artifact_path.read_bytes()) != fixed_hash:
        raise RuntimeError("checkpoint preparation changed the fixed artifact")

    order = [
        condition
        for _round in range(repeats)
        for condition in CONDITIONS
    ]
    random.Random(seed).shuffle(order)
    manifest = {
        "source": str(source),
        "artifact_sha256": fixed_hash,
        "seed": seed,
        "repeats": repeats,
        "max_parallel": max_parallel,
        "order": order,
        "checkpoint_usage": checkpoint_response.usage,
        "runs": [],
    }
    _write_json(output_root / "manifest.json", manifest)

    manifest_lock = threading.Lock()

    def run_one(index_condition):
            index, condition = index_condition
            run_id = f"{index:02d}_{condition.lower().replace('-', '_')}"
            run_dir = output_root / "runs" / run_id
            work_dir = run_dir / "work"
            work_dir.mkdir(parents=True, exist_ok=True)
            run_artifact_path = work_dir / OUTPUT_NAME
            run_artifact_path.write_bytes(source_bytes)
            model, effort = CONDITIONS[condition]
            main_config = _main_config(work_dir)
            run_original_input = _original_input(run_artifact_path)
            job = ForkedReviewExperimentJob(
                config=main_config,
                review=_review_config(work_dir, model, effort),
                artifact_path=run_artifact_path,
                expected_initial_hash=fixed_hash,
                original_input=run_original_input,
                subagents=_research_subagents(main_config),
            )
            job._conversation_state = checkpoint._conversation_state.fork()
            started = time.monotonic()
            status = "completed"
            error = None
            try:
                response = job.run()
            except Exception as exc:
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
                response = job.response
            elapsed = time.monotonic() - started
            shutil.copy2(run_artifact_path, run_dir / OUTPUT_NAME)
            record = {
                "run_id": run_id,
                "condition": condition,
                "model": model,
                "reasoning_effort": effort,
                "status": status,
                "error": error,
                "elapsed_seconds": elapsed,
                "initial_artifact_sha256": fixed_hash,
                "final_artifact_sha256": _sha256(run_artifact_path.read_bytes()),
                "review_passed": job.review_passed,
                "review_summary": job.review_summary,
                "review_findings": list(job.review_findings),
                "attempts": job.current_attempt + 1,
                "repairs": job.repair_index,
                "usage": None if response is None else response.usage,
                "estimated_cost_usd": (
                    None if response is None else response.estimated_cost_usd
                ),
                "log_path": job.log_path,
            }
            _write_json(run_dir / "result.json", record)
            with manifest_lock:
                manifest["runs"].append(record)
                manifest["runs"].sort(key=lambda item: item["run_id"])
                _write_json(output_root / "manifest.json", manifest)
            print(
                f"[Experiment] {run_id} status={status} "
                f"review_passed={job.review_passed} elapsed={elapsed:.1f}s "
                f"log={job.log_path}",
                flush=True,
            )
            return record

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_parallel,
        ) as executor:
            futures = [
                executor.submit(run_one, item)
                for item in enumerate(order, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()
    finally:
        checkpoint.close()
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--max-parallel", type=int, default=3)
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.max_parallel < 1:
        parser.error("--max-parallel must be positive")
    run_experiment(
        source=args.source.resolve(),
        output_root=args.output_root.resolve(),
        repeats=args.repeats,
        seed=args.seed,
        max_parallel=args.max_parallel,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
