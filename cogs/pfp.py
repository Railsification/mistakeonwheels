# cogs/pfp.py
from __future__ import annotations

import base64
import io
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

from core.logger import log_cmd, warn
from core.utils import ensure_deferred
from core.settings import SettingsManager


class PfpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self.image_model = bot.hot_config["openai_image_model"]
        self.api_key = bot.hot_config["openai_api_key"]

    async def _render_pfp(self, prompt: str) -> bytes | None:
        """Call OpenAI images API and return raw PNG bytes, or None on error."""
        if not self.api_key:
            warn("PFP: no API key configured")
            return None

        url = "https://api.openai.com/v1/images/generations"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # NOTE: gpt-image-1 does NOT support response_format.
        # It always returns base64 in data[0].b64_json by default.
        payload = {
            "model": self.image_model,
            "prompt": prompt,
            "size": "1024x1024",
            "n": 1,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, json=payload, timeout=60
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        warn(f"PFP HTTP {resp.status}: {body}")
                        return None
                    data = await resp.json()
        except Exception as e:
            warn(f"PFP request error: {e!r}")
            return None

        try:
            b64_img = data["data"][0]["b64_json"]
            return base64.b64decode(b64_img)
        except Exception as e:
            warn(f"PFP parse error: {e!r}")
            return None

    @app_commands.command(
        name="pfp_theme",
        description="Set server-wide profile picture theme.",
    )
    @app_commands.describe(
        theme="Example: 'cute cartoon ostrich in Whiteout Survival style'"
    )
    async def pfp_theme(self, interaction: discord.Interaction, theme: str):
        log_cmd("pfp_theme", interaction)
        await ensure_deferred(interaction, ephemeral=True)
        self.settings.set_pfp_theme(interaction.guild_id, theme)
        await interaction.followup.send(
            f"✅ PFP theme set to:\n```{self.settings.get_pfp_theme(interaction.guild_id)}```",
            ephemeral=True,
        )

    @app_commands.command(
        name="pfp_topic_check",
        description="Check the current PFP theme.",
    )
    async def pfp_topic_check(self, interaction: discord.Interaction):
        log_cmd("pfp_topic_check", interaction)
        await ensure_deferred(interaction, ephemeral=True)
        theme = self.settings.get_pfp_theme(interaction.guild_id)
        if not theme:
            await interaction.followup.send("No PFP theme set.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Current PFP theme:\n```{theme}```",
            ephemeral=True,
        )

    @app_commands.command(
        name="pfp",
        description="Generate a themed profile picture.",
    )
    @app_commands.describe(
        subject="Short description of you or your character",
    )
    async def pfp(self, interaction: discord.Interaction, subject: str):
        log_cmd("pfp", interaction)

        # channel lock
        if not self.settings.is_feature_allowed(
            interaction.guild_id, interaction.channel_id, "pfp"
        ):
            await interaction.response.send_message(
                "❌ `/pfp` can only be used in the configured PFP channel(s).",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=True)

        theme = self.settings.get_pfp_theme(interaction.guild_id)
        if not theme:
            await interaction.followup.send(
                "⚠️ No PFP theme set. Use `/pfp_theme` first.",
                ephemeral=True,
            )
            return

        if not self.api_key:
            await interaction.followup.send(
                "⚠️ OpenAI API key is not configured.",
                ephemeral=True,
            )
            return

        full_prompt = f"{subject}. Style: {theme}"
        img_bytes = await self._render_pfp(full_prompt)
        if not img_bytes:
            await interaction.followup.send(
                "⚠️ Failed to generate image.",
                ephemeral=True,
            )
            return

        file = discord.File(fp=io.BytesIO(img_bytes), filename="pfp.png")
        await interaction.followup.send(
            content=f"🖼️ Here's your themed PFP for **{subject}**:",
            file=file,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    if not hasattr(bot, "settings"):
        from core.settings import SettingsManager
        bot.settings = SettingsManager(bot.hot_config)
    cog = PfpCog(bot)

    guild_obj = discord.Object(id=bot.hot_config["guild_id"])
    for cmd in cog.get_app_commands():
        cmd._guild_ids = {bot.hot_config["guild_id"]}
        cmd.guilds = (guild_obj,)

    await bot.add_cog(cog)
