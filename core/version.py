# core/version.py
from __future__ import annotations

import ast
from pathlib import Path


BOT_NAME = "HotBot"
MASTER_VERSION = 1
DEFAULT_COMPONENT_VERSION = "1.0.0"
__version__ = "1.0.0"

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _component_paths() -> list[Path]:
    paths: list[Path] = []

    hotbot_path = _REPO_ROOT / "hotbot.py"
    if hotbot_path.is_file():
        paths.append(hotbot_path)

    for folder_name in ("cogs", "core"):
        folder = _REPO_ROOT / folder_name
        if not folder.is_dir():
            continue
        paths.extend(
            path
            for path in folder.rglob("*.py")
            if path.name != "__init__.py"
            and not any(part.startswith("_") for part in path.parts)
        )

    return sorted(set(paths))


def _explicit_version(path: Path) -> str | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return None

    for node in tree.body:
        value_node: ast.expr | None = None

        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            ):
                value_node = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__version__"
        ):
            value_node = node.value

        if (
            isinstance(value_node, ast.Constant)
            and isinstance(value_node.value, str)
        ):
            return value_node.value.strip()

    return None


def _version_digits(version: str) -> tuple[int, int, int]:
    try:
        major_text, minor_text, patch_text = version.split(".", 2)
        digits = (int(major_text), int(minor_text), int(patch_text))
        if any(value < 0 for value in digits):
            raise ValueError
        return digits
    except (AttributeError, TypeError, ValueError):
        return (1, 0, 0)


def component_versions() -> list[tuple[str, str]]:
    """Return every bot component and its explicit or baseline version."""
    components: list[tuple[str, str]] = []

    for path in _component_paths():
        version = _explicit_version(path) or DEFAULT_COMPONENT_VERSION
        if _version_digits(version) == (1, 0, 0):
            version = DEFAULT_COMPONENT_VERSION
        components.append(
            (path.relative_to(_REPO_ROOT).as_posix(), version)
        )

    return components


def calculate_bot_version() -> str:
    """Build MASTER.COUNT.MAJOR_SUM.MINOR_SUM.PATCH_SUM automatically."""
    components = component_versions()
    major_sum = 0
    minor_sum = 0
    patch_sum = 0

    for _path, version in components:
        major, minor, patch = _version_digits(version)
        major_sum += major
        minor_sum += minor
        patch_sum += patch

    return (
        f"{MASTER_VERSION}.{len(components)}."
        f"{major_sum}.{minor_sum}.{patch_sum}"
    )


BOT_VERSION = calculate_bot_version()
