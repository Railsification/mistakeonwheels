# cogs/admin.py
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import all_guild_ids, bind_admin_cog
from core.logger import log_cmd
from core.settings import SettingsManager, FEATURE_KEYS
from core.utils import ensure_deferred


class AdminCog(commands.Cog):
    council = app_commands.Group(name="council", description="Admin/test-server bot controls")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

    # ---------- checks ----------

    def _is_admin_guild(self, interaction: discord.Interaction) -> bool:
        admin_guild_id = int((getattr(self.bot, "hot_config", {}) or {}).get("admin_guild_id", 0) or 0)
        return interaction.guild_id == admin_guild_id

    def _has_admin_role(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        role_names = set((getattr(self.bot, "hot_config", {}) or {}).get("admin_role_names", []))
        if not role_names:
            return False
        return any(role.name in role_names for role in member.roles)

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if not self._is_admin_guild(interaction):
            await interaction.response.send_message("Admin commands only work in the admin/test server.", ephemeral=True)
            return False
        if not self._has_admin_role(interaction):
            await interaction.response.send_message("Nope. Admin role only.", ephemeral=True)
            return False
        return True

    def _all_feature_keys(self, guild_id: int | None = None) -> list[str]:
        keys: list[str] = []
        for attr in ("all_feature_keys", "feature_keys", "get_feature_keys", "list_feature_keys"):
            fn = getattr(self.settings, attr, None)
            if callable(fn):
                try:
                    result = fn(guild_id) if guild_id is not None else fn()
                except TypeError:
                    result = fn()
                if result:
                    keys.extend(str(x).strip() for x in result if str(x).strip())
        keys.extend(str(x).strip() for x in FEATURE_KEYS if str(x).strip())
        seen = set()
        out: list[str] = []
        for key in keys:
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def _configured_server_lines(self) -> list[str]:
        cfg = getattr(self.bot, "hot_config", {}) or {}
        admin_id = int(cfg.get("admin_guild_id") or 0)
        public_ids = [int(x) for x in cfg.get("public_guild_ids", [])]
        lines: list[str] = []
        for gid in all_guild_ids(self.bot):
            guild = self.bot.get_guild(gid)
            name = guild.name if guild else "not cached yet"
            tag = "admin/test" if gid == admin_id else "public"
            if gid in public_ids and gid == admin_id:
                tag = "admin/test + public"
            lines.append(f"`{gid}` — **{name}** ({tag})")
        return lines or ["No configured guild IDs."]

    # ---------- server/config ----------

    @council.command(name="servers", description="List configured admin/public servers.")
    async def servers(self, interaction: discord.Interaction):
        log_cmd("council servers", interaction)
        if not await self._require_admin(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        await interaction.followup.send("\n".join(self._configured_server_lines()), ephemeral=True)

    # ---------- feature channel control ----------

    @council.command(name="feature_channel_add", description="Allow a feature in a channel on a target server.")
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
        log_cmd("council feature_channel_add", interaction)
        if not await self._require_admin(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        try:
            target_guild_id = int(guild_id)
            target_channel_id = int(channel_id)
        except ValueError:
            await interaction.followup.send("Guild ID and channel ID must be numbers.", ephemeral=True)
            return

        feature_key = feature.strip()
        if not feature_key:
            await interaction.followup.send("Feature name cannot be blank.", ephemeral=True)
            return

        self.settings.add_feature_channel(target_guild_id, feature_key, target_channel_id)
        await interaction.followup.send(
            f"✅ Feature **{feature_key}** allowed in `<#{target_channel_id}>` on guild `{target_guild_id}`.",
            ephemeral=True,
        )

    @feature_channel_add.autocomplete("feature")
    async def feature_channel_add_autocomplete(self, interaction: discord.Interaction, current: str):
        current_lower = (current or "").lower()
        matches = [f for f in self._all_feature_keys() if current_lower in f.lower()][:25]
        return [app_commands.Choice(name=f, value=f) for f in matches]

    @council.command(name="feature_channel_remove", description="Remove a feature from a channel on a target server.")
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
        log_cmd("council feature_channel_remove", interaction)
        if not await self._require_admin(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        try:
            target_guild_id = int(guild_id)
            target_channel_id = int(channel_id)
        except ValueError:
            await interaction.followup.send("Guild ID and channel ID must be numbers.", ephemeral=True)
            return

        feature_key = feature.strip()
        if not feature_key:
            await interaction.followup.send("Feature name cannot be blank.", ephemeral=True)
            return

        self.settings.remove_feature_channel(target_guild_id, feature_key, target_channel_id)
        await interaction.followup.send(
            f"✅ Feature **{feature_key}** removed from `<#{target_channel_id}>` on guild `{target_guild_id}`.",
            ephemeral=True,
        )

    @feature_channel_remove.autocomplete("feature")
    async def feature_channel_remove_autocomplete(self, interaction: discord.Interaction, current: str):
        current_lower = (current or "").lower()
        matches = [f for f in self._all_feature_keys() if current_lower in f.lower()][:25]
        return [app_commands.Choice(name=f, value=f) for f in matches]

    @council.command(name="feature_channels", description="List feature channels for a target server.")
    @app_commands.describe(guild_id="Target server ID")
    async def feature_channels(self, interaction: discord.Interaction, guild_id: str):
        log_cmd("council feature_channels", interaction)
        if not await self._require_admin(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        try:
            target_guild_id = int(guild_id)
        except ValueError:
            await interaction.followup.send("Guild ID must be a number.", ephemeral=True)
            return

        guild = self.bot.get_guild(target_guild_id)
        lines = [f"__**Feature channels for {guild.name if guild else target_guild_id}**__"]
        for feature in self._all_feature_keys(target_guild_id):
            ids = self.settings.feature_channels(target_guild_id, feature)
            if not ids:
                lines.append(f"- **{feature}**: _(none)_")
                continue
            mentions = []
            for cid in ids:
                channel = guild.get_channel(cid) if guild else None
                mentions.append(channel.mention if channel else f"`{cid}`")
            lines.append(f"- **{feature}**: " + ", ".join(mentions))

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ---------- sync ----------

    @council.command(name="sync", description="Sync slash commands to configured servers.")
    @app_commands.describe(scope="all, admin, public, or current")
    @app_commands.choices(scope=[
        app_commands.Choice(name="all", value="all"),
        app_commands.Choice(name="admin", value="admin"),
        app_commands.Choice(name="public", value="public"),
        app_commands.Choice(name="current", value="current"),
    ])
    async def sync_cmd(self, interaction: discord.Interaction, scope: str = "all"):
        log_cmd("council sync", interaction)
        if not await self._require_admin(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        cfg = getattr(self.bot, "hot_config", {}) or {}
        admin_id = int(cfg.get("admin_guild_id") or 0)
        public_ids = [int(x) for x in cfg.get("public_guild_ids", [])]

        if scope == "admin":
            targets = [admin_id]
        elif scope == "public":
            targets = public_ids
        elif scope == "current":
            targets = [interaction.guild_id] if interaction.guild_id else []
        else:
            targets = all_guild_ids(self.bot)

        targets = [gid for gid in targets if gid]
        if not targets:
            await interaction.followup.send("No target guilds for that sync scope.", ephemeral=True)
            return

        lines = []

        clear_global = getattr(self.bot, "clear_global_slash_commands", None)
        if callable(clear_global):
            cleared = await clear_global()
            if cleared:
                lines.append("Cleared old global command(s): " + ", ".join(f"`/{name}`" for name in cleared))
            else:
                lines.append("Global command list clear checked: none registered.")

        for gid in targets:
            synced = await self.bot.tree.sync(guild=discord.Object(id=gid))
            names = ", ".join(sorted(c.name for c in synced))
            lines.append(f"`{gid}` — {len(synced)} command(s): {names or '(none)'}")

        await interaction.followup.send("Synced:\n" + "\n".join(lines), ephemeral=True)


    # ---------- furnace admin helpers ----------

    @council.command(name="furnace_reference_check", description="Show loaded furnace reference metadata.")
    async def furnace_reference_check(self, interaction: discord.Interaction):
        log_cmd("council furnace_reference_check", interaction)
        if not await self._require_admin(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        cog = self.bot.get_cog("WOSFurnaceCalculator")
        if cog is None:
            await interaction.followup.send("Furnace cog is not loaded.", ephemeral=True)
            return
        try:
            package_names = []
            for entry in cog.upgrades["levels"]:
                if entry.get("packages"):
                    package_names = list(entry["packages"].keys())
                    break
            tier_lines = [
                f"{tier['name']}: attempts {tier['min_attempt']}-{tier['max_attempt']} | FC/refine {tier['fire_crystal_cost']}"
                for tier in cog.refines["tiers"][:10]
            ]
            embed = cog._base_embed(title="WoS Furnace Reference Check")
            embed.add_field(name="Levels loaded", value=str(len(cog.upgrades["levels"])), inline=True)
            embed.add_field(name="Packages", value=", ".join(package_names) if package_names else "None", inline=True)
            embed.add_field(name="Refine tiers", value=str(len(cog.refines["tiers"])), inline=True)
            embed.add_field(
                name="Level range",
                value=f"{cog.upgrades['levels'][0]['level']} → {cog.upgrades['levels'][-1]['level']}",
                inline=False,
            )
            embed.add_field(name="Refine tiers detail", value="\n".join(tier_lines), inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @council.command(name="furnace_reference_reload", description="Reload furnace reference JSON files.")
    async def furnace_reference_reload(self, interaction: discord.Interaction):
        log_cmd("council furnace_reference_reload", interaction)
        if not await self._require_admin(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        cog = self.bot.get_cog("WOSFurnaceCalculator")
        if cog is None:
            await interaction.followup.send("Furnace cog is not loaded.", ephemeral=True)
            return
        try:
            cog.load_reference_files()
            cog.profile_cache.clear()
            await interaction.followup.send("✅ Reloaded furnace reference files and cleared profile cache.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ Reload failed: {exc}", ephemeral=True)

    @council.command(name="furnace_post_help", description="Post the furnace help sheet into a target channel.")
    @app_commands.describe(guild_id="Target server ID", channel_id="Target channel ID")
    async def furnace_post_help(self, interaction: discord.Interaction, guild_id: str, channel_id: str):
        log_cmd("council furnace_post_help", interaction)
        if not await self._require_admin(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        cog = self.bot.get_cog("WOSFurnaceCalculator")
        if cog is None:
            await interaction.followup.send("Furnace cog is not loaded.", ephemeral=True)
            return
        try:
            target_guild_id = int(guild_id)
            target_channel_id = int(channel_id)
        except ValueError:
            await interaction.followup.send("Guild ID and channel ID must be numbers.", ephemeral=True)
            return
        try:
            channel = self.bot.get_channel(target_channel_id) or await self.bot.fetch_channel(target_channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                raise RuntimeError("Target is not a text channel/thread.")
            await channel.send(embeds=cog._build_help_embeds())
            await interaction.followup.send(f"✅ Posted furnace help to `{target_channel_id}` on `{target_guild_id}`.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    # ---------- suggestion admin helpers ----------

    @council.command(name="suggestion_close", description="Close an active suggestion poll by server/channel ID.")
    @app_commands.describe(guild_id="Target server ID", channel_id="Poll channel ID")
    async def suggestion_close(self, interaction: discord.Interaction, guild_id: str, channel_id: str):
        log_cmd("council suggestion_close", interaction)
        if not await self._require_admin(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        cog = self.bot.get_cog("SuggestionPollCog")
        if cog is None:
            await interaction.followup.send("Suggestion poll cog is not loaded.", ephemeral=True)
            return
        try:
            target_guild_id = int(guild_id)
            target_channel_id = int(channel_id)
        except ValueError:
            await interaction.followup.send("Guild ID and channel ID must be numbers.", ephemeral=True)
            return
        active = cog.get_open_poll_for_channel(target_guild_id, target_channel_id)
        if not active:
            await interaction.followup.send("No open suggestion poll in that channel.", ephemeral=True)
            return
        poll_id, _ = active
        await cog.close_poll(poll_id, post_result=True)
        await interaction.followup.send(f"Closed suggestion poll `{poll_id}`.", ephemeral=True)


async def setup(bot: commands.Bot):
    cog = AdminCog(bot)
    bind_admin_cog(cog, bot)
    await bot.add_cog(cog)
