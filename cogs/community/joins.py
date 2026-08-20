# cogs/joins.py
from __future__ import annotations

import discord
from discord.ext import commands
from discord import app_commands

from core.logger import log_cmd
from core.facts import get_random_fact
from core.settings import SettingsManager
from core.utils import ensure_deferred

class JoinsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # settings manager attached in setup below
        self.settings: SettingsManager = bot.settings

    async def _post_join_fact(self, member: discord.Member):
        guild = member.guild
        guild_id = guild.id

        # join facts are channel-locked
        channels = self.settings.feature_channels(guild_id, "join_fact")
        channel = None
        for cid in channels:
            ch = guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
                channel = ch
                break
        if channel is None:
            return  # no channel configured for join facts

        topic = self.settings.get_topic(guild_id)
        intro = f"👋 Welcome {member.mention}! Here's a random **{topic}** fact:"
        fact = await get_random_fact(topic)
        if fact:
            await channel.send(f"{intro}\n📘 {fact}")
        else:
            await channel.send(f"{intro}\n😕 Couldn't find one right now.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        await self._post_join_fact(member)

    # === Slash commands ===

    @app_commands.command(
        name="join_fact_topic_set",
        description="Set topic for random join facts",
    )
    async def join_fact_topic_set(self, interaction: discord.Interaction, topic: str):
        log_cmd("join_fact_topic_set", interaction)
        await ensure_deferred(interaction, ephemeral=True)
        guild_id = interaction.guild_id
        self.settings.set_topic(guild_id, topic)
        await interaction.followup.send(f"✅ Topic set to **{self.settings.get_topic(guild_id)}**", ephemeral=True)

    @app_commands.command(
        name="join_fact_topic_check",
        description="Check the current join fact topic",
    )
    async def join_fact_topic_check(self, interaction: discord.Interaction):
        log_cmd("join_fact_topic_check", interaction)
        await ensure_deferred(interaction, ephemeral=True)
        topic = self.settings.get_topic(interaction.guild_id)
        await interaction.followup.send(f"Current join topic: **{topic}**", ephemeral=True)

    @app_commands.command(
        name="fact",
        description="Get a random fact for this server's topic",
    )
    async def fact(self, interaction: discord.Interaction, topic: str | None = None):
        log_cmd("fact", interaction)
        await ensure_deferred(interaction)
        guild_id = interaction.guild_id
        topic = (topic or self.settings.get_topic(guild_id) or "science").strip()
        fact_text = await get_random_fact(topic)
        await interaction.followup.send(
            f"📘 Random fact about **{topic}**:\n{fact_text or '😕 None found.'}"
        )

    @app_commands.command(
        name="test_join_fact",
        description="Simulate join fact for a member",
    )
    async def test_join_fact(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        log_cmd("test_join_fact", interaction)
        await ensure_deferred(interaction)
        target = member or interaction.user
        await self._post_join_fact(target)
        await interaction.followup.send(f"🧪 Posted join fact for {target.mention}.")

async def setup(bot: commands.Bot):
    if not hasattr(bot, "settings"):
        from core.settings import SettingsManager
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = JoinsCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
