# cogs/tictactoe.py
from __future__ import annotations

import discord
from discord.ext import commands
from discord import app_commands

from core.logger import log_cmd
from core.utils import ensure_deferred
from core.settings import SettingsManager


EMPTY = "⬜"
P1 = "❌"
P2 = "🟡"


def check_winner(b: list[list[str]]) -> str | None:
    lines = []

    # rows
    lines.extend(b)
    # cols
    lines.extend([[b[r][c] for r in range(3)] for c in range(3)])
    # diags
    lines.append([b[i][i] for i in range(3)])
    lines.append([b[i][2 - i] for i in range(3)])

    for ln in lines:
        if ln[0] != EMPTY and ln[0] == ln[1] == ln[2]:
            return ln[0]
    return None


def is_full(b: list[list[str]]) -> bool:
    return all(cell != EMPTY for row in b for cell in row)


class TTTSquare(discord.ui.Button):
    def __init__(self, r: int, c: int):
        # IMPORTANT: label must be NON-empty. Space " " fails Discord validation.
        super().__init__(label=EMPTY, style=discord.ButtonStyle.secondary, row=r)
        self.r = r
        self.c = c

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeView = self.view  # type: ignore

        # ACK FAST
        await interaction.response.defer()

        # player gate
        if interaction.user.id not in (view.p1_id, view.p2_id):
            await interaction.followup.send("❌ You aren’t playing this game.", ephemeral=True)
            return

        # turn gate
        if interaction.user.id != view.turn_id:
            await interaction.followup.send("⏳ Not your turn.", ephemeral=True)
            return

        if view.board[self.r][self.c] != EMPTY:
            await interaction.followup.send("❌ That spot is already taken.", ephemeral=True)
            return

        mark = P1 if view.turn_id == view.p1_id else P2
        view.board[self.r][self.c] = mark

        # update button
        self.label = mark
        self.style = discord.ButtonStyle.danger if mark == P1 else discord.ButtonStyle.success
        self.disabled = True

        # check end
        w = check_winner(view.board)
        if w:
            view.finished = True
            view.winner = w
            view.disable_all()
        elif is_full(view.board):
            view.finished = True
            view.winner = None
            view.disable_all()
        else:
            # switch turn
            view.turn_id = view.p2_id if view.turn_id == view.p1_id else view.p1_id

        await view.refresh(interaction)


class ResignButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Resign", style=discord.ButtonStyle.danger, row=3)

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeView = self.view  # type: ignore
        await interaction.response.defer()

        if interaction.user.id not in (view.p1_id, view.p2_id):
            await interaction.followup.send("❌ You aren’t playing this game.", ephemeral=True)
            return

        view.finished = True
        view.winner = P2 if interaction.user.id == view.p1_id else P1
        view.disable_all()
        await view.refresh(interaction)


class CancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeView = self.view  # type: ignore
        await interaction.response.defer()

        # only starter can cancel
        if interaction.user.id != view.p1_id:
            await interaction.followup.send("❌ Only the game starter can cancel.", ephemeral=True)
            return

        view.finished = True
        view.cancelled = True
        view.disable_all()
        await view.refresh(interaction)


class TicTacToeView(discord.ui.View):
    def __init__(self, p1_id: int, p2_id: int):
        super().__init__(timeout=3600)
        self.p1_id = p1_id
        self.p2_id = p2_id
        self.turn_id = p1_id

        self.board = [[EMPTY for _ in range(3)] for _ in range(3)]
        self.finished = False
        self.cancelled = False
        self.winner: str | None = None

        # 3x3 grid
        for r in range(3):
            for c in range(3):
                self.add_item(TTTSquare(r, c))

        self.add_item(ResignButton())
        self.add_item(CancelButton())

    def disable_all(self):
        for c in self.children:
            c.disabled = True

    def header(self) -> str:
        if self.cancelled:
            return "🛑 **Tic Tac Toe** — cancelled."
        if self.finished:
            if self.winner == P1:
                return f"🏁 **Tic Tac Toe** — <@{self.p1_id}> wins!"
            if self.winner == P2:
                return f"🏁 **Tic Tac Toe** — <@{self.p2_id}> wins!"
            return "🤝 **Tic Tac Toe** — draw."
        return f"🎯 **Tic Tac Toe** — <@{self.turn_id}>, your turn."

    async def refresh(self, interaction: discord.Interaction):
        content = self.header()
        try:
            await interaction.message.edit(content=content, view=self)  # type: ignore
        except Exception:
            try:
                await interaction.edit_original_response(content=content, view=self)
            except Exception:
                pass


class TicTacToeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

    async def start_game(self, interaction: discord.Interaction, opponent: discord.Member):
        # callable from /games menu
        if opponent.bot:
            await interaction.followup.send("❌ Can’t play against a bot.", ephemeral=True)
            return
        if opponent.id == interaction.user.id:
            await interaction.followup.send("❌ You can’t play yourself.", ephemeral=True)
            return

        view = TicTacToeView(p1_id=interaction.user.id, p2_id=opponent.id)
        await interaction.followup.send(
            content=view.header(),
            view=view,
            ephemeral=False,  # game should be visible in channel
        )

    @app_commands.command(name="tictactoe", description="Start a Tic Tac Toe game.")
    @app_commands.describe(opponent="Who you want to play against")
    async def tictactoe(self, interaction: discord.Interaction, opponent: discord.Member):
        log_cmd("tictactoe", interaction)

        if not self.settings.is_feature_allowed(interaction.guild_id, interaction.channel_id, "tictactoe"):
            await interaction.response.send_message(
                "❌ `/tictactoe` can only be used in the configured game channel(s).",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=True)
        await self.start_game(interaction, opponent)


async def setup(bot: commands.Bot):
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = TicTacToeCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
