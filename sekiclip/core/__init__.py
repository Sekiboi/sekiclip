"""Core domain: models, prefs, jobs, session store, tools, updates, diagnostics."""

from sekiclip.core.diagnostics import build_report, crash_log_path
from sekiclip.core.jobs import JobResult, job_log_path, log_job, run_job
from sekiclip.core.models import ExportJob, JobErrorCode, Look, classify_error
from sekiclip.core.prefs import (
    is_portable_mode,
    load_prefs,
    save_prefs,
    user_data_dir,
)
from sekiclip.core.session_store import (
    EDIT_ACTION_LABELS,
    EDIT_KEY_TO_LABEL,
    EDIT_LABEL_TO_KEY,
    build_session_dict,
    load_session_file,
    save_session_file,
)
from sekiclip.core.tools_registry import TOOL_NAMES, get_tool, load_dev_flags_from_env
from sekiclip.core.updates import UpdateResult, check_for_update, is_newer

__all__ = [
    "ExportJob",
    "JobErrorCode",
    "JobResult",
    "Look",
    "TOOL_NAMES",
    "UpdateResult",
    "build_report",
    "build_session_dict",
    "check_for_update",
    "classify_error",
    "crash_log_path",
    "get_tool",
    "is_newer",
    "is_portable_mode",
    "job_log_path",
    "load_dev_flags_from_env",
    "load_prefs",
    "load_session_file",
    "log_job",
    "run_job",
    "save_prefs",
    "save_session_file",
    "user_data_dir",
    "EDIT_ACTION_LABELS",
    "EDIT_KEY_TO_LABEL",
    "EDIT_LABEL_TO_KEY",
]
