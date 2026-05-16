# cogs/speech.py
from __future__ import annotations

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

from core.logger import log_cmd, warn
from core.utils import ensure_deferred
from core.settings import SettingsManager
from core.storage import load_guild_json, migrate_legacy_file_to_primary, save_guild_json

SPEECH_FILENAME = "speech_styles.json"


def load_styles(bot, guild_id: int) -> dict:
    migrate_legacy_file_to_primary(SPEECH_FILENAME, bot, {})
    data = load_guild_json(guild_id, SPEECH_FILENAME, {})
    return data if isinstance(data, dict) else {}


def save_styles(guild_id: int, data: dict) -> None:
    save_guild_json(guild_id, SPEECH_FILENAME, data)


class SpeechCog(commands.Cog):
    """
    Per-user “speech style” rewriter.

    - /speech_convert  → set style for a member
    - /speech_enabled  → toggle on/off
    - /speech_lookup   → see current style

    Actual rewriting happens in on_message, but **only** in channels
    where feature "speech" is allowed in SettingsManager.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self.api_key: str | None = bot.hot_config["openai_api_key"]
        self.model: str = bot.hot_config["openai_model"]

    # ---------- OpenAI helper ----------

    async def _generate_styled_text(self, original: str, style: str) -> str | None:
        if not self.api_key:
            return None

        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You rewrite the user's messages into the given style. "
                        "Preserve meaning, keep it concise, and do not add extra commentary."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Style: {style}\n\nText: {original}",
                },
            ],
            "temperature": 0.7,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=20) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        warn(f"speech HTTP {resp.status}: {body}")
                        return None
                    data = await resp.json()
        except Exception as e:
            warn(f"speech request error: {e!r}")
            return None

        try:
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            warn(f"speech parse error: {e!r}")
            return None

    # ---------- Listener ----------

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        # ignore bots & DMs
        if msg.author.bot or not msg.guild:
            return

        # feature channel lock: only run in allowed channels
        if not self.settings.is_feature_allowed(msg.guild.id, msg.channel.id, "speech"):
            return

        styles = load_styles(self.bot, msg.guild.id)
        cfg = styles.get(str(msg.author.id))
        if not cfg or not cfg.get("enabled", True) or not self.api_key:
            return

        content = msg.content or ""
        if not content or content.startswith(("/", "!")) or len(content.strip()) < 3:
            return

        style = cfg.get("style", "").strip()
        if not style:
            return

        styled = await self._generate_styled_text(content, style)
        if not styled:
            return

        try:
            await msg.channel.send(f"🗣️ **Styled for {msg.author.mention}:**\n{styled}")
        except Exception as e:
            warn(f"speech send failed: {e!r}")

    # ---------- Slash commands ----------

    @app_commands.command(
        name="speech_convert",
        description="Set a speech style for a member (rewrites their messages).",
    )
    @app_commands.describe(
        member="Member to configure",
        style="Describe how their messages should sound",
    )
    async def speech_convert(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        style: str,
    ):
        log_cmd("speech_convert", interaction)

        # channel lock for the command as well
        if not self.settings.is_feature_allowed(
            interaction.guild_id, interaction.channel_id, "speech"
        ):
            await interaction.response.send_message(
                "❌ `/speech_convert` can only be used in the configured **speech** channel(s).",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=True)

        if not self.api_key:
            await interaction.followup.send(
                "⚠️ OpenAI API key is not configured on the bot.",
                ephemeral=True,
            )
            return

        styles = load_styles(self.bot, interaction.guild_id)
        styles[str(member.id)] = {
            "style": style,
            "enabled": True,
        }
        save_styles(interaction.guild_id, styles)

        await interaction.followup.send(
            f"✅ Speech style set for {member.mention}.\n"
            f"```{style}```",
            ephemeral=True,
        )

    @app_commands.command(
        name="speech_enabled",
        description="Turn speech conversion on or off for a member.",
    )
    @app_commands.describe(
        member="Member whose style you want to toggle",
        enabled="True to enable, False to disable",
    )
    async def speech_enabled(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        enabled: bool,
    ):
        log_cmd("speech_enabled", interaction)

        # same channel lock as convert
        if not self.settings.is_feature_allowed(
            interaction.guild_id, interaction.channel_id, "speech"
        ):
            await interaction.response.send_message(
                "❌ `/speech_enabled` can only be used in the configured **speech** channel(s).",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=True)

        styles = load_styles(self.bot, interaction.guild_id)
        cfg = styles.get(str(member.id))
        if not cfg:
            await interaction.followup.send(
                "ℹ️ No speech style configured for that member.",
                ephemeral=True,
            )
            return

        cfg["enabled"] = enabled
        save_styles(interaction.guild_id, styles)
        state = "ON" if enabled else "OFF"
        await interaction.followup.send(
            f"✅ Speech style for {member.mention} is now **{state}**.",
            ephemeral=True,
        )

    @app_commands.command(
        name="speech_lookup",
        description="Show the speech style for a member.",
    )
    @app_commands.describe(member="Member to inspect")
    async def speech_lookup(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        log_cmd("speech_lookup", interaction)

        # same channel lock as convert
        if not self.settings.is_feature_allowed(
            interaction.guild_id, interaction.channel_id, "speech"
        ):
            await interaction.response.send_message(
                "❌ `/speech_lookup` can only be used in the configured **speech** channel(s).",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=True)

        target = member or interaction.user
        styles = load_styles(self.bot, interaction.guild_id)
        cfg = styles.get(str(target.id))
        if not cfg:
            await interaction.followup.send(
                f"ℹ️ No speech style configured for {target.mention}.",
                ephemeral=True,
            )
            return

        style = cfg.get("style", "(none)")
        enabled = cfg.get("enabled", True)
        state = "ON" if enabled else "OFF"
        text = (
            f"Speech style for {target.mention} is **{state}**.\n\n"
            f"```{style}```"
        )
        await interaction.followup.send(text, ephemeral=True)


async def setup(bot: commands.Bot):
    if not hasattr(bot, "settings"):
        from core.settings import SettingsManager
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = SpeechCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
