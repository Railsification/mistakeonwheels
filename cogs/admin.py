# cogs/admin.py
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import all_guild_ids, bind_admin_cog
from core.logger import log_cmd
from core.settings import FEATURE_KEYS, SettingsManager
from core.utils import ensure_deferred


MESSAGE_CHANNEL_TYPES = (discord.TextChannel, discord.Thread)


class CouncilMessageModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "AdminCog",
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
            max_length=1989 if ping_everyone else 2000,
        )

        self.add_item(self.message_input)

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not await self.cog._require_admin(interaction):
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        target, error_message = self.cog._resolve_message_target(
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

        if (
            self.ping_everyone
            and not content.lstrip().startswith("@everyone")
        ):
            content = f"@everyone {content}"

        if len(content) > 2000:
            await interaction.followup.send(
                "❌ The message is over Discord's "
                "2,000-character limit.",
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
                "❌ The bot does not have permission "
                "to send in that channel.",
                ephemeral=True,
            )
            return

        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Discord failed to send the message: {exc}",
                ephemeral=True,
            )
            return

        everyone_status = (
            "ON"
            if self.ping_everyone
            else "OFF"
        )

        await interaction.followup.send(
            f"✅ Sent to **{guild.name}** → {channel.mention}\n"
            f"`@everyone`: **{everyone_status}**\n"
            f"[Open message]({sent_message.jump_url})",
            ephemeral=True,
        )


