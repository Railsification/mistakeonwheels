from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .utils import DATA_DIR, load_json, save_json
from .logger import info, warn

GUILDS_DIR = DATA_DIR / "guilds"
GLOBAL_DIR = DATA_DIR / "global"


def ensure_storage_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GUILDS_DIR.mkdir(parents=True, exist_ok=True)
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)


def guild_dir(guild_id: int) -> Path:
    ensure_storage_dirs()
    path = GUILDS_DIR / str(int(guild_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def guild_json_path(guild_id: int, filename: str) -> Path:
    return guild_dir(guild_id) / filename


def global_json_path(filename: str) -> Path:
    ensure_storage_dirs()
    return GLOBAL_DIR / filename


def load_guild_json(guild_id: int, filename: str, default: Any) -> Any:
    return load_json(guild_json_path(guild_id, filename), default)


def save_guild_json(guild_id: int, filename: str, data: Any) -> None:
    save_json(guild_json_path(guild_id, filename), data)


def load_global_json(filename: str, default: Any) -> Any:
    return load_json(global_json_path(filename), default)


def save_global_json(filename: str, data: Any) -> None:
    save_json(global_json_path(filename), data)


def known_guild_dirs() -> list[int]:
    ensure_storage_dirs()
    ids: list[int] = []
    for path in GUILDS_DIR.iterdir():
        if path.is_dir() and path.name.isdigit():
            ids.append(int(path.name))
    return sorted(ids)


def configured_guild_ids(bot_or_config: Any = None) -> list[int]:
    cfg = bot_or_config
    if hasattr(bot_or_config, "hot_config"):
        cfg = getattr(bot_or_config, "hot_config")
    if not isinstance(cfg, dict):
        return known_guild_dirs()
    ids = cfg.get("all_guild_ids") or cfg.get("guild_ids") or []
    return [int(x) for x in ids if int(x)]


def primary_data_guild_id(bot_or_config: Any = None) -> int:
    cfg = bot_or_config
    if hasattr(bot_or_config, "hot_config"):
        cfg = getattr(bot_or_config, "hot_config")
    if isinstance(cfg, dict):
        value = int(cfg.get("primary_data_guild_id") or cfg.get("guild_id") or 0)
        if value:
            return value
    ids = configured_guild_ids(bot_or_config)
    return ids[0] if ids else 0


def migrate_legacy_file_to_primary(filename: str, bot_or_config: Any, default: Any) -> None:
    """Copy old data/<filename> into data/guilds/<primary>/<filename> once.

    This is intentionally one primary guild only so old data does not bleed into every server.
    """
    primary = primary_data_guild_id(bot_or_config)
    if not primary:
        return
    legacy = DATA_DIR / filename
    target = guild_json_path(primary, filename)
    if target.exists() or not legacy.exists():
        return
    try:
        loaded = load_json(legacy, default)
        save_json(target, loaded)
        info(f"Migrated legacy data/{filename} -> data/guilds/{primary}/{filename}")
    except Exception as exc:
        warn(f"Failed to migrate legacy {filename}: {exc!r}")


def backup_legacy_file(filename: str) -> None:
    legacy = DATA_DIR / filename
    if not legacy.exists():
        return
    backup = DATA_DIR / f"{filename}.legacy.bak"
    if backup.exists():
        return
    try:
        shutil.copy2(legacy, backup)
    except Exception:
        pass
