"""Codex wrapper job for probing read-only corporate-number lookup access."""

import sys
from pathlib import Path


WRAPPER_ROOT = Path.home() / ".codex" / "skills" / "codex-job-runner"
if WRAPPER_ROOT.exists() and str(WRAPPER_ROOT) not in sys.path:
    sys.path.insert(0, str(WRAPPER_ROOT))

try:
    from codex_job_runner import CodexJob
except ModuleNotFoundError as exc:
    if exc.name == "codex_job_runner":
        raise ModuleNotFoundError(
            "codex_job_runner not found. Install it or place it at "
            f"{WRAPPER_ROOT}."
        ) from exc
    raise


HERE = Path(__file__).resolve().parent
WORKSPACE_ROOT = HERE.parents[1]
PROMPT_PATH = HERE / "prompt.md"
LOOKUP_SCRIPT_PATH = WORKSPACE_ROOT / "scripts" / "corporate_number_lookup.py"
RESOLVER_LIB_PATH = WORKSPACE_ROOT / "scripts" / "corporate_number_resolver_lib.py"
DEFAULT_DB_PATH = WORKSPACE_ROOT / "data" / "cache" / "corporate_number" / "corporate_numbers.sqlite3"


class CorporateNumberReadonlyProbeJob(CodexJob):
    """Probe whether the lookup script can read the SQLite DB with read-only paths."""

    def __init__(
        self,
        *,
        config,
        corporate_number="1010001128061",
        db_path=None,
        read_paths=None,
    ):
        self.corporate_number = str(corporate_number)
        db_path = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
        self.db_path = db_path
        prompt = _build_input(
            corporate_number=self.corporate_number,
            db_path=str(db_path),
        )

        job_read_paths = [
            *config.workspace.read_paths,
            LOOKUP_SCRIPT_PATH,
            RESOLVER_LIB_PATH,
            db_path,
        ]
        if read_paths:
            job_read_paths.extend(read_paths)
        job_config = config.with_overrides(
            workspace=config.workspace.with_overrides(
                read_paths=tuple(dict.fromkeys(job_read_paths)),
                write_paths=(),
            ),
        )
        super().__init__(
            config=job_config,
            input=prompt,
        )


def _build_input(*, corporate_number: str, db_path: str) -> str:
    prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    sections = [
        prompt,
        "Target corporate_number:\n" + corporate_number,
        "Target db_path:\n" + db_path,
    ]
    return "\n\n".join(sections)