class AdminCog(commands.Cog):
    council = app_commands.Group(
        name="council",
        description="Admin/test-server bot controls",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

    # ---------- checks ----------

    def _is_admin_guild(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        admin_guild_id = int(
            (
                getattr(
                    self.bot,
                    "hot_config",
                    {},
                )
                or {}
            ).get(
                "admin_guild_id",
                0,
            )
            or 0
        )

        return interaction.guild_id == admin_guild_id

    def _has_admin_role(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        member = interaction.user

        if not isinstance(member, discord.Member):
            return False

        if member.guild_permissions.administrator:
            return True

        role_names = set(
            (
                getattr(
                    self.bot,
                    "hot_config",
                    {},
                )
                or {}
            ).get(
                "admin_role_names",
                [],
            )
        )

        if not role_names:
            return False

        return any(
            role.name in role_names
            for role in member.roles
        )

    async def _require_admin(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if not self._is_admin_guild(interaction):
            await interaction.response.send_message(
                "Admin commands only work in the "
                "admin/test server.",
                ephemeral=True,
            )
            return False

        if not self._has_admin_role(interaction):
            await interaction.response.send_message(
                "Nope. Admin role only.",
                ephemeral=True,
            )
            return False

        return True

    def _all_feature_keys(
        self,
        guild_id: int | None = None,
    ) -> list[str]:
        keys: list[str] = []

        for attr in (
            "all_feature_keys",
            "feature_keys",
            "get_feature_keys",
            "list_feature_keys",
        ):
            fn = getattr(
                self.settings,
                attr,
                None,
            )

            if callable(fn):
                try:
                    result = (
                        fn(guild_id)
                        if guild_id is not None
                        else fn()
                    )
                except TypeError:
                    result = fn()

                if result:
                    keys.extend(
                        str(item).strip()
                        for item in result
                        if str(item).strip()
                    )

        keys.extend(
            str(item).strip()
            for item in FEATURE_KEYS
            if str(item).strip()
        )

        seen: set[str] = set()
        output: list[str] = []

        for key in keys:
            if key not in seen:
                seen.add(key)
                output.append(key)

        return output

    def _configured_server_lines(
        self,
    ) -> list[str]:
        config = (
            getattr(
                self.bot,
                "hot_config",
                {},
            )
            or {}
        )

        admin_id = int(
            config.get("admin_guild_id")
            or 0
        )

        public_ids = [
            int(guild_id)
            for guild_id in config.get(
                "public_guild_ids",
                [],
            )
        ]

        lines: list[str] = []

        for guild_id in all_guild_ids(self.bot):
            guild = self.bot.get_guild(guild_id)

            name = (
                guild.name
                if guild
                else "not cached yet"
            )

            tag = (
                "admin/test"
                if guild_id == admin_id
                else "public"
            )

            if (
                guild_id in public_ids
                and guild_id == admin_id
            ):
                tag = "admin/test + public"

            lines.append(
                f"`{guild_id}` — "
                f"**{name}** ({tag})"
            )

        return lines or [
            "No configured guild IDs."
        ]

    # ---------- manual bot message ----------

    @staticmethod
    def _parse_discord_id(
        raw_value: object,
    ) -> int | None:
        if isinstance(
            raw_value,
            app_commands.Choice,
        ):
            raw_value = raw_value.value

        value = str(
            raw_value or ""
        ).strip()

        if (
            value.startswith("<#")
            and value.endswith(">")
        ):
            value = value[2:-1]

        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None

        if parsed <= 0:
            return None

        return parsed

    @staticmethod
    def _get_message_channel(
        guild: discord.Guild,
        channel_id: int,
    ) -> (
        discord.TextChannel
        | discord.Thread
        | None
    ):
        get_channel_or_thread = getattr(
            guild,
            "get_channel_or_thread",
            None,
        )

        if callable(get_channel_or_thread):
            channel = get_channel_or_thread(
                channel_id
            )
        else:
            channel = (
                guild.get_channel(channel_id)
                or guild.get_thread(channel_id)
            )

        if isinstance(
            channel,
            MESSAGE_CHANNEL_TYPES,
        ):
            return channel

        return None

    def _resolve_message_target(
        self,
        *,
        guild_id: int,
        channel_id: int,
        ping_everyone: bool,
    ) -> tuple[
        tuple[
            discord.Guild,
            discord.TextChannel | discord.Thread,
        ]
        | None,
        str | None,
    ]:
        if guild_id not in all_guild_ids(self.bot):
            return (
                None,
                "That server is not configured "
                "for this bot.",
            )

        guild = self.bot.get_guild(guild_id)

        if guild is None:
            return (
                None,
                "The bot is not connected "
                "to that server.",
            )

        channel = self._get_message_channel(
            guild,
            channel_id,
        )

        if channel is None:
            return (
                None,
                "That text channel was not found "
                "in the selected server.",
            )

        bot_member = guild.me

        if bot_member is None:
            return (
                None,
                "The bot member could not be found "
                "in that server.",
            )

        permissions = channel.permissions_for(
            bot_member
        )

        if not permissions.view_channel:
            return (
                None,
                "The bot cannot view that channel.",
            )

        if isinstance(
            channel,
            discord.Thread,
        ):
            if not permissions.send_messages_in_threads:
                return (
                    None,
                    "The bot cannot send messages "
                    "in that thread.",
                )

        elif not permissions.send_messages:
            return (
                None,
                "The bot cannot send messages "
                "in that channel.",
            )

        if (
            ping_everyone
            and not permissions.mention_everyone
        ):
            return (
                None,
                "The bot does not have the "
                "Mention @everyone permission "
                "in that channel.",
            )

        return (
            (
                guild,
                channel,
            ),
            None,
        )

    @council.command(
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
    ):
        log_cmd(
            "council message",
            interaction,
        )

        if not await self._require_admin(
            interaction
        ):
            return

        guild_id = self._parse_discord_id(
            server
        )

        channel_id = self._parse_discord_id(
            channel
        )

        if (
            guild_id is None
            or channel_id is None
        ):
            await interaction.response.send_message(
                "❌ Select a server and channel "
                "from the list, or enter valid "
                "numeric IDs.",
                ephemeral=True,
            )
            return

        ping_everyone = everyone.value == "on"

        target, error_message = (
            self._resolve_message_target(
                guild_id=guild_id,
                channel_id=channel_id,
                ping_everyone=ping_everyone,
            )
        )

        if target is None:
            await interaction.response.send_message(
                f"❌ {error_message}",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            CouncilMessageModal(
                self,
                guild_id=guild_id,
                channel_id=channel_id,
                ping_everyone=ping_everyone,
            )
        )

    @message.autocomplete("server")
    async def message_server_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current_lower = (
            current or ""
        ).lower().strip()

        choices: list[
            app_commands.Choice[str]
        ] = []

        for guild_id in all_guild_ids(self.bot):
            guild = self.bot.get_guild(guild_id)

            guild_name = (
                guild.name
                if guild
                else "Server not cached"
            )

            search_text = (
                f"{guild_name} {guild_id}"
            ).lower()

            if (
                current_lower
                and current_lower not in search_text
            ):
                continue

            choices.append(
                app_commands.Choice(
                    name=(
                        f"{guild_name} — {guild_id}"
                    )[:100],
                    value=str(guild_id),
                )
            )

            if len(choices) >= 25:
                break

        return choices

    @message.autocomplete("channel")
    async def message_channel_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        selected_server = getattr(
            interaction.namespace,
            "server",
            None,
        )

        guild_id = self._parse_discord_id(
            selected_server
        )

        if (
            guild_id is None
            or guild_id
            not in all_guild_ids(self.bot)
        ):
            return []

        guild = self.bot.get_guild(guild_id)

        if guild is None:
            return []

        current_lower = (
            current or ""
        ).lower().strip()

        choices: list[
            app_commands.Choice[str]
        ] = []

        text_channels = sorted(
            guild.text_channels,
            key=lambda text_channel: (
                (
                    text_channel.category.position
                    if text_channel.category
                    else -1
                ),
                text_channel.position,
                text_channel.name.lower(),
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

            if (
                current_lower
                and current_lower not in search_text
            ):
                continue

            choices.append(
                app_commands.Choice(
                    name=(
                        f"#{text_channel.name} — "
                        f"{category_name}"
                    )[:100],
                    value=str(text_channel.id),
                )
            )

            if len(choices) >= 25:
                return choices

        for thread in sorted(
            guild.threads,
            key=lambda guild_thread: (
                guild_thread.name.lower()
            ),
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

            if (
                current_lower
                and current_lower not in search_text
            ):
                continue

            choices.append(
                app_commands.Choice(
                    name=(
                        f"Thread: {thread.name} — "
                        f"#{parent_name}"
                    )[:100],
                    value=str(thread.id),
                )
            )

            if len(choices) >= 25:
                break

        return choices

    # ---------- server/config ----------

    @council.command(
        name="servers",
        description="List configured admin/public servers.",
    )
    async def servers(
        self,
        interaction: discord.Interaction,
    ):
        log_cmd(
            "council servers",
            interaction,
        )

        if not await self._require_admin(
            interaction
        ):
            return

        await ensure_deferred(
            interaction,
            ephemeral=True,
        )

        await interaction.followup.send(
            "\n".join(
                self._configured_server_lines()
            ),
            ephemeral=True,
        )

    # ---------- feature channel control ----------

    @council.command(
        name="feature_channel_add",
        description=(
            "Allow a feature in a channel "
            "on a target server."
        ),
    )
    @app_commands.describe(
        guild_id="Target server ID",
        feature="Feature key to allow",
        channel_id="Target channel ID",
    )
    async def feature_channel_add(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        feature: str,
        channel_id: str,
    ):
        log_cmd(
            "council feature_channel_add",
            interaction,
        )

        if not await self._require_admin(
            interaction
        ):
            return

        await ensure_deferred(
            interaction,
            ephemeral=True,
        )

        try:
            target_guild_id = int(guild_id)
            target_channel_id = int(channel_id)

        except ValueError:
            await interaction.followup.send(
                "Guild ID and channel ID "
                "must be numbers.",
                ephemeral=True,
            )
            return

        feature_key = feature.strip()

        if not feature_key:
            await interaction.followup.send(
                "Feature name cannot be blank.",
                ephemeral=True,
            )
            return

        self.settings.add_feature_channel(
            target_guild_id,
            feature_key,
            target_channel_id,
        )

        await interaction.followup.send(
            f"✅ Feature **{feature_key}** "
            f"allowed in `<#{target_channel_id}>` "
            f"on guild `{target_guild_id}`.",
            ephemeral=True,
        )

    @feature_channel_add.autocomplete(
        "feature"
    )
    async def feature_channel_add_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        current_lower = (
            current or ""
        ).lower()

        matches = [
            feature
            for feature in self._all_feature_keys()
            if current_lower in feature.lower()
        ][:25]

        return [
            app_commands.Choice(
                name=feature,
                value=feature,
            )
            for feature in matches
        ]

    @council.command(
        name="feature_channel_remove",
        description=(
            "Remove a feature from a channel "
            "on a target server."
        ),
    )
    @app_commands.describe(
        guild_id="Target server ID",
        feature="Feature key to remove",
        channel_id="Target channel ID",
    )
    async def feature_channel_remove(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        feature: str,
        channel_id: str,
    ):
        log_cmd(
            "council feature_channel_remove",
            interaction,
        )

        if not await self._require_admin(
            interaction
        ):
            return

        await ensure_deferred(
            interaction,
            ephemeral=True,
        )

        try:
            target_guild_id = int(guild_id)
            target_channel_id = int(channel_id)

        except ValueError:
            await interaction.followup.send(
                "Guild ID and channel ID "
                "must be numbers.",
                ephemeral=True,
            )
            return

        feature_key = feature.strip()

        if not feature_key:
            await interaction.followup.send(
                "Feature name cannot be blank.",
                ephemeral=True,
            )
            return

        self.settings.remove_feature_channel(
            target_guild_id,
            feature_key,
            target_channel_id,
        )

        await interaction.followup.send(
            f"✅ Feature **{feature_key}** "
            f"removed from `<#{target_channel_id}>` "
            f"on guild `{target_guild_id}`.",
            ephemeral=True,
        )

    @feature_channel_remove.autocomplete(
        "feature"
    )
    async def feature_channel_remove_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        current_lower = (
            current or ""
        ).lower()

        matches = [
            feature
            for feature in self._all_feature_keys()
            if current_lower in feature.lower()
        ][:25]

        return [
            app_commands.Choice(
                name=feature,
                value=feature,
            )
            for feature in matches
        ]

    @council.command(
        name="feature_channels",
        description=(
            "List feature channels "
            "for a target server."
        ),
    )
    @app_commands.describe(
        guild_id="Target server ID"
    )
    async def feature_channels(
        self,
        interaction: discord.Interaction,
        guild_id: str,
    ):
        log_cmd(
            "council feature_channels",
            interaction,
        )

        if not await self._require_admin(
            interaction
        ):
            return

        await ensure_deferred(
            interaction,
            ephemeral=True,
        )

        try:
            target_guild_id = int(guild_id)

        except ValueError:
            await interaction.followup.send(
                "Guild ID must be a number.",
                ephemeral=True,
            )
            return

        guild = self.bot.get_guild(
            target_guild_id
        )

        guild_name = (
            guild.name
            if guild
            else target_guild_id
        )

        lines = [
            f"__**Feature channels for "
            f"{guild_name}**__"
        ]

        for feature in self._all_feature_keys(
            target_guild_id
        ):
            channel_ids = (
                self.settings.feature_channels(
                    target_guild_id,
                    feature,
                )
            )

            if not channel_ids:
                lines.append(
                    f"- **{feature}**: _(none)_"
                )
                continue

            mentions: list[str] = []

            for channel_id in channel_ids:
                channel = (
                    guild.get_channel(channel_id)
                    if guild
                    else None
                )

                mentions.append(
                    channel.mention
                    if channel
                    else f"`{channel_id}`"
                )

            lines.append(
                f"- **{feature}**: "
                + ", ".join(mentions)
            )

        await interaction.followup.send(
            "\n".join(lines),
            ephemeral=True,
        )

    # ---------- sync ----------

    @council.command(
        name="sync",
        description=(
            "Sync slash commands "
            "to configured servers."
        ),
    )
    @app_commands.describe(
        scope="all, admin, public, or current"
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(
                name="all",
                value="all",
            ),
            app_commands.Choice(
                name="admin",
                value="admin",
            ),
            app_commands.Choice(
                name="public",
                value="public",
            ),
            app_commands.Choice(
                name="current",
                value="current",
            ),
        ]
    )
    async def sync_cmd(
        self,
        interaction: discord.Interaction,
        scope: str = "all",
    ):
        log_cmd(
            "council sync",
            interaction,
        )

        if not await self._require_admin(
            interaction
        ):
            return

        await ensure_deferred(
            interaction,
            ephemeral=True,
        )

        config = (
            getattr(
                self.bot,
                "hot_config",
                {},
            )
            or {}
        )

        admin_id = int(
            config.get("admin_guild_id")
            or 0
        )

        public_ids = [
            int(guild_id)
            for guild_id in config.get(
                "public_guild_ids",
                [],
            )
        ]

        if scope == "admin":
            targets = [admin_id]

        elif scope == "public":
            targets = public_ids

        elif scope == "current":
            targets = (
                [interaction.guild_id]
                if interaction.guild_id
                else []
            )

        else:
            targets = all_guild_ids(self.bot)

        targets = [
            guild_id
            for guild_id in targets
            if guild_id
        ]

        if not targets:
            await interaction.followup.send(
                "No target guilds "
                "for that sync scope.",
                ephemeral=True,
            )
            return

        lines: list[str] = []

        clear_global = getattr(
            self.bot,
            "clear_global_slash_commands",
            None,
        )

        if callable(clear_global):
            cleared = await clear_global()

            if cleared:
                lines.append(
                    "Cleared old global command(s): "
                    + ", ".join(
                        f"`/{name}`"
                        for name in cleared
                    )
                )
            else:
                lines.append(
                    "Global command list clear checked: "
                    "none registered."
                )

        for guild_id in targets:
            synced = await self.bot.tree.sync(
                guild=discord.Object(
                    id=guild_id
                )
            )

            names = ", ".join(
                sorted(
                    command.name
                    for command in synced
                )
            )

            lines.append(
                f"`{guild_id}` — "
                f"{len(synced)} command(s): "
                f"{names or '(none)'}"
            )

        await interaction.followup.send(
            "Synced:\n" + "\n".join(lines),
            ephemeral=True,
        )

    # ---------- furnace admin helpers ----------

    @council.command(
        name="furnace_reference_check",
        description=(
            "Show loaded furnace "
            "reference metadata."
        ),
    )
    async def furnace_reference_check(
        self,
        interaction: discord.Interaction,
    ):
        log_cmd(
            "council furnace_reference_check",
            interaction,
        )

        if not await self._require_admin(
            interaction
        ):
            return

        await ensure_deferred(
            interaction,
            ephemeral=True,
        )

        cog = self.bot.get_cog(
            "WOSFurnaceCalculator"
        )

        if cog is None:
            await interaction.followup.send(
                "Furnace cog is not loaded.",
                ephemeral=True,
            )
            return

        try:
            package_names = []

            for entry in cog.upgrades["levels"]:
                if entry.get("packages"):
                    package_names = list(
                        entry["packages"].keys()
                    )
                    break

            tier_lines = [
                f"{tier['name']}: attempts "
                f"{tier['min_attempt']}-"
                f"{tier['max_attempt']} | "
                f"FC/refine "
                f"{tier['fire_crystal_cost']}"
                for tier in cog.refines["tiers"][:10]
            ]

            embed = cog._base_embed(
                title="WoS Furnace Reference Check"
            )

            embed.add_field(
                name="Levels loaded",
                value=str(
                    len(cog.upgrades["levels"])
                ),
                inline=True,
            )

            embed.add_field(
                name="Packages",
                value=(
                    ", ".join(package_names)
                    if package_names
                    else "None"
                ),
                inline=True,
            )

            embed.add_field(
                name="Refine tiers",
                value=str(
                    len(cog.refines["tiers"])
                ),
                inline=True,
            )

            embed.add_field(
                name="Level range",
                value=(
                    f"{cog.upgrades['levels'][0]['level']}"
                    f" → "
                    f"{cog.upgrades['levels'][-1]['level']}"
                ),
                inline=False,
            )

            embed.add_field(
                name="Refine tiers detail",
                value="\n".join(tier_lines),
                inline=False,
            )

            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
            )

        except Exception as exc:
            await interaction.followup.send(
                f"❌ {exc}",
                ephemeral=True,
            )

    @council.command(
        name="furnace_reference_reload",
        description=(
            "Reload furnace reference JSON files."
        ),
    )
    async def furnace_reference_reload(
        self,
        interaction: discord.Interaction,
    ):
        log_cmd(
            "council furnace_reference_reload",
            interaction,
        )

        if not await self._require_admin(
            interaction
        ):
            return

        await ensure_deferred(
            interaction,
            ephemeral=True,
        )

        cog = self.bot.get_cog(
            "WOSFurnaceCalculator"
        )

        if cog is None:
            await interaction.followup.send(
                "Furnace cog is not loaded.",
                ephemeral=True,
            )
            return

        try:
            cog.load_reference_files()
            cog.profile_cache.clear()

            await interaction.followup.send(
                "✅ Reloaded furnace reference "
                "files and cleared profile cache.",
                ephemeral=True,
            )

        except Exception as exc:
            await interaction.followup.send(
                f"❌ Reload failed: {exc}",
                ephemeral=True,
            )

    @council.command(
        name="furnace_post_help",
        description=(
            "Post the furnace help sheet "
            "into a target channel."
        ),
    )
    @app_commands.describe(
        guild_id="Target server ID",
        channel_id="Target channel ID",
    )
    async def furnace_post_help(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        channel_id: str,
    ):
        log_cmd(
            "council furnace_post_help",
            interaction,
        )

        if not await self._require_admin(
            interaction
        ):
            return

        await ensure_deferred(
            interaction,
            ephemeral=True,
        )

        cog = self.bot.get_cog(
            "WOSFurnaceCalculator"
        )

        if cog is None:
            await interaction.followup.send(
                "Furnace cog is not loaded.",
                ephemeral=True,
            )
            return

        try:
            target_guild_id = int(guild_id)
            target_channel_id = int(channel_id)

        except ValueError:
            await interaction.followup.send(
                "Guild ID and channel ID "
                "must be numbers.",
                ephemeral=True,
            )
            return

        try:
            channel = (
                self.bot.get_channel(
                    target_channel_id
                )
                or await self.bot.fetch_channel(
                    target_channel_id
                )
            )

            if not isinstance(
                channel,
                (
                    discord.TextChannel,
                    discord.Thread,
                ),
            ):
                raise RuntimeError(
                    "Target is not a "
                    "text channel/thread."
                )

            await channel.send(
                embeds=cog._build_help_embeds()
            )

            await interaction.followup.send(
                f"✅ Posted furnace help to "
                f"`{target_channel_id}` on "
                f"`{target_guild_id}`.",
                ephemeral=True,
            )

        except Exception as exc:
            await interaction.followup.send(
                f"❌ {exc}",
                ephemeral=True,
            )

    # ---------- suggestion admin helpers ----------

    @council.command(
        name="suggestion_close",
        description=(
            "Close an active suggestion poll "
            "by server/channel ID."
        ),
    )
    @app_commands.describe(
        guild_id="Target server ID",
        channel_id="Poll channel ID",
    )
    async def suggestion_close(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        channel_id: str,
    ):
        log_cmd(
            "council suggestion_close",
            interaction,
        )

        if not await self._require_admin(
            interaction
        ):
            return

        await ensure_deferred(
            interaction,
            ephemeral=True,
        )

        cog = self.bot.get_cog(
            "SuggestionPollCog"
        )

        if cog is None:
            await interaction.followup.send(
                "Suggestion poll cog is not loaded.",
                ephemeral=True,
            )
            return

        try:
            target_guild_id = int(guild_id)
            target_channel_id = int(channel_id)

        except ValueError:
            await interaction.followup.send(
                "Guild ID and channel ID "
                "must be numbers.",
                ephemeral=True,
            )
            return

        active = cog.get_open_poll_for_channel(
            target_guild_id,
            target_channel_id,
        )

        if not active:
            await interaction.followup.send(
                "No open suggestion poll "
                "in that channel.",
                ephemeral=True,
            )
            return

        poll_id, _ = active

        await cog.close_poll(
            poll_id,
            post_result=True,
        )

        await interaction.followup.send(
            f"Closed suggestion poll `{poll_id}`.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    cog = AdminCog(bot)
    bind_admin_cog(cog, bot)
    await bot.add_cog(cog)