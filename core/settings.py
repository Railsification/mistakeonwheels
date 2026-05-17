from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .logger import warn
from .storage import (
    DATA_DIR,
    configured_guild_ids,
    global_json_path,
    load_global_json,
    load_guild_json,
    primary_data_guild_id,
    save_global_json,
    save_guild_json,
)
from .utils import load_json


FEATURE_KEYS = [
    "speech",
    "tag_image",
    "pfp",
    "join_fact",
    "image_poll",
    "connect4",
    "tictactoe",
    "games",
    "wos_furnace",
    "suggestion_poll",
    "canyon",
    "chest_pattern",
    "chief_gear",
]

SETTINGS_FILENAME = "settings.json"
FEATURE_INDEX_FILENAME = "feature_keys.json"
LEGACY_SETTINGS_FILE = DATA_DIR / "settings.json"


@dataclass
class GuildSettings:
    topic: str = "science"
    pfp_theme: str = ""
    feature_channels: Dict[str, List[int]] = field(default_factory=dict)


def _normalize_feature(feature: str) -> str:
    return (feature or "").strip()


def _dedupe_keep_order(values: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _int_list(values) -> list[int]:
    if not isinstance(values, list):
        return []
    out: list[int] = []
    for value in values:
        try:
            ivalue = int(value)
        except Exception:
            continue
        if ivalue and ivalue not in out:
            out.append(ivalue)
    return out


def _default_feature_keys() -> List[str]:
    return list(FEATURE_KEYS)


class SettingsManager:
    """Per-guild settings manager.

    Settings now live under:
        data/guilds/<guild_id>/settings.json

    Feature keys are global metadata only:
        data/global/feature_keys.json
    """

    def __init__(self, config_defaults: dict):
        self._defaults = config_defaults
        self._feature_keys: List[str] = self._load_feature_keys()
        self._migrate_legacy_settings_once()

    # ---------- feature key index ----------

    def _load_feature_keys(self) -> list[str]:
        raw = load_global_json(FEATURE_INDEX_FILENAME, {})
        keys = _default_feature_keys()
        if isinstance(raw, dict) and isinstance(raw.get("feature_keys"), list):
            keys.extend(str(v) for v in raw["feature_keys"])
        return _dedupe_keep_order(keys)

    def _save_feature_keys(self) -> None:
        save_global_json(FEATURE_INDEX_FILENAME, {"feature_keys": self._feature_keys})

    def _ensure_feature_registered(self, feature: str) -> str:
        feature_key = _normalize_feature(feature)
        if not feature_key:
            return ""
        if feature_key not in self._feature_keys:
            self._feature_keys.append(feature_key)
            self._save_feature_keys()
        return feature_key

    def feature_keys(self) -> list[str]:
        return list(self._feature_keys)

    def all_feature_keys(self, guild_id: int | None = None) -> list[str]:
        keys = list(self._feature_keys)
        if guild_id:
            gs = self._ensure_guild(guild_id)
            keys.extend(gs.feature_channels.keys())
        return _dedupe_keep_order(keys)

    # ---------- migration ----------

    def _migrate_legacy_settings_once(self) -> None:
        if not LEGACY_SETTINGS_FILE.exists():
            return

        raw = load_json(LEGACY_SETTINGS_FILE, {})
        if not isinstance(raw, dict):
            return

        # Old v1.6 shape: {"feature_keys": [...], "guilds": {gid: {...}}}
        if isinstance(raw.get("feature_keys"), list):
            self._feature_keys = _dedupe_keep_order([*self._feature_keys, *[str(v) for v in raw["feature_keys"]]])
            self._save_feature_keys()

        raw_guilds = raw.get("guilds")
        if isinstance(raw_guilds, dict):
            for gid_raw, blob in raw_guilds.items():
                try:
                    gid = int(gid_raw)
                except Exception:
                    continue
                target_data = load_guild_json(gid, SETTINGS_FILENAME, None)
                if target_data is not None:
                    continue
                if not isinstance(blob, dict):
                    continue
                save_guild_json(gid, SETTINGS_FILENAME, self._normalise_raw_settings(blob))
            return

        # Very old shape: settings for the one current server. Migrate only to primary.
        primary = primary_data_guild_id(self._defaults)
        if primary and load_guild_json(primary, SETTINGS_FILENAME, None) is None:
            save_guild_json(primary, SETTINGS_FILENAME, self._normalise_raw_settings(raw))

    # ---------- per-guild load/save ----------

    def _normalise_raw_settings(self, raw: dict | None) -> dict:
        raw = raw or {}
        fc_raw = raw.get("feature_channels") or raw.get("channels") or {}
        feature_channels: dict[str, list[int]] = {key: [] for key in self._feature_keys}
        if isinstance(fc_raw, dict):
            for feature, channel_ids in fc_raw.items():
                feature_key = self._ensure_feature_registered(str(feature))
                if feature_key:
                    feature_channels[feature_key] = _int_list(channel_ids)

        return {
            "topic": raw.get("topic") or self._defaults.get("topic_default", "science"),
            "pfp_theme": raw.get("pfp_theme") or self._defaults.get("pfp_theme_default", ""),
            "feature_channels": feature_channels,
        }

    def _load_guild_raw(self, guild_id: int) -> dict:
        raw = load_guild_json(guild_id, SETTINGS_FILENAME, None)
        if raw is None:
            raw = {
                "topic": self._defaults.get("topic_default", "science"),
                "pfp_theme": self._defaults.get("pfp_theme_default", ""),
                "feature_channels": {key: [] for key in self._feature_keys},
            }
            save_guild_json(guild_id, SETTINGS_FILENAME, raw)
            return raw
        if not isinstance(raw, dict):
            raw = {}
        normalised = self._normalise_raw_settings(raw)
        save_guild_json(guild_id, SETTINGS_FILENAME, normalised)
        return normalised

    def _save_guild_raw(self, guild_id: int, raw: dict) -> None:
        save_guild_json(guild_id, SETTINGS_FILENAME, raw)

    def _ensure_guild(self, guild_id: int) -> GuildSettings:
        raw = self._load_guild_raw(guild_id)
        fc = raw.get("feature_channels") if isinstance(raw.get("feature_channels"), dict) else {}
        for feature_key in self._feature_keys:
            fc.setdefault(feature_key, [])
        return GuildSettings(
            topic=raw.get("topic") or self._defaults.get("topic_default", "science"),
            pfp_theme=raw.get("pfp_theme") or self._defaults.get("pfp_theme_default", ""),
            feature_channels={str(k): _int_list(v) for k, v in fc.items()},
        )

    def _save_guild_settings(self, guild_id: int, gs: GuildSettings) -> None:
        self._save_guild_raw(
            guild_id,
            {
                "topic": gs.topic,
                "pfp_theme": gs.pfp_theme,
                "feature_channels": {k: list(v) for k, v in gs.feature_channels.items()},
            },
        )

    # ---------- public API ----------

    def get_topic(self, guild_id: int) -> str:
        return self._ensure_guild(guild_id).topic

    def set_topic(self, guild_id: int, topic: str):
        gs = self._ensure_guild(guild_id)
        gs.topic = topic.strip() or "science"
        self._save_guild_settings(guild_id, gs)

    def get_pfp_theme(self, guild_id: int) -> str:
        return self._ensure_guild(guild_id).pfp_theme

    def set_pfp_theme(self, guild_id: int, theme: str):
        gs = self._ensure_guild(guild_id)
        gs.pfp_theme = theme.strip()
        self._save_guild_settings(guild_id, gs)

    def feature_channels(self, guild_id: int, feature: str) -> list[int]:
        feature_key = self._ensure_feature_registered(feature)
        if not feature_key:
            return []
        gs = self._ensure_guild(guild_id)
        return list(gs.feature_channels.setdefault(feature_key, []))

    def add_feature_channel(self, guild_id: int, feature: str, channel_id: int):
        feature_key = self._ensure_feature_registered(feature)
        if not feature_key:
            warn("Blank feature passed to add_feature_channel")
            return
        gs = self._ensure_guild(guild_id)
        channels = gs.feature_channels.setdefault(feature_key, [])
        channel_id = int(channel_id)
        if channel_id not in channels:
            channels.append(channel_id)
            self._save_guild_settings(guild_id, gs)

    def remove_feature_channel(self, guild_id: int, feature: str, channel_id: int):
        feature_key = self._ensure_feature_registered(feature)
        if not feature_key:
            warn("Blank feature passed to remove_feature_channel")
            return
        gs = self._ensure_guild(guild_id)
        channels = gs.feature_channels.setdefault(feature_key, [])
        channel_id = int(channel_id)
        if channel_id in channels:
            channels.remove(channel_id)
            self._save_guild_settings(guild_id, gs)

    def is_feature_allowed(self, guild_id: int | None, channel_id: int | None, feature: str) -> bool:
        if guild_id is None or channel_id is None:
            return False
        feature_key = self._ensure_feature_registered(feature)
        if not feature_key:
            return False
        gs = self._ensure_guild(int(guild_id))
        allowed = gs.feature_channels.get(feature_key) or []
        return int(channel_id) in allowed
