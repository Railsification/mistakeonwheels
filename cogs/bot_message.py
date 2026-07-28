# cogs/bot_message.py
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import all_guild_ids, bind_admin_cog
from core.logger import err, log_cmd


MESSAGE_CHANNEL_TYPES = (discord.TextChannel, discord.Thread)


class BotMessageModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "BotMessageCog",
        *,
        guild_id: int,
        channel_id: int,
        ping_everyone: bool,
    ):
        super().__init__(title="Send bot message", timeout=300)

        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.ping_everyone = ping_everyone

        self.message_input = discord.ui.TextInput(
            label="Message",
            placeholder="Type exactly what the bot should post...",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000,
        )

        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.cog.require_admin(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        target, error_message = self.cog.resolve_target(
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            ping_everyone=self.ping_everyone,
        )

        if target is None:
            await interaction.followup.send(
                f"❌ {error_message}",
                ephemeral=True,
            )
            return

        guild, channel = target

        content = str(self.message_input.value).strip()

        if not content:
            await interaction.followup.send(
                "❌ The message cannot be blank.",
                ephemeral=True,
            )
            return

        if self.ping_everyone and not content.lstrip().startswith("@everyone"):
            content = f"@everyone {content}"

        if len(content) > 2000:
            await interaction.followup.send(
                "❌ The message is over Discord's 2,000-character limit.",
                ephemeral=True,
            )
            return

        allowed_mentions = discord.AllowedMentions(
            everyone=self.ping_everyone,
            users=True,
            roles=True,
            replied_user=False,
        )

        try:
            sent_message = await channel.send(
                content,
                allowed_mentions=allowed_mentions,
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ The bot does not have permission to send that message.",
                ephemeral=True,
            )
            return

        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Discord failed to send the message: {exc}",
                ephemeral=True,
            )
            return

        everyone_status = "ON" if self.ping_everyone else "OFF"

        await interaction.followup.send(
            f"✅ Sent to **{guild.name}** → {channel.mention}\n"
            f"`@everyone`: **{everyone_status}**\n"
            f"[Open message]({sent_message.jump_url})",
            ephemeral=True,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        err(f"Bot message modal failed: {error!r}")

        error_text = "❌ Something went wrong while sending the bot message."

        if interaction.response.is_done():
            await interaction.followup.send(
                error_text,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                error_text,
                ephemeral=True,
            )


class BotMessageCog(commands.Cog):
    bot_group = app_commands.Group(
        name="bot",
        description="Admin-only bot controls",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def is_admin_guild(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        config = getattr(self.bot, "hot_config", {}) or {}

        admin_guild_id = int(
            config.get("admin_guild_id", 0) or 0
        )

        return interaction.guild_id == admin_guild_id

    def has_admin_role(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        member = interaction.user

        if not isinstance(member, discord.Member):
            return False

        if member.guild_permissions.administrator:
            return True

        config = getattr(self.bot, "hot_config", {}) or {}

        admin_role_names = set(
            config.get("admin_role_names", [])
        )

        if not admin_role_names:
            return False

        return any(
            role.name in admin_role_names
            for role in member.roles
        )

    async def require_admin(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if not self.is_admin_guild(interaction):
            await interaction.response.send_message(
                "Admin commands only work in the admin/test server.",
                ephemeral=True,
            )
            return False

        if not self.has_admin_role(interaction):
            await interaction.response.send_message(
                "Nope. Admin role only.",
                ephemeral=True,
            )
            return False

        return True

    def configured_guild_ids(self) -> list[int]:
        return all_guild_ids(self.bot)

    @staticmethod
    def parse_id(raw_value: object) -> int | None:
        if isinstance(raw_value, app_commands.Choice):
            raw_value = raw_value.value

        value = str(raw_value or "").strip()

        if value.startswith("<#") and value.endswith(">"):
            value = value[2:-1]

        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None

        if parsed <= 0:
            return None

        return parsed

    @staticmethod
    def get_channel_or_thread(
        guild: discord.Guild,
        channel_id: int,
    ) -> discord.TextChannel | discord.Thread | None:
        get_combined = getattr(
            guild,
            "get_channel_or_thread",
            None,
        )

        if callable(get_combined):
            channel = get_combined(channel_id)
        else:
            channel = (
                guild.get_channel(channel_id)
                or guild.get_thread(channel_id)
            )

        if isinstance(channel, MESSAGE_CHANNEL_TYPES):
            return channel

        return None

    def resolve_target(
        self,
        *,
        guild_id: int,
        channel_id: int,
        ping_everyone: bool,
    ) -> tuple[
        tuple[
            discord.Guild,
            discord.TextChannel | discord.Thread,
        ] | None,
        str | None,
    ]:
        if guild_id not in self.configured_guild_ids():
            return None, "That server is not configured for this bot."

        guild = self.bot.get_guild(guild_id)

        if guild is None:
            return None, "The bot is not connected to that server."

        channel = self.get_channel_or_thread(
            guild,
            channel_id,
        )

        if channel is None:
            return None, "That channel was not found in the selected server."

        bot_member = guild.me

        if bot_member is None:
            return None, "The bot member could not be found in that server."

        permissions = channel.permissions_for(bot_member)

        if not permissions.view_channel:
            return None, "The bot cannot view that channel."

        if isinstance(channel, discord.Thread):
            if not permissions.send_messages_in_threads:
                return None, "The bot cannot send messages in that thread."

        elif not permissions.send_messages:
            return None, "The bot cannot send messages in that channel."

        if ping_everyone and not permissions.mention_everyone:
            return (
                None,
                "The bot does not have the Mention @everyone permission "
                "in that channel.",
            )

        return (guild, channel), None

    @bot_group.command(
        name="message",
        description="Send a manual message from the bot.",
    )
    @app_commands.describe(
        server="Target server name or ID",
        channel="Target channel name or ID",
        everyone="Turn the @everyone ping on or off",
    )
    @app_commands.choices(
        everyone=[
            app_commands.Choice(
                name="Off — no @everyone ping",
                value="off",
            ),
            app_commands.Choice(
                name="On — ping @everyone",
                value="on",
            ),
        ]
    )
    async def message(
        self,
        interaction: discord.Interaction,
        server: str,
        channel: str,
        everyone: app_commands.Choice[str],
    ) -> None:
        log_cmd("bot message", interaction)

        if not await self.require_admin(interaction):
            return

        guild_id = self.parse_id(server)
        channel_id = self.parse_id(channel)

        if guild_id is None or channel_id is None:
            await interaction.response.send_message(
                "❌ Select a server and channel from the list, "
                "or enter valid numeric IDs.",
                ephemeral=True,
            )
            return

        ping_everyone = everyone.value == "on"

        target, error_message = self.resolve_target(
            guild_id=guild_id,
            channel_id=channel_id,
            ping_everyone=ping_everyone,
        )

        if target is None:
            await interaction.response.send_message(
                f"❌ {error_message}",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            BotMessageModal(
                self,
                guild_id=guild_id,
                channel_id=channel_id,
                ping_everyone=ping_everyone,
            )
        )

    @message.autocomplete("server")
    async def server_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current_lower = (current or "").lower().strip()

        choices: list[app_commands.Choice[str]] = []

        for guild_id in self.configured_guild_ids():
            guild = self.bot.get_guild(guild_id)

            guild_name = (
                guild.name
                if guild is not None
                else "Server not cached"
            )

            search_text = f"{guild_name} {guild_id}".lower()

            if current_lower and current_lower not in search_text:
                continue

            label = f"{guild_name} — {guild_id}"[:100]

            choices.append(
                app_commands.Choice(
                    name=label,
                    value=str(guild_id),
                )
            )

            if len(choices) >= 25:
                break

        return choices

    @message.autocomplete("channel")
    async def channel_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        server_value = getattr(
            interaction.namespace,
            "server",
            None,
        )

        guild_id = self.parse_id(server_value)

        if (
            guild_id is None
            or guild_id not in self.configured_guild_ids()
        ):
            return []

        guild = self.bot.get_guild(guild_id)

        if guild is None:
            return []

        current_lower = (current or "").lower().strip()

        choices: list[app_commands.Choice[str]] = []

        text_channels = sorted(
            guild.text_channels,
            key=lambda channel: (
                channel.category.position
                if channel.category
                else -1,
                channel.position,
                channel.name.lower(),
            ),
        )

        for text_channel in text_channels:
            category_name = (
                text_channel.category.name
                if text_channel.category
                else "No category"
            )

            search_text = (
                f"{text_channel.name} "
                f"{category_name} "
                f"{text_channel.id}"
            ).lower()

            if current_lower and current_lower not in search_text:
                continue

            label = (
                f"#{text_channel.name} — {category_name}"
            )[:100]

            choices.append(
                app_commands.Choice(
                    name=label,
                    value=str(text_channel.id),
                )
            )

            if len(choices) >= 25:
                return choices

        for thread in sorted(
            guild.threads,
            key=lambda thread: thread.name.lower(),
        ):
            parent_name = (
                thread.parent.name
                if thread.parent
                else "Unknown"
            )

            search_text = (
                f"{thread.name} "
                f"{parent_name} "
                f"{thread.id}"
            ).lower()

            if current_lower and current_lower not in search_text:
                continue

            label = (
                f"Thread: {thread.name} — #{parent_name}"
            )[:100]

            choices.append(
                app_commands.Choice(
                    name=label,
                    value=str(thread.id),
                )
            )

            if len(choices) >= 25:
                break

        return choices


async def setup(bot: commands.Bot) -> None:
    cog = BotMessageCog(bot)

    bind_admin_cog(cog, bot)

    await bot.add_cog(cog)