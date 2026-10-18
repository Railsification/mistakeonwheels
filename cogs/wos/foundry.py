# cogs/wos/foundry.py
"""Discord UI for two persistent Whiteout Survival Foundry plans."""
from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.foundry_plan import (
    ALL_SLOTS,
    ATTACKER_SLOTS,
    BACKUP_SLOTS,
    BUILDING_NAMES,
    MAIN_SLOTS,
    PHASE_BUILDINGS,
    SUPPORT_SLOTS,
    default_plan,
    phase_lines,
    player_at,
    rename_player,
    set_assignment,
    validate_plan,
    wos_messages,
    wos_length,
)
from core.logger import warn
from core.storage import (
    configured_guild_ids,
    known_guild_dirs,
    load_guild_json,
    save_guild_json,
)

__version__ = "1.0.0"
DATA_FILE = "foundry_plans.json"
FOUNDRY_CHOICES = [
    app_commands.Choice(name="Foundry 1", value=1),
    app_commands.Choice(name="Foundry 2", value=2),
]
COPY_LIMIT_CHOICES = [
    app_commands.Choice(name="200 characters (safest)", value=200),
    app_commands.Choice(name="300 characters", value=300),
    app_commands.Choice(name="400 characters", value=400),
]


def can_edit(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(
        interaction.guild_id
        and permissions
        and (permissions.manage_guild or permissions.administrator)
    )


class BuildingChoice(discord.ui.Select):
    def __init__(self, cog: "FoundryCog", foundry: int, phase: str):
        self.cog = cog
        self.foundry = foundry
        self.phase = phase
        options = [
            discord.SelectOption(label=BUILDING_NAMES[building], value=building)
            for building in PHASE_BUILDINGS[phase]
        ]
        super().__init__(
            placeholder="Select the building to change",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not can_edit(interaction):
            await interaction.response.send_message("Manage Server permission is required.", ephemeral=True)
            return
        building = self.values[0]
        try:
            plan = self.cog.get_plan(int(interaction.guild_id), self.foundry)
            next_view = AttackerChoiceView(self.cog, self.foundry, self.phase, building, plan)
        except (ValueError, OSError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(
            content=(
                f"**Foundry {self.foundry} | {self.phase.title()}**\n"
                f"{BUILDING_NAMES[building]} is currently held by "
                f"**{player_at(plan, self.phase, building)}**.\n"
                "Pick an attacker. If they already hold another building, the two swap."
            ),
            view=next_view,
        )


class BuildingChoiceView(discord.ui.View):
    def __init__(self, cog: "FoundryCog", foundry: int, phase: str):
        super().__init__(timeout=600)
        self.add_item(BuildingChoice(cog, foundry, phase))


class AttackerChoice(discord.ui.Select):
    def __init__(self, cog: "FoundryCog", foundry: int, phase: str, building: str, plan: dict):
        self.cog = cog
        self.foundry = foundry
        self.phase = phase
        self.building = building
        options = []
        for slot in ATTACKER_SLOTS:
            currently_at = next(
                key for key, value in plan[phase].items() if value == slot
            )
            options.append(
                discord.SelectOption(
                    label=plan["roster"][slot],
                    value=slot,
                    description=f"Now: {BUILDING_NAMES[currently_at]}",
                    default=plan[phase][building] == slot,
                )
            )
        super().__init__(
            placeholder="Choose the new attacker",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not can_edit(interaction):
            await interaction.response.send_message("Manage Server permission is required.", ephemeral=True)
            return
        guild_id = int(interaction.guild_id)
        try:
            result = await self.cog.change_assignment(
                guild_id, self.foundry, self.phase, self.building, self.values[0]
            )
        except (OSError, ValueError) as exc:
            await interaction.response.send_message(f"Not saved: {exc}", ephemeral=True)
            return
        await interaction.response.edit_message(content=f"Saved. {result}", view=None)
        if not await self.cog.refresh_board(guild_id, self.foundry):
            await interaction.followup.send(
                "The assignment was saved, but the public board could not be refreshed. "
                "Use `/foundry post` to update it.",
                ephemeral=True,
            )


class AttackerChoiceView(discord.ui.View):
    def __init__(self, cog: "FoundryCog", foundry: int, phase: str, building: str, plan: dict):
        super().__init__(timeout=600)
        self.add_item(AttackerChoice(cog, foundry, phase, building, plan))


class RosterNameModal(discord.ui.Modal):
    def __init__(self, cog: "FoundryCog", foundry: int, slot: str, current_name: str):
        super().__init__(title=f"Foundry {foundry} - Rename Player", timeout=300)
        self.cog = cog
        self.foundry = foundry
        self.slot = slot
        self.player_name = discord.ui.TextInput(
            label="Player name",
            default=current_name,
            max_length=24,
            min_length=1,
            required=True,
            placeholder="Type their in-game name",
        )
        self.add_item(self.player_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not can_edit(interaction):
            await interaction.response.send_message("Manage Server permission is required.", ephemeral=True)
            return
        guild_id = int(interaction.guild_id)
        try:
            previous, new = await self.cog.change_name(
                guild_id, self.foundry, self.slot, str(self.player_name.value)
            )
        except (ValueError, OSError) as exc:
            await interaction.response.send_message(f"Not saved: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Saved for Foundry {self.foundry}: **{previous}** → **{new}**. "
            "Assignments in both phases now show the new name.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if not await self.cog.refresh_board(guild_id, self.foundry):
            await interaction.followup.send(
                "The name was saved, but the public board could not be refreshed. "
                "Use `/foundry post` to update it.",
                ephemeral=True,
            )


class RosterChoice(discord.ui.Select):
    def __init__(self, cog: "FoundryCog", foundry: int, plan: dict):
        self.cog = cog
        self.foundry = foundry
        options = []
        for slot in ALL_SLOTS:
            if slot in MAIN_SLOTS:
                role = "Main attacker"
            elif slot in BACKUP_SLOTS:
                role = "Backup attacker"
            else:
                role = "Support / gathering"
            options.append(
                discord.SelectOption(
                    label=plan["roster"][slot],
                    value=slot,
                    description=f"{role} | Slot {slot.upper()}",
                )
            )
        super().__init__(
            placeholder="Select a player slot to rename",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not can_edit(interaction):
            await interaction.response.send_message("Manage Server permission is required.", ephemeral=True)
            return
        slot = self.values[0]
        try:
            plan = self.cog.get_plan(int(interaction.guild_id), self.foundry)
        except (ValueError, OSError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_modal(
            RosterNameModal(self.cog, self.foundry, slot, plan["roster"][slot])
        )


class RosterChoiceView(discord.ui.View):
    def __init__(self, cog: "FoundryCog", foundry: int, plan: dict):
        super().__init__(timeout=600)
        self.add_item(RosterChoice(cog, foundry, plan))


class FoundryBoardView(discord.ui.View):
    """Persistent public board, or an intentionally short-lived private preview."""

    def __init__(self, cog: "FoundryCog", foundry: int, *, persistent: bool):
        super().__init__(timeout=None if persistent else 900)
        self.cog = cog
        self.foundry = foundry
        self.persistent = persistent

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild_id:
            await interaction.response.send_message("Use this inside a server.", ephemeral=True)
            return False
        if self.persistent and interaction.message:
            record = self.cog.get_board_record(int(interaction.guild_id), self.foundry)
            if not record or int(record.get("message_id") or 0) != interaction.message.id:
                await interaction.response.send_message(
                    "This is an old board. Use `/foundry view` or the current posted board.",
                    ephemeral=True,
                )
                return False
        return True

    @discord.ui.button(label="Edit Opening", style=discord.ButtonStyle.primary, custom_id="foundry:opening")
    async def opening(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._edit_phase(interaction, "opening")

    @discord.ui.button(label="Edit Mid", style=discord.ButtonStyle.primary, custom_id="foundry:mid")
    async def mid(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._edit_phase(interaction, "mid")

    async def _edit_phase(self, interaction: discord.Interaction, phase: str) -> None:
        if not can_edit(interaction):
            await interaction.response.send_message("Manage Server permission is required to edit.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**Foundry {self.foundry}: Choose a {phase} building**",
            view=BuildingChoiceView(self.cog, self.foundry, phase),
            ephemeral=True,
        )

    @discord.ui.button(label="Edit Roster", style=discord.ButtonStyle.secondary, custom_id="foundry:roster")
    async def roster(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not can_edit(interaction):
            await interaction.response.send_message("Manage Server permission is required to edit.", ephemeral=True)
            return
        try:
            plan = self.cog.get_plan(int(interaction.guild_id), self.foundry)
        except (ValueError, OSError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"**Foundry {self.foundry}: Rename one of the 15 player slots**",
            view=RosterChoiceView(self.cog, self.foundry, plan),
            ephemeral=True,
        )

    @discord.ui.button(label="WoS Copy", style=discord.ButtonStyle.success, custom_id="foundry:copy")
    async def copy(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self.cog.send_wos_chunks(interaction, self.foundry, 200)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, custom_id="foundry:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            plan = self.cog.get_plan(int(interaction.guild_id), self.foundry)
        except (ValueError, OSError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if self.persistent:
            await interaction.response.defer(thinking=True, ephemeral=True)
            refreshed = await self.cog.refresh_board(int(interaction.guild_id), self.foundry)
            await interaction.followup.send(
                "Public Foundry board refreshed." if refreshed else "Could not refresh the public board.",
                ephemeral=True,
            )
        else:
            await interaction.response.edit_message(
                embed=self.cog.build_embed(self.foundry, plan), view=self
            )


class FoundryCog(commands.Cog):
    """Foundry 1 and 2 each have their own stored 15-person roster and battle plan."""

    foundry = app_commands.Group(name="foundry", description="Plan Foundry 1 and 2 teams and objectives")

    HELP_META = {
        "title": "Foundry Planner",
        "summary": "Independent Foundry 1/2 rosters and 7-objective plans, with easy swaps and WoS copy text.",
        "details": (
            "Use `/foundry view` for a private preview or `/foundry post` to create/update "
            "a persistent public board. Editors with Manage Server can choose a building then "
            "select an attacker to swap locations, or rename any roster slot. "
            "Use `/foundry copy` to get separately copyable, WoS-sized messages. "
            "Foundry 1 and 2 save independently and survive bot restarts via guild storage."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._registered: dict[int, FoundryBoardView] = {}

    async def cog_load(self) -> None:
        guild_ids = set(known_guild_dirs()) | set(configured_guild_ids(self.bot))
        for guild_id in guild_ids:
            book = self._load_book(guild_id)
            for key, record in book.get("boards", {}).items():
                if key not in ("1", "2") or not isinstance(record, dict):
                    continue
                message_id = int(record.get("message_id") or 0)
                if not message_id:
                    continue
                view = FoundryBoardView(self, int(key), persistent=True)
                try:
                    self.bot.add_view(view, message_id=message_id)
                    self._registered[message_id] = view
                except Exception as exc:
                    warn(f"Could not restore Foundry {key} board {message_id}: {exc!r}")

    async def cog_unload(self) -> None:
        for message_id, view in list(self._registered.items()):
            try:
                self.bot.remove_view(view, message_id=message_id)
            except Exception:
                pass
            view.stop()
        self._registered.clear()

    @staticmethod
    def _load_book(guild_id: int) -> dict:
        book = load_guild_json(guild_id, DATA_FILE, {})
        if not isinstance(book, dict):
            raise ValueError("Saved Foundry data is invalid; no existing data was overwritten.")
        if not isinstance(book.get("plans", {}), dict) or not isinstance(book.get("boards", {}), dict):
            raise ValueError("Saved Foundry plans/boards are invalid; no existing data was overwritten.")
        book.setdefault("plans", {})
        book.setdefault("boards", {})
        return book

    def get_plan(self, guild_id: int, foundry: int) -> dict:
        if foundry not in (1, 2):
            raise ValueError("Foundry must be 1 or 2.")
        book = self._load_book(guild_id)
        key = str(foundry)
        # Previews and exports are read-only. Only edits/public-board creation
        # save the initial template, avoiding concurrent read/write lost updates.
        plan = book["plans"][key] if key in book["plans"] else default_plan()
        validate_plan(plan)
        return plan

    def get_board_record(self, guild_id: int, foundry: int) -> Optional[dict]:
        return self._load_book(guild_id)["boards"].get(str(foundry))

    async def change_assignment(
        self, guild_id: int, foundry: int, phase: str, building: str, slot: str
    ) -> str:
        async with self._lock:
            book = self._load_book(guild_id)
            plan = book["plans"].setdefault(str(foundry), default_plan())
            validate_plan(plan)
            old_slot, previous_building = set_assignment(plan, phase, building, slot)
            save_guild_json(guild_id, DATA_FILE, book)
            player = plan["roster"][slot]
            old_player = plan["roster"][old_slot]
            label = BUILDING_NAMES[building]
            if previous_building == building:
                return f"{player} stays at {label} ({phase})."
            return (
                f"{player} → {label}; {old_player} → "
                f"{BUILDING_NAMES[previous_building]} ({phase})."
            )

    async def change_name(
        self, guild_id: int, foundry: int, slot: str, value: str
    ) -> tuple[str, str]:
        async with self._lock:
            book = self._load_book(guild_id)
            plan = book["plans"].setdefault(str(foundry), default_plan())
            previous, new = rename_player(plan, slot, value)
            save_guild_json(guild_id, DATA_FILE, book)
            return previous, new

    @staticmethod
    def build_embed(foundry: int, plan: dict) -> discord.Embed:
        validate_plan(plan)
        roster = plan["roster"]
        embed = discord.Embed(
            title=f"FOUNDRY {foundry} | 15-player Plan",
            description=(
                "**4 main attackers + 3 backup attackers + 8 support**\n"
                "Capture and defend 7 priority objectives. Edit assignments without rewriting the plan."
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="Attack Team (7)",
            value=(
                "**Main:** " + ", ".join(roster[s] for s in MAIN_SLOTS) + "\n"
                "**Backup:** " + ", ".join(roster[s] for s in BACKUP_SLOTS)
            ),
            inline=False,
        )
        embed.add_field(
            name="Phase 1 - Opening",
            value="\n".join(phase_lines(plan, "opening")),
            inline=False,
        )
        embed.add_field(
            name="Phase 2 - Mid-Game (when center opens)",
            value="\n".join(phase_lines(plan, "mid")),
            inline=False,
        )
        embed.add_field(
            name="8 Support / Late Game",
            value=(
                ", ".join(roster[s] for s in SUPPORT_SLOTS)
                + "\n**No combat.** When Workshop opens, gather resources with remaining marches for points."
            ),
            inline=False,
        )
        embed.set_footer(
            text="Manage Server: Edit Opening / Edit Mid / Edit Roster | Everyone: WoS Copy"
        )
        return embed

    async def refresh_board(self, guild_id: int, foundry: int) -> bool:
        record = self.get_board_record(guild_id, foundry)
        if not isinstance(record, dict):
            return True  # No public board to refresh yet.
        channel_id = int(record.get("channel_id") or 0)
        message_id = int(record.get("message_id") or 0)
        if not channel_id or not message_id:
            return False
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            plan = self.get_plan(guild_id, foundry)
            await message.edit(
                embed=self.build_embed(foundry, plan),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except (discord.DiscordException, ValueError, OSError, AttributeError) as exc:
            warn(f"Foundry {foundry} saved, but board refresh failed: {exc!r}")
            return False

    async def send_wos_chunks(
        self, interaction: discord.Interaction, foundry: int, limit: int
    ) -> None:
        try:
            plan = self.get_plan(int(interaction.guild_id), foundry)
            messages = wos_messages(plan, foundry, limit)
        except (ValueError, OSError) as exc:
            await interaction.followup.send(f"Could not export Foundry {foundry}: {exc}", ephemeral=True)
            return
        for i, message in enumerate(messages, 1):
            await interaction.followup.send(
                f"**Foundry {foundry} | Copy {i}/{len(messages)} ({wos_length(message)}/{limit})**\n"
                f"```text\n{message}\n```",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @foundry.command(name="help", description="How to edit Foundry teams, swap objectives and copy to WoS")
    async def foundry_help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "**Foundry 1 and Foundry 2 are separate plans.**\n"
            "`/foundry view` - privately see the plan with controls.\n"
            "`/foundry post` - create/update the live public board (Manage Server).\n"
            "`/foundry copy` - get numbered, individually copyable WoS messages "
            "(200/300/400-character limit).\n"
            "**Edit Opening / Edit Mid:** choose a building, then an attacker. "
            "If that attacker already holds another building, the two swap.\n"
            "**Edit Roster:** rename any of the 15 slots, including 8 support. "
            "Both phase assignments update automatically.\n"
            "Only members with **Manage Server** may edit; all members may view/copy. "
            "Assignments and public-board controls survive restarts.",
            ephemeral=True,
        )

    @foundry.command(name="view", description="Privately view Foundry 1 or 2 and open editing controls")
    @app_commands.choices(foundry=FOUNDRY_CHOICES)
    async def foundry_view(self, interaction: discord.Interaction, foundry: int) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        try:
            plan = self.get_plan(int(interaction.guild_id), foundry)
        except (ValueError, OSError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=self.build_embed(foundry, plan),
            view=FoundryBoardView(self, foundry, persistent=False),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @foundry.command(name="post", description="Create or update Foundry's live board in this server")
    @app_commands.choices(foundry=FOUNDRY_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    async def foundry_post(self, interaction: discord.Interaction, foundry: int) -> None:
        if not can_edit(interaction):
            await interaction.response.send_message("Manage Server permission is required.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild_id = int(interaction.guild_id)
        try:
            # Serialize posting as well as edits, so two moderators cannot create
            # duplicate boards by running /foundry post at the same time.
            async with self._lock:
                plan = self.get_plan(guild_id, foundry)
                record = self.get_board_record(guild_id, foundry)
                updated_url = None
                # Keep one active board per Foundry, even across channels.
                if isinstance(record, dict) and record.get("message_id") and record.get("channel_id"):
                    try:
                        channel_id = int(record["channel_id"])
                        old_channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                        old_message = await old_channel.fetch_message(int(record["message_id"]))
                        await old_message.edit(
                            embed=self.build_embed(foundry, plan),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        updated_url = old_message.jump_url
                    except discord.NotFound:
                        pass  # Old board deleted; replace it.
                if updated_url is None:
                    channel = interaction.channel
                    if channel is None or not hasattr(channel, "send"):
                        await interaction.followup.send("Cannot post in this channel.", ephemeral=True)
                        return
                    view = FoundryBoardView(self, foundry, persistent=True)
                    message = await channel.send(
                        embed=self.build_embed(foundry, plan),
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    try:
                        book = self._load_book(guild_id)
                        book["plans"].setdefault(str(foundry), plan)
                        book["boards"][str(foundry)] = {
                            "channel_id": int(message.channel.id),
                            "message_id": int(message.id),
                        }
                        save_guild_json(guild_id, DATA_FILE, book)
                    except Exception:
                        # Do not leave an untracked public board when the
                        # persistent message ID could not be saved.
                        try:
                            await message.delete()
                        except discord.DiscordException:
                            warn("Could not clean up an untracked Foundry post")
                        raise
                    previous_id = int(record.get("message_id") or 0) if isinstance(record, dict) else 0
                    if previous_id in self._registered:
                        previous_view = self._registered.pop(previous_id)
                        try:
                            self.bot.remove_view(previous_view, message_id=previous_id)
                        except Exception:
                            pass
                        previous_view.stop()
                    self._registered[message.id] = view
                    result = f"Posted Foundry {foundry} live board: {message.jump_url}"
                else:
                    result = f"Updated the existing Foundry {foundry} board: {updated_url}"
            await interaction.followup.send(result, ephemeral=True)
        except (discord.DiscordException, OSError, ValueError) as exc:
            warn(f"Could not post Foundry {foundry}: {exc!r}")
            await interaction.followup.send(
                f"Could not complete the Foundry {foundry} post: {exc}", ephemeral=True
            )

    @foundry.command(name="copy", description="Export a Foundry plan as separate WoS-sized copy blocks")
    @app_commands.choices(foundry=FOUNDRY_CHOICES, limit=COPY_LIMIT_CHOICES)
    async def foundry_copy(
        self, interaction: discord.Interaction, foundry: int, limit: int = 200
    ) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self.send_wos_chunks(interaction, foundry, limit)


async def setup(bot: commands.Bot) -> None:
    cog = FoundryCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
