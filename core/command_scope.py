from __future__ import annotations

from typing import Iterable

import discord
from discord.ext import commands


def _dedupe(ids: Iterable[int]) -> list[int]:
    out: list[int] = []
    for raw in ids:
        try:
            gid = int(raw)
        except Exception:
            continue
        if gid and gid not in out:
            out.append(gid)
    return out


def admin_guild_ids(bot: commands.Bot) -> list[int]:
    cfg = getattr(bot, "hot_config", {}) or {}
    gid = int(cfg.get("admin_guild_id") or cfg.get("guild_id") or 0)
    return [gid] if gid else []


def public_guild_ids(bot: commands.Bot, *, include_admin: bool = True) -> list[int]:
    cfg = getattr(bot, "hot_config", {}) or {}
    ids = list(cfg.get("public_guild_ids") or [])
    if include_admin:
        ids = [*admin_guild_ids(bot), *ids]
    return _dedupe(ids)


def all_guild_ids(bot: commands.Bot) -> list[int]:
    cfg = getattr(bot, "hot_config", {}) or {}
    return _dedupe(cfg.get("all_guild_ids") or [*admin_guild_ids(bot), *public_guild_ids(bot)])


def bind_command_to_guilds(command, guild_ids: Iterable[int]) -> None:
    ids = _dedupe(guild_ids)
    guild_objects = tuple(discord.Object(id=gid) for gid in ids)
    command.guild_only = True
    command._guild_ids = set(ids)
    command.guilds = guild_objects


def bind_cog_to_guilds(cog: commands.Cog, guild_ids: Iterable[int]) -> None:
    ids = _dedupe(guild_ids)
    for command in cog.get_app_commands():
        bind_command_to_guilds(command, ids)


def bind_public_cog(cog: commands.Cog, bot: commands.Bot, *, include_admin: bool = True) -> None:
    bind_cog_to_guilds(cog, public_guild_ids(bot, include_admin=include_admin))


def bind_admin_cog(cog: commands.Cog, bot: commands.Bot) -> None:
    bind_cog_to_guilds(cog, admin_guild_ids(bot))


def bind_group_public(group, bot: commands.Bot, *, include_admin: bool = True) -> None:
    bind_command_to_guilds(group, public_guild_ids(bot, include_admin=include_admin))


def bind_group_admin(group, bot: commands.Bot) -> None:
    bind_command_to_guilds(group, admin_guild_ids(bot))
