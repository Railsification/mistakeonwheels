from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.logger import log_cmd
from core.settings import SettingsManager
from core.utils import ensure_deferred

GAMES = [
    ("Hangman", "hangman"),
    ("Connect 4", "connect4"),
    ("Tic Tac Toe", "tictactoe"),
]

GAME_NAMES = {key: name for name, key in GAMES}
TWO_PLAYER_GAMES = {"connect4", "tictactoe"}


def game_label(key: str | None) -> str:
    if not key:
        return "None"
    return GAME_NAMES.get(key, key)


class GameSelect(discord.ui.Select):
    def __init__(self, allowed_games: list[str]):
        options = [
            discord.SelectOption(
                label=name,
                value=key,
                description=f"Play {name}",
            )
            for name, key in GAMES
            if key in allowed_games
        ]

        super().__init__(
            placeholder="Choose a game...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        view.selected_game = self.values[0]
        await view.refresh(interaction)


class OpponentSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Pick an opponent (not needed for Hangman)...",
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        view.opponent_id = self.values[0].id
        await view.refresh(interaction)


class GamesView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        author_id: int,
        guild_id: int,
        channel_id: int,
        allowed_games: list[str],
    ):
        super().__init__(timeout=300)
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self.author_id = int(author_id)
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.allowed_games = list(allowed_games)
        self.selected_game: str | None = None
        self.opponent_id: int | None = None

        self.add_item(GameSelect(self.allowed_games))
        self.add_item(OpponentSelect())
        self.add_item(StartButton())
        self.add_item(CloseButton())

    def disable_all(self):
        for child in self.children:
            child.disabled = True

    def render_content(self) -> str:
        game = game_label(self.selected_game)

        if self.selected_game == "hangman":
            opponent_line = (
                "Opponent: _not needed — the whole channel plays_"
            )
            ready = "✅"
        else:
            opponent = (
                f"<@{self.opponent_id}>"
                if self.opponent_id
                else "_(none)_"
            )
            opponent_line = f"Opponent: {opponent}"
            ready = (
                "✅"
                if self.selected_game and self.opponent_id
                else "❌"
            )

        available = ", ".join(
            game_label(key) for key in self.allowed_games
        )

        return (
            "🎮 **Games Menu**\n"
            f"Available here: **{available}**\n"
            "Pick a game, then press **Start**.\n"
            f"{opponent_line}\n\n"
            f"{ready} **{game}** — "
            + ("ready." if ready == "✅" else "pick an opponent.")
        )

    async def refresh(self, interaction: discord.Interaction):
        try:
            await interaction.edit_original_response(
                content=self.render_content(),
                view=self,
            )
        except discord.NotFound:
            return

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
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
        super().__init__(
            label="Start",
            style=discord.ButtonStyle.success,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)

        selected = view.selected_game
        if not selected:
            await interaction.followup.send(
                "❌ Pick a game first.",
                ephemeral=True,
            )
            return

        if not interaction.guild:
            await interaction.followup.send(
                "❌ This must be used in a server.",
                ephemeral=True,
            )
            return

        # Re-check when Start is pressed so the menu cannot bypass
        # per-game channel restrictions.
        if not view.settings.is_game_allowed(
            interaction.guild_id,
            interaction.channel_id,
            selected,
        ):
            await interaction.followup.send(
                f"❌ **{game_label(selected)}** is not enabled in this channel.",
                ephemeral=True,
            )
            return

        if selected == "hangman":
            view.disable_all()
            await view.refresh(interaction)

            hangman = interaction.client.get_cog("HangmanCog")
            if hangman is None:
                await interaction.followup.send(
                    "❌ Hangman cog isn’t loaded.",
                    ephemeral=True,
                )
                return

            await hangman.service.start_game(interaction)  # type: ignore[attr-defined]
            return

        if selected in TWO_PLAYER_GAMES and not view.opponent_id:
            await interaction.followup.send(
                "❌ Pick an opponent first.",
                ephemeral=True,
            )
            return

        opponent = (
            interaction.guild.get_member(view.opponent_id)
            if view.opponent_id
            else None
        )

        if opponent is None and view.opponent_id:
            try:
                opponent = await interaction.guild.fetch_member(
                    view.opponent_id
                )
            except Exception:
                opponent = None

        if opponent is None:
            await interaction.followup.send(
                "❌ Couldn’t resolve that opponent.",
                ephemeral=True,
            )
            return

        view.disable_all()
        await view.refresh(interaction)

        if selected == "connect4":
            connect4 = interaction.client.get_cog("Connect4Cog")
            if connect4 is None:
                await interaction.followup.send(
                    "❌ Connect4 cog isn’t loaded.",
                    ephemeral=True,
                )
                return

            await connect4.start_game(interaction, opponent)  # type: ignore[attr-defined]
            return

        if selected == "tictactoe":
            tictactoe = interaction.client.get_cog("TicTacToeCog")
            if tictactoe is None:
                await interaction.followup.send(
                    "❌ TicTacToe cog isn’t loaded.",
                    ephemeral=True,
                )
                return

            await tictactoe.start_game(interaction, opponent)  # type: ignore[attr-defined]
            return

        await interaction.followup.send(
            "❌ Unknown game.",
            ephemeral=True,
        )


class CloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Close",
            style=discord.ButtonStyle.danger,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        view.disable_all()
        await view.refresh(interaction)


class GamesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

    @app_commands.command(
        name="games",
        description="Open the games menu (mobile-friendly)",
    )
    async def games(self, interaction: discord.Interaction):
        log_cmd("games", interaction)

        allowed_games = self.settings.available_games(
            interaction.guild_id,
            interaction.channel_id,
        )

        if not allowed_games:
            await interaction.response.send_message(
                "❌ No games are enabled in this channel.",
                ephemeral=True,
            )
            return

        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "❌ This must be used in a server.",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=True)

        view = GamesView(
            self.bot,
            author_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            allowed_games=allowed_games,
        )
        await interaction.followup.send(
            content=view.render_content(),
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = GamesCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
