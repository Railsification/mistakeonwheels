# cogs/admin.py
from __future__ import annotations

import discord
from discord.ext import commands
from discord import app_commands

from core.logger import log_cmd
from core.settings import SettingsManager, FEATURE_KEYS
from core.utils import ensure_deferred


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

    # ==== helpers ====

    def _all_feature_keys(self, guild_id: int | None = None) -> list[str]:
        keys: list[str] = []

        # Prefer dynamic methods if the settings manager has them
        for attr in ("all_feature_keys", "feature_keys", "get_feature_keys", "list_feature_keys"):
            fn = getattr(self.settings, attr, None)
            if callable(fn):
                try:
                    result = fn(guild_id) if guild_id is not None else fn()
                except TypeError:
                    result = fn()
                if result:
                    keys.extend(str(x).strip() for x in result if str(x).strip())

        # Fall back to whatever is currently stored on the guild
        if guild_id is not None:
            try:
                gs = self.settings._ensure_guild(guild_id)  # noqa: SLF001
                fc = getattr(gs, "feature_channels", {}) or {}
                keys.extend(str(x).strip() for x in fc.keys() if str(x).strip())
            except Exception:
                pass

        # Always include static defaults too
        keys.extend(str(x).strip() for x in FEATURE_KEYS if str(x).strip())

        # Deduplicate, preserve order
        seen = set()
        out: list[str] = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out

    # ==== Feature channel control ====

    @app_commands.command(
        name="feature_channel_add",
        description="Allow a feature in a specific channel.",
    )
    @app_commands.describe(
        feature="Which feature to allow in this channel",
        channel="Channel to enable it in",
    )
    async def feature_channel_add(
        self,
        interaction: discord.Interaction,
        feature: str,
        channel: discord.TextChannel,
    ):
        log_cmd("feature_channel_add", interaction)
        await ensure_deferred(interaction, ephemeral=True)

        feature_key = feature.strip()
        if not feature_key:
            await interaction.followup.send("❌ Feature name cannot be blank.", ephemeral=True)
            return

        self.settings.add_feature_channel(
            interaction.guild_id,
            feature_key,
            channel.id,
        )
        await interaction.followup.send(
            f"✅ Feature **{feature_key}** allowed in {channel.mention}.",
            ephemeral=True,
        )

    @feature_channel_add.autocomplete("feature")
    async def feature_channel_add_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        current_lower = (current or "").lower()
        matches = [
            f for f in self._all_feature_keys(interaction.guild_id)
            if current_lower in f.lower()
        ][:25]

        return [
            app_commands.Choice(name=f, value=f)
            for f in matches
        ]

    @app_commands.command(
        name="feature_channel_remove",
        description="Remove a feature from a channel.",
    )
    @app_commands.describe(
        feature="Which feature to remove",
        channel="Channel to disable it in",
    )
    async def feature_channel_remove(
        self,
        interaction: discord.Interaction,
        feature: str,
        channel: discord.TextChannel,
    ):
        log_cmd("feature_channel_remove", interaction)
        await ensure_deferred(interaction, ephemeral=True)

        feature_key = feature.strip()
        if not feature_key:
            await interaction.followup.send("❌ Feature name cannot be blank.", ephemeral=True)
            return

        self.settings.remove_feature_channel(
            interaction.guild_id,
            feature_key,
            channel.id,
        )
        await interaction.followup.send(
            f"✅ Feature **{feature_key}** removed from {channel.mention}.",
            ephemeral=True,
        )

    @feature_channel_remove.autocomplete("feature")
    async def feature_remove_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        current_lower = (current or "").lower()
        matches = [
            f for f in self._all_feature_keys(interaction.guild_id)
            if current_lower in f.lower()
        ][:25]

        return [
            app_commands.Choice(name=f, value=f)
            for f in matches
        ]

    @app_commands.command(
        name="feature_channels",
        description="List feature channels in this server.",
    )
    async def feature_channels(self, interaction: discord.Interaction):
        log_cmd("feature_channels", interaction)
        await ensure_deferred(interaction, ephemeral=True)

        guild = interaction.guild
        gid = guild.id
        lines = ["__**Feature channels for this server**__"]
        for f in self._all_feature_keys(gid):
            ids = self.settings.feature_channels(gid, f)
            if not ids:
                lines.append(f"- **{f}**: _(none)_")
                continue
            mentions = []
            for cid in ids:
                ch = guild.get_channel(cid)
                if ch:
                    mentions.append(ch.mention)
                else:
                    mentions.append(f"`#{cid}`")
            lines.append(f"- **{f}**: " + ", ".join(mentions))

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ==== sync ====

    @app_commands.command(
        name="sync",
        description="Force re-sync of slash commands",
    )
    async def sync_cmd(self, interaction: discord.Interaction):
        log_cmd("sync", interaction)
        await ensure_deferred(interaction, ephemeral=True)
        guild = interaction.guild
        synced = await self.bot.tree.sync(guild=guild)
        names = ", ".join(sorted(c.name for c in synced))
        await interaction.followup.send(
            f"Synced {len(synced)} command(s):\n```\n{names}\n```",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    if not hasattr(bot, "settings"):
        from core.settings import SettingsManager
        bot.settings = SettingsManager(bot.hot_config)

    cog = AdminCog(bot)

    guild_obj = discord.Object(id=bot.hot_config["guild_id"])
    for cmd in cog.get_app_commands():
        cmd._guild_ids = {bot.hot_config["guild_id"]}
        cmd.guilds = (guild_obj,)

    await bot.add_cog(cog)
