# cogs/connect4.py
from __future__ import annotations

from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.logger import log_cmd
from core.utils import ensure_deferred
from core.settings import SettingsManager

ROWS = 6
COLS = 7
EMPTY = "⚪"
P1 = "🔴"
P2 = "🟡"


def new_board() -> List[List[str]]:
    return [[EMPTY for _ in range(COLS)] for _ in range(ROWS)]


def render(board: List[List[str]]) -> str:
    nums = "1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"
    return "\n".join("".join(r) for r in board) + "\n" + nums


def check_dir(board: List[List[str]], r: int, c: int, dr: int, dc: int, piece: str) -> bool:
    for i in range(4):
        rr = r + dr * i
        cc = c + dc * i
        if not (0 <= rr < ROWS and 0 <= cc < COLS):
            return False
        if board[rr][cc] != piece:
            return False
    return True


def check_win(board: List[List[str]], piece: str) -> bool:
    for r in range(ROWS):
        for c in range(COLS):
            if (
                check_dir(board, r, c, 1, 0, piece) or
                check_dir(board, r, c, 0, 1, piece) or
                check_dir(board, r, c, 1, 1, piece) or
                check_dir(board, r, c, 1, -1, piece)
            ):
                return True
    return False


def is_draw(board: List[List[str]]) -> bool:
    return all(board[0][c] != EMPTY for c in range(COLS))


def drop_piece(board: List[List[str]], column: int, piece: str) -> bool:
    col = column - 1
    for row in reversed(range(ROWS)):
        if board[row][col] == EMPTY:
            board[row][col] = piece
            return True
    return False


class Connect4View(discord.ui.View):
    def __init__(self, cog: "Connect4Cog", channel_id: int):
        super().__init__(timeout=3600)
        self.cog = cog
        self.channel_id = channel_id

    def disable_all(self):
        for item in self.children:
            item.disabled = True

    def game(self) -> Optional[dict]:
        return self.cog.games.get(self.channel_id)

    async def end_game(self, interaction: discord.Interaction, content: str):
        self.disable_all()
        self.cog.games.pop(self.channel_id, None)
        await interaction.response.edit_message(content=content, view=self)

    async def handle_move(self, interaction: discord.Interaction, col: int):
        game = self.game()
        if not game:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        p1, p2 = game["players"]
        turn = game["turn"]
        board = game["board"]

        if interaction.user.id not in (p1.id, p2.id):
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return

        current = p1 if turn == 0 else p2
        piece = P1 if turn == 0 else P2

        if interaction.user.id != current.id:
            await interaction.response.send_message("Not your turn.", ephemeral=True)
            return

        if not drop_piece(board, col, piece):
            await interaction.response.send_message("Column full.", ephemeral=True)
            return

        if check_win(board, piece):
            await self.end_game(
                interaction,
                f"🎮 **Connect Four**\n{render(board)}\n🏆 {interaction.user.mention} wins!",
            )
            return

        if is_draw(board):
            await self.end_game(
                interaction,
                f"🎮 **Connect Four**\n{render(board)}\n🤝 Draw!",
            )
            return

        game["turn"] = 1 - turn
        next_player = p1 if game["turn"] == 0 else p2
        await interaction.response.edit_message(
            content=f"🎮 **Connect Four**\n{render(board)}\n{next_player.mention}, your turn!",
            view=self,
        )

    async def handle_cancel(self, interaction: discord.Interaction, resigned: bool):
        game = self.game()
        if not game:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        p1, p2 = game["players"]
        board = game["board"]

        if interaction.user.id not in (p1.id, p2.id):
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return

        if resigned:
            other = p2 if interaction.user.id == p1.id else p1
            msg = f"🏳️ {interaction.user.mention} resigned. {other.mention} wins!"
        else:
            msg = f"🛑 Game cancelled by {interaction.user.mention}."

        await self.end_game(interaction, f"🎮 **Connect Four**\n{render(board)}\n{msg}")

    # IMPORTANT: split buttons across rows (max 5 per row)
    @discord.ui.button(label="1", row=0)
    async def c1(self, i: discord.Interaction, b: discord.ui.Button): await self.handle_move(i, 1)

    @discord.ui.button(label="2", row=0)
    async def c2(self, i: discord.Interaction, b: discord.ui.Button): await self.handle_move(i, 2)

    @discord.ui.button(label="3", row=0)
    async def c3(self, i: discord.Interaction, b: discord.ui.Button): await self.handle_move(i, 3)

    @discord.ui.button(label="4", row=0)
    async def c4(self, i: discord.Interaction, b: discord.ui.Button): await self.handle_move(i, 4)

    @discord.ui.button(label="5", row=0)
    async def c5(self, i: discord.Interaction, b: discord.ui.Button): await self.handle_move(i, 5)

    @discord.ui.button(label="6", row=1)
    async def c6(self, i: discord.Interaction, b: discord.ui.Button): await self.handle_move(i, 6)

    @discord.ui.button(label="7", row=1)
    async def c7(self, i: discord.Interaction, b: discord.ui.Button): await self.handle_move(i, 7)

    @discord.ui.button(label="Resign", style=discord.ButtonStyle.danger, row=2)
    async def resign(self, i: discord.Interaction, b: discord.ui.Button): await self.handle_cancel(i, resigned=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, i: discord.Interaction, b: discord.ui.Button): await self.handle_cancel(i, resigned=False)


class Connect4Cog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self.games: Dict[int, dict] = {}

    def allowed(self, interaction: discord.Interaction) -> bool:
        return self.settings.is_feature_allowed(interaction.guild_id, interaction.channel_id, "connect4")

    async def start_game(self, interaction: discord.Interaction, opponent: discord.Member):
        cid = interaction.channel_id

        if not self.allowed(interaction):
            await interaction.followup.send("❌ `/connect4` can only be used in the configured game channel(s).", ephemeral=True)
            return

        if cid in self.games:
            await interaction.followup.send("A Connect4 game is already running here.", ephemeral=True)
            return

        if opponent.bot or opponent.id == interaction.user.id:
            await interaction.followup.send("Pick a real opponent.", ephemeral=True)
            return

        board = new_board()
        self.games[cid] = {"players": [interaction.user, opponent], "turn": 0, "board": board}

        view = Connect4View(self, cid)
        await interaction.followup.send(
            content=f"🎮 **Connect Four**\n{render(board)}\n{interaction.user.mention}, your turn!",
            view=view,
        )

    @app_commands.command(name="connect4", description="Play Connect Four")
    @app_commands.describe(opponent="Who you want to play against")
    async def connect4(self, interaction: discord.Interaction, opponent: discord.Member):
        log_cmd("connect4", interaction)
        await ensure_deferred(interaction, ephemeral=False)
        await self.start_game(interaction, opponent)


async def setup(bot: commands.Bot):
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = Connect4Cog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
