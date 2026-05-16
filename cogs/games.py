# cogs/games.py
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.logger import log_cmd
from core.utils import ensure_deferred
from core.settings import SettingsManager


GAMES = [
    ("Connect 4", "connect4"),
    ("Tic Tac Toe", "tictactoe"),
]


def game_label(key: str | None) -> str:
    if not key:
        return "None"
    for name, k in GAMES:
        if k == key:
            return name
    return key


class GameSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose a game...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=name, value=key, description=f"Play {name}")
                for name, key in GAMES
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: GamesView = self.view  # type: ignore
        await interaction.response.defer(ephemeral=True)
        view.selected_game = self.values[0]
        await view.refresh(interaction)


class OpponentSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Pick an opponent...", min_values=1, max_values=1, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: GamesView = self.view  # type: ignore
        await interaction.response.defer(ephemeral=True)
        view.opponent_id = self.values[0].id
        await view.refresh(interaction)


class GamesView(discord.ui.View):
    def __init__(self, bot: commands.Bot, author_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.author_id = author_id

        self.selected_game: str | None = None
        self.opponent_id: int | None = None

        self.add_item(GameSelect())
        self.add_item(OpponentSelect())
        self.add_item(StartButton())
        self.add_item(CloseButton())

    def disable_all(self):
        for c in self.children:
            c.disabled = True

    def render_content(self) -> str:
        opp = f"<@{self.opponent_id}>" if self.opponent_id else "_(none)_"
        game = game_label(self.selected_game)
        ready = "✅" if self.selected_game else "❌"
        return (
            "🎮 **Games Menu**\n"
            "Pick a game + opponent, then press **Start**.\n"
            f"Opponent: {opp}\n\n"
            f"{ready} **{game}** — ready."
        )

    async def refresh(self, interaction: discord.Interaction):
        # This edits the ephemeral message reliably after defer()
        try:
            await interaction.edit_original_response(content=self.render_content(), view=self)
        except discord.NotFound:
            return

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            try:
                await interaction.response.send_message("❌ This menu isn’t yours.", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send("❌ This menu isn’t yours.", ephemeral=True)
            return False
        return True


class StartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Start", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: GamesView = self.view  # type: ignore

        # ACK FAST
        await interaction.response.defer(ephemeral=True)

        if not view.selected_game:
            await interaction.followup.send("❌ Pick a game first.", ephemeral=True)
            return
        if not view.opponent_id:
            await interaction.followup.send("❌ Pick an opponent first.", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.followup.send("❌ This must be used in a server.", ephemeral=True)
            return

        # Resolve opponent
        opponent = interaction.guild.get_member(view.opponent_id)
        if opponent is None:
            try:
                opponent = await interaction.guild.fetch_member(view.opponent_id)
            except Exception:
                opponent = None
        if opponent is None:
            await interaction.followup.send("❌ Couldn’t resolve that opponent.", ephemeral=True)
            return

        # Close menu UI
        view.disable_all()
        await view.refresh(interaction)

        # Start the selected game
        if view.selected_game == "connect4":
            c4 = interaction.client.get_cog("Connect4Cog")
            if c4 is None:
                await interaction.followup.send("❌ Connect4 cog isn’t loaded.", ephemeral=True)
                return
            await c4.start_game(interaction, opponent)  # type: ignore
            return

        if view.selected_game == "tictactoe":
            ttt = interaction.client.get_cog("TicTacToeCog")
            if ttt is None:
                await interaction.followup.send("❌ TicTacToe cog isn’t loaded.", ephemeral=True)
                return
            await ttt.start_game(interaction, opponent)  # type: ignore
            return

        await interaction.followup.send("❌ Unknown game.", ephemeral=True)


class CloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Close", style=discord.ButtonStyle.danger, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: GamesView = self.view  # type: ignore
        await interaction.response.defer(ephemeral=True)
        view.disable_all()
        await view.refresh(interaction)


class GamesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

    @app_commands.command(name="games", description="Open the games menu (mobile-friendly)")
    async def games(self, interaction: discord.Interaction):
        log_cmd("games", interaction)

        if not self.settings.is_feature_allowed(interaction.guild_id, interaction.channel_id, "games"):
            await interaction.response.send_message(
                "❌ `/games` can only be used in the configured game channel(s).",
                ephemeral=True,
            )
            return

        # CRITICAL: prevent 10062
        await ensure_deferred(interaction, ephemeral=True)

        view = GamesView(self.bot, author_id=interaction.user.id)
        await interaction.followup.send(content=view.render_content(), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = GamesCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
