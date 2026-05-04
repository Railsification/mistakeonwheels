import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)


class ChestPatternCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="chest_pattern", description="Private chest pattern tool")
    @app_commands.describe(
        screenshot_1="Screenshot 1",
        screenshot_2="Screenshot 2",
        screenshot_3="Screenshot 3",
        screenshot_4="Screenshot 4",
        screenshot_5="Screenshot 5",
        screenshot_6="Screenshot 6",
        screenshot_7="Screenshot 7",
        screenshot_8="Screenshot 8",
        screenshot_9="Screenshot 9",
        screenshot_10="Screenshot 10",
    )
    async def chest_pattern(
        self,
        interaction: discord.Interaction,
        screenshot_1: discord.Attachment,
        screenshot_2: Optional[discord.Attachment] = None,
        screenshot_3: Optional[discord.Attachment] = None,
        screenshot_4: Optional[discord.Attachment] = None,
        screenshot_5: Optional[discord.Attachment] = None,
        screenshot_6: Optional[discord.Attachment] = None,
        screenshot_7: Optional[discord.Attachment] = None,
        screenshot_8: Optional[discord.Attachment] = None,
        screenshot_9: Optional[discord.Attachment] = None,
        screenshot_10: Optional[discord.Attachment] = None,
    ):
        files = [
            screenshot_1, screenshot_2, screenshot_3, screenshot_4, screenshot_5,
            screenshot_6, screenshot_7, screenshot_8, screenshot_9, screenshot_10,
        ]
        files = [f for f in files if f is not None]

        await interaction.response.send_message(
            f"Chest command is registered. Files received: {len(files)}",
            ephemeral=True,
        )

    @app_commands.command(name="chest_stats", description="Private chest stats")
    async def chest_stats(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Chest stats command is registered.",
            ephemeral=True,
        )

    @app_commands.command(name="chest_reset", description="Private chest reset")
    async def chest_reset(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Chest reset command is registered.",
            ephemeral=True,
        )


async def setup(bot):
    cog = ChestPatternCog(bot)
    if GUILD_ID:
        await bot.add_cog(cog, guild=discord.Object(id=GUILD_ID))
    else:
        await bot.add_cog(cog)
