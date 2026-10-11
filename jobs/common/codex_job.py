"""Common helpers for Codex wrapper jobs."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path


WRAPPER_ROOT = Path.home() / ".codex" / "skills" / "codex-job-runner"
if WRAPPER_ROOT.exists() and str(WRAPPER_ROOT) not in sys.path:
    sys.path.insert(0, str(WRAPPER_ROOT))

try:
    from codex_job_runner import (
        CodexJob,
        CodexWorkerPool,
        FileCollectionConfig,
        ReviewConfig,
        SessionConfig,
        SubagentConfig,
        ValidationConfig,
        WorkspaceConfig,
    )
except ModuleNotFoundError as exc:
    if exc.name == "codex_job_runner":
        raise ModuleNotFoundError(
            "codex_job_runner not found. Install it or place it at "
            f"{WRAPPER_ROOT}."
        ) from exc
    raise


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
COMMON_PROMPT_PATH = WORKSPACE_ROOT / "jobs" / "common" / "research_prompt.md"
COMMON_REVIEW_PROMPT_PATH = WORKSPACE_ROOT / "jobs" / "common" / "review_prompt.md"
COMMON_REPAIR_PROMPT_PATH = WORKSPACE_ROOT / "jobs" / "common" / "repair_prompt.md"
COMMON_MODELS_PATH = WORKSPACE_ROOT / "schemas" / "common_models.py"
# Bounds review loops that keep raising new findings, which the runner cannot detect.
DEFAULT_JOB_TIMEOUT_SECONDS = 1800

def build_session_config(
    *,
    cwd=WORKSPACE_ROOT,
    context=(),
    model="gpt-6-luna",
    reasoning_effort="high",
    codex_bin=None,
    timeout=DEFAULT_JOB_TIMEOUT_SECONDS,
):
    """Build the default isolated session config used by this workspace."""
    return SessionConfig(
        context=context,
        model=model,
        reasoning_effort=reasoning_effort,
        web_search="live",
        codex_bin=codex_bin,
        timeout=timeout,
        ignore_global_context=True,
        allowed_skills=(),
        allow_shell_commands=True,
        workspace=WorkspaceConfig(
            cwd=cwd,
            subcwd_root=WORKSPACE_ROOT / "tmp" / "codex-job-runner",
        ),
    )


def build_research_subagent_config(parent_config):
    """Build the bounded Luna-high child policy for one parent job."""
    workspace = parent_config.workspace
    child_session = parent_config.with_overrides(
        context=(),
        model="gpt-6-luna",
        reasoning_effort="high",
        tools=None,
        workspace=workspace.with_overrides(
            write_paths=(),
        ),
    )
    return SubagentConfig(
        session=child_session,
        allowed_models={"gpt-6-luna": ("high",)},
        max_concurrent=3,
    )


class CollectedJsonCodexJob(CodexJob):
    """Base job for wrapper jobs that collect one JSON file from SUBCWD."""

    def __init__(
        self,
        *,
        config,
        fixed_context,
        prompt,
        collected_output_path,
        domain_schema_path,
        review_shared_context=(),
        review_read_paths=(),
        review_extra_context=(),
        read_paths=None,
        write_paths=None,
        review=None,
    ):
        self._collected_output_path = Path(collected_output_path)
        caller_context = tuple(config.context)
        merged_context = tuple(
            dict.fromkeys(
                str(path) for path in (*fixed_context, *caller_context)
            )
        )
        workspace = config.workspace
        merged_read_paths = tuple(
            dict.fromkeys(
                str(path)
                for path in (*workspace.read_paths, *(read_paths or ()))
            )
        )
        merged_write_paths = tuple(
            dict.fromkeys(
                str(path)
                for path in (
                    *workspace.write_paths,
                    "${SUBCWD}/.",
                    *(write_paths or ()),
                )
            )
        )
        job_config = config.with_overrides(
            context=merged_context,
            workspace=workspace.with_overrides(
                read_paths=merged_read_paths,
                write_paths=merged_write_paths,
            ),
        )

        review_session = config.with_overrides(
            context=(),
            model="gpt-6-luna",
            reasoning_effort="high",
            allow_shell_commands=True,
            tools=None,
            workspace=workspace.with_overrides(
                read_paths=("${SUBCWD}/.", *map(str, review_read_paths)),
                write_paths=(),
            ),
        )
        canonical_artifact_path = "${SUBCWD}/" + self._collected_output_path.as_posix()
        if review is None:
            review = ReviewConfig(
                context=build_review_instructions(
                    collected_output_path,
                    domain_schema_path,
                    shared_context=review_shared_context,
                ) + tuple(review_extra_context),
                artifact_paths=(canonical_artifact_path,),
                session=review_session,
                reuse_session=True,
                enable_peer_exchange=True,
                # Non-convergence is detected by the runner; job timeout bounds the rest.
                max_repairs=None,
                subagents=build_research_subagent_config(review_session),
            )
        elif canonical_artifact_path not in review.artifact_paths:
            review = replace(
                review,
                artifact_paths=(*review.artifact_paths, canonical_artifact_path),
            )
        if review_read_paths or review_extra_context:
            # Respect a caller-supplied reviewer, adding the same host evidence
            # access/contract that the default reviewer receives.
            session = review.session or review_session
            session = session.with_overrides(workspace=session.workspace.with_overrides(
                read_paths=tuple(dict.fromkeys((*session.workspace.read_paths, *map(str, review_read_paths)))),
            ))
            children = review.subagents
            if children is not None and children.session is not None:
                children = replace(children, session=children.session.with_overrides(
                    workspace=children.session.workspace.with_overrides(
                        read_paths=tuple(dict.fromkeys((*children.session.workspace.read_paths, *map(str, review_read_paths)))),
                    ),
                ))
            review = replace(review, session=session, subagents=children,
                             context=tuple(dict.fromkeys((*review.context, *review_extra_context))))
        super().__init__(
            config=job_config,
            input=prompt,
            validation=ValidationConfig(
                repair=True,
                repair_prompt_path=COMMON_REPAIR_PROMPT_PATH,
            ),
            review=review,
            files=FileCollectionConfig(enabled=True),
            subagents=build_research_subagent_config(job_config),
        )


def build_recruitment_input(
    *,
    company_id,
    legal_name,
    recruitment_year,
    collected_output_path,
    extra_sections=None,
    extra_input=None,
):
    sections = [
        "Target company_id:\n" + str(company_id),
        "Target legal_name:\n" + str(legal_name),
        "Target recruitment_year:\n" + str(recruitment_year),
    ]
    sections.extend(extra_sections or [])
    sections.append("Collected output path:\n" + f"${{SUBCWD}}/{collected_output_path}")
    if extra_input:
        sections.append("Additional instructions:\n" + extra_input)
    return "\n\n".join(sections)


def build_review_instructions(
    collected_output_path,
    domain_schema_path,
    *,
    shared_context=(),
):
    del collected_output_path  # The exact canonical path is already in original_input.
    schema_paths = dict.fromkeys((COMMON_MODELS_PATH, Path(domain_schema_path)))
    shared_paths = dict.fromkeys(
        Path(path) for path in (COMMON_PROMPT_PATH, *shared_context)
    )
    return (
        "Fixed review contract:\n"
        + COMMON_REVIEW_PROMPT_PATH.read_text(encoding="utf-8").strip(),
        *(
            f"Shared contract ({path.relative_to(WORKSPACE_ROOT)}):\n"
            + path.read_text(encoding="utf-8").strip()
            for path in shared_paths
        ),
        *(
            f"Canonical schema/model ({path.relative_to(WORKSPACE_ROOT)}):\n"
            + path.read_text(encoding="utf-8").strip()
            for path in schema_paths
        ),
    )
