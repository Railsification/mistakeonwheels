# cogs/games.py
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.game_registry import discover_games, get_game_entry
from core.game_stats import get_game_catalog
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.utils import ensure_deferred


# Kept for compatibility with any existing imports. These are refreshed from
# the shared registry whenever /games is opened.
GAMES: list[tuple[str, str]] = []
TWO_PLAYER_GAMES: set[str] = set()


def _refresh_compatibility_constants(bot: commands.Bot) -> dict[str, dict[str, Any]]:
    registry = discover_games(bot)
    GAMES[:] = [
        (str(details["label"]), key)
        for key, details in registry.items()
    ]
    TWO_PLAYER_GAMES.clear()
    TWO_PLAYER_GAMES.update(
        key
        for key, details in registry.items()
        if bool(details.get("requires_opponent"))
    )
    return registry


def game_label(key: str | None) -> str:
    if not key:
        return "None"

    wanted = str(key).strip().lower()
    for name, game_key in GAMES:
        if game_key == wanted:
            return name

    details = get_game_catalog().get(wanted)
    if isinstance(details, dict):
        return str(details.get("label") or wanted)
    return wanted


class GameSelect(discord.ui.Select):
    def __init__(self, bot: commands.Bot):
        registry = _refresh_compatibility_constants(bot)
        options: list[discord.SelectOption] = []

        for key, details in registry.items():
            options.append(
                discord.SelectOption(
                    label=str(details["label"])[:100],
                    value=key,
                    description=str(details.get("description") or "Play this game")[:100],
                    emoji=str(details.get("emoji") or "") or None,
                )
            )
            if len(options) >= 25:
                break

        if not options:
            options.append(
                discord.SelectOption(
                    label="No games loaded",
                    value="__none__",
                    description="No playable game cogs were discovered.",
                )
            )

        super().__init__(
            placeholder="Choose a game...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        selected = self.values[0]
        view.selected_game = None if selected == "__none__" else selected
        view.sync_controls()
        await view.refresh(interaction)


class OpponentSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Pick an opponent after choosing a two-player game...",
            min_values=1,
            max_values=1,
            row=1,
            disabled=True,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        view.opponent_id = self.values[0].id
        await view.refresh(interaction)


class GamesView(discord.ui.View):
    def __init__(self, bot: commands.Bot, author_id: int):
        # The private picker expires; started games control their own lifetime.
        super().__init__(timeout=300)
        self.bot = bot
        self.author_id = int(author_id)
        self.selected_game: str | None = None
        self.opponent_id: int | None = None
        self.registry = _refresh_compatibility_constants(bot)
        self.add_item(GameSelect(bot))
        self.add_item(OpponentSelect())
        self.add_item(StartButton())
        self.add_item(CloseButton())
        self.sync_controls()

    def selected_entry(self) -> dict[str, Any] | None:
        if not self.selected_game:
            return None
        entry = get_game_entry(self.bot, self.selected_game)
        if entry is not None:
            self.registry[self.selected_game] = entry
        return entry

    def sync_controls(self) -> None:
        entry = self.registry.get(str(self.selected_game or ""))
        needs_opponent = bool(entry and entry.get("requires_opponent"))

        for child in self.children:
            if isinstance(child, OpponentSelect):
                child.disabled = not needs_opponent
                child.placeholder = (
                    "Pick an opponent..."
                    if needs_opponent
                    else "No opponent needed for this game"
                )

        if not needs_opponent:
            self.opponent_id = None

    def disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    def render_content(self) -> str:
        entry = self.registry.get(str(self.selected_game or ""))
        if entry is None:
            return (
                "🎮 **Games Menu**\n"
                "Choose a game from the dropdown. The list is loaded automatically "
                "from the installed game cogs."
            )

        label = str(entry.get("label") or game_label(self.selected_game))
        if bool(entry.get("requires_opponent")):
            opponent = f"<@{self.opponent_id}>" if self.opponent_id else "_(none)_"
            ready = self.opponent_id is not None
            opponent_line = f"Opponent: {opponent}"
            instruction = "ready." if ready else "pick an opponent."
        else:
            ready = True
            opponent_line = "Opponent: _not needed — the whole channel can play_"
            instruction = "ready."

        return (
            "🎮 **Games Menu**\n"
            "Pick a game, then press **Start**.\n"
            f"{opponent_line}\n\n"
            f"{'✅' if ready else '❌'} **{label}** — {instruction}"
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.edit_original_response(
                content=self.render_content(),
                view=self,
            )
        except discord.NotFound:
            return

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            try:
                await interaction.response.send_message(
                    "❌ This menu isn’t yours.",
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                await interaction.followup.send(
                    "❌ This menu isn’t yours.",
                    ephemeral=True,
                )
            return False
        return True


class StartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Start", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)

        if not view.selected_game:
            await interaction.followup.send("❌ Pick a game first.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.followup.send(
                "❌ This must be used in a server.",
                ephemeral=True,
            )
            return

        entry = view.selected_entry()
        if entry is None:
            await interaction.followup.send(
                "❌ That game cog is no longer loaded.",
                ephemeral=True,
            )
            return

        opponent: discord.Member | None = None
        if bool(entry.get("requires_opponent")):
            if not view.opponent_id:
                await interaction.followup.send(
                    "❌ Pick an opponent first.",
                    ephemeral=True,
                )
                return

            opponent = interaction.guild.get_member(view.opponent_id)
            if opponent is None:
                try:
                    opponent = await interaction.guild.fetch_member(view.opponent_id)
                except Exception:
                    opponent = None
            if opponent is None:
                await interaction.followup.send(
                    "❌ Couldn’t resolve that opponent.",
                    ephemeral=True,
                )
                return

        launcher = entry.get("launcher")
        if not callable(launcher):
            await interaction.followup.send(
                "❌ That game has no valid start method.",
                ephemeral=True,
            )
            return

        view.disable_all()
        await view.refresh(interaction)

        try:
            if bool(entry.get("requires_opponent")):
                await launcher(interaction, opponent)
            else:
                await launcher(interaction)
        except Exception as exc:
            warn(
                f"Dynamic game launch failed for {view.selected_game}: {exc!r}"
            )
            await interaction.followup.send(
                "❌ The game failed to start. Check the Railway log.",
                ephemeral=True,
            )


class CloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Close", style=discord.ButtonStyle.danger, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        view.disable_all()
        await view.refresh(interaction)


class GamesCog(commands.Cog):
    HELP_META = {
        "title": "Games Menu",
        "summary": "Automatically lists every loaded game cog in one menu.",
        "details": "Use /games, choose a game, select an opponent when required, then press Start.",
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

    @app_commands.command(
        name="games",
        description="Open the automatically generated games menu",
    )
    async def games(self, interaction: discord.Interaction) -> None:
        log_cmd("games", interaction)
        if not self.settings.is_feature_allowed(
            interaction.guild_id,
            interaction.channel_id,
            "games",
        ):
            await interaction.response.send_message(
                "❌ `/games` can only be used in the configured game channel(s).",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=True)
        view = GamesView(self.bot, author_id=interaction.user.id)
        await interaction.followup.send(
            content=view.render_content(),
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = GamesCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
