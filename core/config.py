from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable


def _parse_ids(raw: str | None) -> list[int]:
    ids: list[int] = []
    if not raw:
        return ids
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value and value not in ids:
            ids.append(value)
    return ids


def _dedupe(values: Iterable[int]) -> list[int]:
    out: list[int] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


@dataclass(slots=True)
class BotConfig:
    token: str
    admin_guild_id: int
    public_guild_ids: list[int]
    all_guild_ids: list[int]
    primary_data_guild_id: int
    legacy_guild_ids: list[int]
    media_channel_id: int
    topic_default: str
    pfp_theme_default: str
    openai_api_key: str
    openai_model: str
    openai_image_model: str
    auto_sync_on_startup: bool
    admin_role_names: list[str]

    def as_hot_config(self) -> dict:
        return {
            "guild_id": self.primary_data_guild_id,
            "guild_ids": self.all_guild_ids,
            "admin_guild_id": self.admin_guild_id,
            "public_guild_ids": self.public_guild_ids,
            "all_guild_ids": self.all_guild_ids,
            "primary_data_guild_id": self.primary_data_guild_id,
            "legacy_guild_ids": self.legacy_guild_ids,
            "media_channel_id": self.media_channel_id,
            "topic_default": self.topic_default,
            "pfp_theme_default": self.pfp_theme_default,
            "openai_api_key": self.openai_api_key,
            "openai_model": self.openai_model,
            "openai_image_model": self.openai_image_model,
            "admin_role_names": self.admin_role_names,
        }


def load_bot_config() -> BotConfig:
    token = os.getenv("BOT_TOKEN", "").strip()

    legacy_guild_ids = _parse_ids(os.getenv("GUILD_ID"))
    admin_guild_id = _parse_ids(os.getenv("ADMIN_GUILD_ID"))
    public_guild_ids = _parse_ids(os.getenv("PUBLIC_GUILD_IDS"))

    admin_id = admin_guild_id[0] if admin_guild_id else (legacy_guild_ids[0] if legacy_guild_ids else 0)

    # Backwards compatible fallback: if PUBLIC_GUILD_IDS is not set yet, use GUILD_ID.
    # Once PUBLIC_GUILD_IDS is set, only those public servers get user commands.
    if not public_guild_ids:
        public_guild_ids = [gid for gid in legacy_guild_ids if gid != admin_id]
        if not public_guild_ids and admin_id:
            public_guild_ids = [admin_id]

    all_guild_ids = _dedupe([admin_id, *public_guild_ids])
    primary_data_guild_id = public_guild_ids[0] if public_guild_ids else admin_id

    # Startup slash-command sync must stay on so admin/public command visibility stays correct
    # after every deploy. Do not gate this behind an env flag.
    auto_sync_on_startup = True

    role_raw = os.getenv("ADMIN_ROLE_NAMES") or "Tech,Admin,Council"
    admin_role_names = [x.strip() for x in role_raw.split(",") if x.strip()]

    return BotConfig(
        token=token,
        admin_guild_id=admin_id,
        public_guild_ids=public_guild_ids,
        all_guild_ids=all_guild_ids,
        primary_data_guild_id=primary_data_guild_id,
        legacy_guild_ids=legacy_guild_ids,
        media_channel_id=int(os.getenv("MEDIA_CHANNEL_ID", "0") or 0),
        topic_default=(os.getenv("TOPIC") or "science").strip(),
        pfp_theme_default=(os.getenv("PFP_THEME") or "").strip(),
        openai_api_key=(os.getenv("OPENAI_API_KEY") or "").strip(),
        openai_model=(os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip(),
        openai_image_model=(os.getenv("OPENAI_IMAGE_MODEL") or "gpt-image-1").strip(),
        auto_sync_on_startup=auto_sync_on_startup,
        admin_role_names=admin_role_names,
    )
