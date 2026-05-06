from __future__ import annotations

from dataclasses import dataclass, field
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
    feature_channels: Dict[str, List[int]] = field(default_factory=dict)


@dataclass
class SettingsState:
    guilds: Dict[str, GuildSettings] = field(default_factory=dict)
    feature_keys: List[str] = field(default_factory=list)


def _guild_key(guild_id: int) -> str:
    return str(guild_id)


def _normalize_feature(feature: str) -> str:
    return (feature or "").strip()


def _dedupe_keep_order(values: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _default_feature_keys() -> List[str]:
    return list(FEATURE_KEYS)


def load_settings(config_defaults: dict) -> SettingsState:
    raw = load_json(SETTINGS_FILE, {})

    stored_feature_keys = raw.get("feature_keys") if isinstance(raw, dict) else None
    feature_keys = _default_feature_keys()
    if isinstance(stored_feature_keys, list):
        feature_keys.extend(str(v) for v in stored_feature_keys if str(v).strip())
    feature_keys = _dedupe_keep_order(feature_keys)

    guilds: Dict[str, GuildSettings] = {}
    raw_guilds = raw.get("guilds", {}) if isinstance(raw, dict) else {}

    for gid, blob in raw_guilds.items():
        blob = blob or {}
        fc = blob.get("feature_channels") or {}

        # Preserve unknown feature keys already stored in settings.json.
        if isinstance(fc, dict):
            for key in fc.keys():
                key = _normalize_feature(str(key))
                if key:
                    feature_keys.append(key)
        feature_keys = _dedupe_keep_order(feature_keys)

        fs: Dict[str, List[int]] = {k: [] for k in feature_keys}
        if isinstance(fc, dict):
            for k, values in fc.items():
                nk = _normalize_feature(str(k))
                if not nk:
                    continue
                if not isinstance(values, list):
                    continue
                fs[nk] = [int(c) for c in values]

        guilds[str(gid)] = GuildSettings(
            topic=blob.get("topic") or config_defaults.get("topic_default", "science"),
            pfp_theme=blob.get("pfp_theme") or config_defaults.get("pfp_theme_default", ""),
            feature_channels=fs,
        )

    return SettingsState(guilds=guilds, feature_keys=_dedupe_keep_order(feature_keys))


def save_settings(state: SettingsState):
    raw = {
        "feature_keys": list(state.feature_keys),
        "guilds": {},
    }
    for gid, gs in state.guilds.items():
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
        state = load_settings(config_defaults)
        self._guilds: Dict[str, GuildSettings] = state.guilds
        self._feature_keys: List[str] = state.feature_keys

    def _save(self):
        save_settings(SettingsState(guilds=self._guilds, feature_keys=self._feature_keys))

    def _ensure_feature_registered(self, feature: str) -> str:
        feature_key = _normalize_feature(feature)
        if not feature_key:
            return ""
        if feature_key not in self._feature_keys:
            self._feature_keys.append(feature_key)
            # Add the newly-discovered feature to every guild as disabled by default.
            for gs in self._guilds.values():
                gs.feature_channels.setdefault(feature_key, [])
            self._save()
        return feature_key

    def feature_keys(self) -> list[str]:
        return list(self._feature_keys)

    def _ensure_guild(self, guild_id: int) -> GuildSettings:
        key = _guild_key(guild_id)
        if key not in self._guilds:
            self._guilds[key] = GuildSettings(
                topic=self._defaults.get("topic_default", "science"),
                pfp_theme=self._defaults.get("pfp_theme_default", ""),
                feature_channels={k: [] for k in self._feature_keys},
            )
        else:
            for feature_key in self._feature_keys:
                self._guilds[key].feature_channels.setdefault(feature_key, [])
        return self._guilds[key]

    def get_topic(self, guild_id: int) -> str:
        return self._ensure_guild(guild_id).topic

    def set_topic(self, guild_id: int, topic: str):
        gs = self._ensure_guild(guild_id)
        gs.topic = topic.strip() or "science"
        self._save()

    def get_pfp_theme(self, guild_id: int) -> str:
        return self._ensure_guild(guild_id).pfp_theme

    def set_pfp_theme(self, guild_id: int, theme: str):
        gs = self._ensure_guild(guild_id)
        gs.pfp_theme = theme.strip()
        self._save()

    def feature_channels(self, guild_id: int, feature: str) -> list[int]:
        feature_key = self._ensure_feature_registered(feature)
        if not feature_key:
            return []
        gs = self._ensure_guild(guild_id)
        return gs.feature_channels.setdefault(feature_key, [])

    def add_feature_channel(self, guild_id: int, feature: str, channel_id: int):
        feature_key = self._ensure_feature_registered(feature)
        if not feature_key:
            warn("Blank feature passed to add_feature_channel")
            return
        gs = self._ensure_guild(guild_id)
        lst = gs.feature_channels.setdefault(feature_key, [])
        if channel_id not in lst:
            lst.append(channel_id)
            self._save()

    def remove_feature_channel(self, guild_id: int, feature: str, channel_id: int):
        feature_key = self._ensure_feature_registered(feature)
        if not feature_key:
            warn("Blank feature passed to remove_feature_channel")
            return
        gs = self._ensure_guild(guild_id)
        lst = gs.feature_channels.setdefault(feature_key, [])
        if channel_id in lst:
            lst.remove(channel_id)
            self._save()

    def is_feature_allowed(self, guild_id: int, channel_id: int, feature: str) -> bool:
        """Only allowed inside channels explicitly added."""
        feature_key = self._ensure_feature_registered(feature)
        if not feature_key:
            return False
        gs = self._ensure_guild(guild_id)
        allowed = gs.feature_channels.get(feature_key) or []
        # If no channels are configured, treat as disabled.
        return channel_id in allowed
