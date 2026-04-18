# core/settings.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from .utils import DATA_DIR, load_json, save_json
from .logger import warn

SETTINGS_FILE = DATA_DIR / "settings.json"


FEATURE_KEYS = [
    "speech",        # /speech_convert etc
    "tag_image",     # /tag_member_image
    "pfp",           # /pfp, /pfp_theme
    "join_fact",     # join fact welcome posts
    "image_poll",    # /image_poll
    "connect4",      # /connect4
    "tictactoe",     # /tictactoe
    "games",         # /games
]


@dataclass
class GuildSettings:
    topic: str = "science"
    pfp_theme: str = ""
    feature_channels: Dict[str, List[int]] = field(
        default_factory=lambda: {k: [] for k in FEATURE_KEYS}
    )


def _default_all(config_defaults: dict) -> Dict[str, GuildSettings]:
    return {}


def _guild_key(guild_id: int) -> str:
    return str(guild_id)


def load_settings(config_defaults: dict) -> Dict[str, GuildSettings]:
    raw = load_json(SETTINGS_FILE, {})
    result: Dict[str, GuildSettings] = {}

    for gid, blob in raw.get("guilds", {}).items():
        fs = {k: [] for k in FEATURE_KEYS}
        fc = blob.get("feature_channels") or {}
        for k in FEATURE_KEYS:
            fs[k] = [int(c) for c in fc.get(k, [])]
        result[gid] = GuildSettings(
            topic=blob.get("topic") or config_defaults.get("topic_default", "science"),
            pfp_theme=blob.get("pfp_theme") or config_defaults.get("pfp_theme_default", ""),
            feature_channels=fs,
        )
    return result


def save_settings(all_settings: Dict[str, GuildSettings]):
    raw = {"guilds": {}}
    for gid, gs in all_settings.items():
        raw["guilds"][gid] = {
            "topic": gs.topic,
            "pfp_theme": gs.pfp_theme,
            "feature_channels": {
                k: list(v) for k, v in gs.feature_channels.items()
            },
        }
    save_json(SETTINGS_FILE, raw)


class SettingsManager:
    def __init__(self, config_defaults: dict):
        self._defaults = config_defaults
        self._guilds: Dict[str, GuildSettings] = load_settings(config_defaults)

    def _ensure_guild(self, guild_id: int) -> GuildSettings:
        key = _guild_key(guild_id)
        if key not in self._guilds:
            self._guilds[key] = GuildSettings(
                topic=self._defaults.get("topic_default", "science"),
                pfp_theme=self._defaults.get("pfp_theme_default", ""),
            )
        return self._guilds[key]

    def get_topic(self, guild_id: int) -> str:
        return self._ensure_guild(guild_id).topic

    def set_topic(self, guild_id: int, topic: str):
        gs = self._ensure_guild(guild_id)
        gs.topic = topic.strip() or "science"
        save_settings(self._guilds)

    def get_pfp_theme(self, guild_id: int) -> str:
        return self._ensure_guild(guild_id).pfp_theme

    def set_pfp_theme(self, guild_id: int, theme: str):
        gs = self._ensure_guild(guild_id)
        gs.pfp_theme = theme.strip()
        save_settings(self._guilds)

    def feature_channels(self, guild_id: int, feature: str) -> list[int]:
        gs = self._ensure_guild(guild_id)
        return gs.feature_channels.get(feature, [])

    def add_feature_channel(self, guild_id: int, feature: str, channel_id: int):
        if feature not in FEATURE_KEYS:
            warn(f"Unknown feature '{feature}' in add_feature_channel")
            return
        gs = self._ensure_guild(guild_id)
        lst = gs.feature_channels.setdefault(feature, [])
        if channel_id not in lst:
            lst.append(channel_id)
            save_settings(self._guilds)

    def remove_feature_channel(self, guild_id: int, feature: str, channel_id: int):
        if feature not in FEATURE_KEYS:
            warn(f"Unknown feature '{feature}' in remove_feature_channel")
            return
        gs = self._ensure_guild(guild_id)
        lst = gs.feature_channels.setdefault(feature, [])
        if channel_id in lst:
            lst.remove(channel_id)
            save_settings(self._guilds)

    def is_feature_allowed(self, guild_id: int, channel_id: int, feature: str) -> bool:
        """Only allowed inside channels explicitly added."""
        if feature not in FEATURE_KEYS:
            return False
        gs = self._ensure_guild(guild_id)
        allowed = gs.feature_channels.get(feature) or []
        # If no channels are configured, treat as disabled
        return channel_id in allowed
