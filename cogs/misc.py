# cogs/misc.py
from __future__ import annotations

import discord
from discord.ext import commands
from discord import app_commands

from core.logger import log_cmd
from core.utils import ensure_deferred


class MiscCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="hello",
        description="Sanity check",
    )
    async def hello(self, interaction: discord.Interaction):
        log_cmd("hello", interaction)
        await ensure_deferred(interaction, ephemeral=True)
        await interaction.followup.send("✅ hello works", ephemeral=True)

    @app_commands.command(
        name="acktest",
        description="Check interaction latency",
    )
    async def acktest(self, interaction: discord.Interaction):
        log_cmd("acktest", interaction)
        await ensure_deferred(interaction, ephemeral=True)
        age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        await interaction.followup.send(f"Ack OK (age {age:.3f}s)", ephemeral=True)

    @app_commands.command(
        name="help",
        description="List all bot commands and their descriptions.",
    )
    async def help_cmd(self, interaction: discord.Interaction):
        log_cmd("help", interaction)
        await ensure_deferred(interaction, ephemeral=True)

        guild = interaction.guild
        guild_obj = discord.Object(id=guild.id)

        # Fix: handle _guild_ids being None
        commands_for_guild = []
        for c in self.bot.tree.get_commands():
            gids = getattr(c, "_guild_ids", None)
            # If gids is None, treat as "global" and include it
            if gids is None or guild_obj.id in gids:
                commands_for_guild.append(c)

        cmds_sorted = sorted(commands_for_guild, key=lambda c: c.name.lower())

        lines = ["__**Bot Commands**__\n"]
        for cmd in cmds_sorted:
            desc = cmd.description or "(no description)"
            lines.append(f"• `/{cmd.name}` — {desc}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)




async def setup(bot: commands.Bot):
    cog = MiscCog(bot)

    guild_obj = discord.Object(id=bot.hot_config["guild_id"])
    for cmd in cog.get_app_commands():
        cmd._guild_ids = {bot.hot_config["guild_id"]}
        cmd.guilds = (guild_obj,)

    await bot.add_cog(cog)
