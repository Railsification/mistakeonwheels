# cogs/channel_cleanup.py
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_admin_cog
from core.logger import log_cmd


class CleanupConfirmationView(discord.ui.View):
    def __init__(
        self,
        cog: "ChannelCleanupCog",
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.requester_id:
            return True

        await interaction.response.send_message(
            "Only the admin who opened this confirmation can use it.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Delete non-bot messages",
        style=discord.ButtonStyle.danger,
        emoji="🧹",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.cog._require_admin(interaction):
            return

        target, error_message = self.cog._resolve_target(
            self.guild_id,
            self.channel_id,
        )

        if target is None:
            await interaction.response.edit_message(
                content=f"❌ {error_message}",
                view=None,
            )
            self.stop()
            return

        guild, channel = target

        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content=(
                f"🧹 Cleaning **{guild.name}** → "
                f"**#{channel.name}** (`{channel.id}`)…\n"
                "Bot and webhook messages are being kept."
            ),
            view=self,
        )

        try:
            deleted = await channel.purge(
                limit=None,
                check=lambda message: (
                    not message.author.bot
                    and message.webhook_id is None
                ),
                bulk=True,
                reason=(
                    "Remote non-bot cleanup requested by "
                    f"{interaction.user} ({interaction.user.id})"
                ),
            )

        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    "❌ Discord refused the cleanup. The bot needs "
                    "**View Channel**, **Read Message History**, and "
                    "**Manage Messages** in that channel."
                ),
                view=None,
            )
            self.stop()
            return

        except discord.HTTPException as exc:
            await interaction.edit_original_response(
                content=f"❌ Discord failed during cleanup: {exc}",
                view=None,
            )
            self.stop()
            return

        await interaction.edit_original_response(
            content=(
                f"✅ Deleted **{len(deleted):,}** non-bot message(s) "
                f"from **{guild.name}** → "
                f"**#{channel.name}** (`{channel.id}`).\n"
                "All bot and webhook messages were left in place."
            ),
            view=None,
        )
        self.stop()

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Cleanup cancelled. Nothing was deleted.",
            view=None,
        )
        self.stop()


class ChannelCleanupCog(commands.Cog):
    cleanup = app_commands.Group(
        name="cleanup",
        description="Admin-only remote channel cleanup tools",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _admin_cog(self):
        return self.bot.get_cog("AdminCog")

    async def _require_admin(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        admin_cog = self._admin_cog()

        if admin_cog is None:
            await interaction.response.send_message(
                "❌ The admin cog is not loaded.",
                ephemeral=True,
            )
            return False

        return await admin_cog._require_admin(interaction)

    def _resolve_target(
        self,
        guild_value: object,
        channel_value: object,
    ) -> tuple[
        tuple[discord.Guild, discord.TextChannel] | None,
        str | None,
    ]:
        admin_cog = self._admin_cog()

        if admin_cog is None:
            return None, "The admin cog is not loaded."

        guild_id, channel_id, error_message = (
            admin_cog._target_ids(
                guild_value,
                channel_value,
            )
        )

        if error_message:
            return None, error_message

        guild = self.bot.get_guild(guild_id)
        channel = (
            guild.get_channel(channel_id)
            if guild
            else None
        )

        if not isinstance(channel, discord.TextChannel):
            return None, (
                "Select a normal text channel, not a thread."
            )

        bot_member = guild.me

        if bot_member is None:
            return None, (
                "The bot member could not be found in that server."
            )

        permissions = channel.permissions_for(bot_member)
        missing: list[str] = []

        if not permissions.view_channel:
            missing.append("View Channel")

        if not permissions.read_message_history:
            missing.append("Read Message History")

        if not permissions.manage_messages:
            missing.append("Manage Messages")

        if missing:
            return None, (
                "The bot is missing: "
                + ", ".join(
                    f"**{permission}**"
                    for permission in missing
                )
                + "."
            )

        return (guild, channel), None

    @cleanup.command(
        name="non_bot",
        description=(
            "Delete all human messages while keeping bot messages."
        ),
    )
    @app_commands.describe(
        server="Target server name or ID",
        channel="Target channel name or ID",
    )
    async def non_bot(
        self,
        interaction: discord.Interaction,
        server: str,
        channel: str,
    ) -> None:
        log_cmd("cleanup non_bot", interaction)

        if not await self._require_admin(interaction):
            return

        target, error_message = self._resolve_target(
            server,
            channel,
        )

        if target is None:
            await interaction.response.send_message(
                f"❌ {error_message}",
                ephemeral=True,
            )
            return

        guild, target_channel = target

        category_name = (
            target_channel.category.name
            if target_channel.category
            else "No category"
        )

        await interaction.response.send_message(
            (
                "⚠️ **Confirm channel cleanup**\n\n"
                f"**Server:** {guild.name} (`{guild.id}`)\n"
                f"**Channel:** #{target_channel.name} "
                f"(`{target_channel.id}`)\n"
                f"**Category:** {category_name}\n\n"
                "This permanently deletes "
                "**every non-bot message** in the selected "
                "channel. Bot and webhook messages remain."
            ),
            view=CleanupConfirmationView(
                self,
                requester_id=interaction.user.id,
                guild_id=guild.id,
                channel_id=target_channel.id,
            ),
            ephemeral=True,
        )

    @non_bot.autocomplete("server")
    async def non_bot_server_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        admin_cog = self._admin_cog()

        if admin_cog is None:
            return []

        return await admin_cog._server_choices(current)

    @non_bot.autocomplete("channel")
    async def non_bot_channel_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        admin_cog = self._admin_cog()

        if admin_cog is None:
            return []

        return await admin_cog._channel_choices(
            interaction,
            current,
            guild_parameter="server",
        )


async def setup(bot: commands.Bot) -> None:
    cog = ChannelCleanupCog(bot)
    bind_admin_cog(cog, bot)
    await bot.add_cog(cog)