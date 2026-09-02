# core/version.py
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


BOT_NAME = "HotBot"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEPLOYMENT_SHA_VARIABLES = (
    "RAILWAY_GIT_COMMIT_SHA",
    "GITHUB_SHA",
    "SOURCE_VERSION",
)


def _short_revision(value: str) -> str:
    cleaned = value.strip()
    return cleaned[:8] if cleaned else ""


def _environment_revision() -> str:
    for variable_name in _DEPLOYMENT_SHA_VARIABLES:
        revision = _short_revision(os.getenv(variable_name, ""))
        if revision:
            return revision
    return ""


def _git_revision() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _short_revision(completed.stdout)


def file_revision(path: str | Path) -> str:
    """Return a stable version generated from the file's actual contents."""
    file_path = Path(path)
    try:
        content = file_path.read_bytes()
    except OSError:
        return "unavailable"
    return hashlib.sha256(content).hexdigest()[:8]


def _source_revision() -> str:
    digest = hashlib.sha256()
    source_files = sorted(
        path
        for path in _REPO_ROOT.rglob("*.py")
        if ".git" not in path.parts
    )

    for source_file in source_files:
        try:
            relative_path = source_file.relative_to(_REPO_ROOT)
            digest.update(relative_path.as_posix().encode("utf-8"))
            digest.update(source_file.read_bytes())
        except OSError:
            continue

    return digest.hexdigest()[:8]


def get_bot_version() -> str:
    """Return the deployment revision without requiring a manual bump."""
    return _environment_revision() or _git_revision() or _source_revision()


BOT_VERSION = get_bot_version()
