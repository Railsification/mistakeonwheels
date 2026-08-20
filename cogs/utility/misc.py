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





async def setup(bot: commands.Bot):
    from core.command_scope import bind_public_cog

    cog = MiscCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
