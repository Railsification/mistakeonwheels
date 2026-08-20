# cogs/tictactoe.py
from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.game_stats import record_head_to_head_result, register_game
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.storage import known_guild_dirs, load_guild_json, save_guild_json
from core.utils import ensure_deferred


EMPTY = "⬜"
P1 = "❌"
P2 = "🟡"
GAMES_FILENAME = "tictactoe_games.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_board() -> list[list[str]]:
    return [[EMPTY for _ in range(3)] for _ in range(3)]


def check_winner(board: list[list[str]]) -> str | None:
    lines: list[list[str]] = []
    lines.extend(board)
    lines.extend([[board[r][c] for r in range(3)] for c in range(3)])
    lines.append([board[i][i] for i in range(3)])
    lines.append([board[i][2 - i] for i in range(3)])

    for line in lines:
        if line[0] != EMPTY and line[0] == line[1] == line[2]:
            return line[0]
    return None


def is_full(board: list[list[str]]) -> bool:
    return all(cell != EMPTY for row in board for cell in row)


def _valid_board(raw: Any) -> list[list[str]]:
    if not isinstance(raw, list) or len(raw) != 3:
        return _new_board()

    board: list[list[str]] = []
    for raw_row in raw:
        if not isinstance(raw_row, list) or len(raw_row) != 3:
            return _new_board()
        board.append([
            cell if cell in (EMPTY, P1, P2) else EMPTY
            for cell in raw_row
        ])
    return board


def active_content(game: dict[str, Any]) -> str:
    turn_id = int(game.get("turn_id") or 0)
    if bool(game.get("computer")) and turn_id == int(game.get("p2_id") or 0):
        return "🎯 **Tic Tac Toe** — 🤖 Computer is thinking..."
    return f"🎯 **Tic Tac Toe** — <@{turn_id}>, your turn."


def final_content(
    game: dict[str, Any],
    *,
    winner_id: int | None = None,
    cancelled: bool = False,
) -> str:
    if cancelled:
        return "🛑 **Tic Tac Toe** — cancelled."
    if winner_id:
        if bool(game.get("computer")) and winner_id == int(game.get("p2_id") or 0):
            return "🏁 **Tic Tac Toe** — 🤖 Computer wins!"
        return f"🏁 **Tic Tac Toe** — <@{winner_id}> wins!"
    return "🤝 **Tic Tac Toe** — draw."


def _available_moves(board: list[list[str]]) -> list[tuple[int, int]]:
    return [
        (row, col)
        for row in range(3)
        for col in range(3)
        if board[row][col] == EMPTY
    ]


def _minimax(board: list[list[str]], maximizing: bool, depth: int = 0) -> int:
    winner = check_winner(board)
    if winner == P2:
        return 10 - depth
    if winner == P1:
        return depth - 10
    if is_full(board):
        return 0

    if maximizing:
        best = -100
        for row, col in _available_moves(board):
            board[row][col] = P2
            best = max(best, _minimax(board, False, depth + 1))
            board[row][col] = EMPTY
        return best

    best = 100
    for row, col in _available_moves(board):
        board[row][col] = P1
        best = min(best, _minimax(board, True, depth + 1))
        board[row][col] = EMPTY
    return best


def computer_move(board: list[list[str]]) -> tuple[int, int] | None:
    moves = _available_moves(board)
    if not moves:
        return None

    best_score = -100
    best_moves: list[tuple[int, int]] = []
    for row, col in moves:
        board[row][col] = P2
        score = _minimax(board, False, 1)
        board[row][col] = EMPTY

        if score > best_score:
            best_score = score
            best_moves = [(row, col)]
        elif score == best_score:
            best_moves.append((row, col))

    return random.choice(best_moves)


class TTTSquare(discord.ui.Button):
    def __init__(
        self,
        cog: "TicTacToeCog",
        row_index: int,
        col_index: int,
    ):
        super().__init__(
            label=EMPTY,
            style=discord.ButtonStyle.secondary,
            custom_id=f"hotbot:tictactoe:cell:{row_index}:{col_index}:v2",
            row=row_index,
        )
        self.cog = cog
        self.row_index = row_index
        self.col_index = col_index

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_move(
            interaction,
            self.row_index,
            self.col_index,
        )


class TTTResignButton(discord.ui.Button):
    def __init__(self, cog: "TicTacToeCog"):
        super().__init__(
            label="Resign",
            style=discord.ButtonStyle.danger,
            custom_id="hotbot:tictactoe:resign:v2",
            row=3,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_resign(interaction)


class TTTCancelButton(discord.ui.Button):
    def __init__(self, cog: "TicTacToeCog"):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="hotbot:tictactoe:cancel:v2",
            row=3,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_cancel(interaction)


class TicTacToeView(discord.ui.View):
    def __init__(
        self,
        cog: "TicTacToeCog",
        game: dict[str, Any] | None = None,
        *,
        finished: bool = False,
    ):
        # No timeout: Discord can route these buttons after a bot restart.
        super().__init__(timeout=None)
        self.cog = cog

        for row_index in range(3):
            for col_index in range(3):
                self.add_item(TTTSquare(cog, row_index, col_index))

        self.add_item(TTTResignButton(cog))
        self.add_item(TTTCancelButton(cog))

        if game is not None:
            self.apply_game(game, finished=finished)

    def apply_game(
        self,
        game: dict[str, Any],
        *,
        finished: bool,
    ) -> None:
        board = _valid_board(game.get("board"))
        for item in self.children:
            if isinstance(item, TTTSquare):
                mark = board[item.row_index][item.col_index]
                item.label = mark
                item.disabled = finished or mark != EMPTY
                if mark == P1:
                    item.style = discord.ButtonStyle.danger
                elif mark == P2:
                    item.style = discord.ButtonStyle.success
                else:
                    item.style = discord.ButtonStyle.secondary
            elif finished:
                item.disabled = True


class TicTacToeCog(commands.Cog):
    GAME_META = {
        "key": "tictactoe",
        "label": "Tic Tac Toe",
        "kind": "head_to_head",
        "result_word": "win",
        "description": "Classic 3×3 Tic Tac Toe",
        "emoji": "❎",
        "requires_opponent": True,
    }

    HELP_META = {
        "title": "Tic Tac Toe",
        "summary": "Persistent Tic Tac Toe for two players or one player vs Computer.",
        "details": (
            "Use /tictactoe and optionally choose an opponent. Leave the opponent blank "
            "for Computer mode, or choose Tic Tac Toe from /games and press Computer. "
            "Games survive normal Railway restarts."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._restored_once = False

        register_game(
            "tictactoe",
            label="Tic Tac Toe",
            kind="head_to_head",
            result_word="win",
        )

    async def cog_load(self) -> None:
        # One generic persistent view routes every saved Tic Tac Toe message.
        self.bot.add_view(TicTacToeView(self))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored_once:
            return
        await self.restore_saved_games()
        self._restored_once = True

    def _lock_for(self, guild_id: int, channel_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(channel_id))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _load_blob(self, guild_id: int) -> dict[str, Any]:
        raw = load_guild_json(
            guild_id,
            GAMES_FILENAME,
            {"games": {}},
        )
        if not isinstance(raw, dict):
            raw = {"games": {}}
        if not isinstance(raw.get("games"), dict):
            raw["games"] = {}
        return raw

    def _save_blob(self, guild_id: int, blob: dict[str, Any]) -> None:
        save_guild_json(guild_id, GAMES_FILENAME, blob)

    def _get_game(
        self,
        guild_id: int,
        channel_id: int,
    ) -> dict[str, Any] | None:
        game = self._load_blob(guild_id)["games"].get(str(channel_id))
        if not isinstance(game, dict):
            return None

        game["board"] = _valid_board(game.get("board"))
        game["computer"] = bool(game.get("computer"))
        return game

    def _set_game(
        self,
        guild_id: int,
        channel_id: int,
        game: dict[str, Any],
    ) -> None:
        blob = self._load_blob(guild_id)
        blob["games"][str(channel_id)] = game
        self._save_blob(guild_id, blob)

    def _remove_game(
        self,
        guild_id: int,
        channel_id: int,
    ) -> dict[str, Any] | None:
        blob = self._load_blob(guild_id)
        old = blob["games"].pop(str(channel_id), None)
        self._save_blob(guild_id, blob)
        return old if isinstance(old, dict) else None

    def _jump_link(self, game: dict[str, Any]) -> str:
        guild_id = int(game.get("guild_id") or 0)
        channel_id = int(game.get("channel_id") or 0)
        message_id = int(game.get("message_id") or 0)
        if guild_id and channel_id and message_id:
            return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        return ""

    def _current_game_for_interaction(
        self,
        interaction: discord.Interaction,
    ) -> dict[str, Any] | None:
        if (
            interaction.guild_id is None
            or interaction.channel_id is None
            or interaction.message is None
        ):
            return None

        game = self._get_game(
            interaction.guild_id,
            interaction.channel_id,
        )
        if not game:
            return None
        if int(game.get("message_id") or 0) != interaction.message.id:
            return None
        return game

    def _record_result(
        self,
        guild_id: int,
        game: dict[str, Any],
        winner_id: int | None,
    ) -> None:
        if bool(game.get("computer")):
            return

        p1_id = int(game.get("p1_id") or 0)
        p2_id = int(game.get("p2_id") or 0)
        game_id = str(game.get("game_id") or "").strip()
        if not p1_id or not p2_id or not game_id:
            return

        record_head_to_head_result(
            guild_id,
            "tictactoe",
            p1_id,
            p2_id,
            winner_id=winner_id,
            result_id=f"tictactoe:{game_id}",
        )

    async def restore_saved_games(self) -> None:
        for guild_id in known_guild_dirs():
            blob = self._load_blob(guild_id)
            stale_channels: list[str] = []

            for channel_key, game in list(blob["games"].items()):
                if not isinstance(game, dict):
                    stale_channels.append(channel_key)
                    continue

                channel_id = int(game.get("channel_id") or channel_key or 0)
                message_id = int(game.get("message_id") or 0)
                if not channel_id or not message_id:
                    stale_channels.append(channel_key)
                    continue

                try:
                    channel = self.bot.get_channel(channel_id)
                    if channel is None:
                        channel = await self.bot.fetch_channel(channel_id)
                    message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
                    await message.edit(
                        content=active_content(game),
                        view=TicTacToeView(self, game),
                    )
                except discord.NotFound:
                    stale_channels.append(channel_key)
                except Exception as exc:
                    warn(
                        f"Tic Tac Toe restore failed for "
                        f"{guild_id}/{channel_id}/{message_id}: {exc!r}"
                    )

            if stale_channels:
                for channel_key in stale_channels:
                    blob["games"].pop(channel_key, None)
                self._save_blob(guild_id, blob)

    async def _start(
        self,
        interaction: discord.Interaction,
        *,
        p2_id: int,
        computer: bool,
    ) -> None:
        if interaction.guild is None or interaction.channel_id is None:
            await interaction.followup.send(
                "This game must be started in a server channel.",
                ephemeral=True,
            )
            return

        if not self.settings.is_game_allowed(
            interaction.guild_id,
            interaction.channel_id,
            "tictactoe",
        ):
            await interaction.followup.send(
                "❌ Tic Tac Toe is not enabled in this channel.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            existing = self._get_game(guild_id, channel_id)
            if existing:
                text = "A Tic Tac Toe game is already running here."
                jump = self._jump_link(existing)
                if jump:
                    text += f" [Open it]({jump})"
                await interaction.followup.send(text, ephemeral=True)
                return

            game: dict[str, Any] = {
                "game_id": uuid.uuid4().hex,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "message_id": 0,
                "p1_id": interaction.user.id,
                "p2_id": int(p2_id),
                "computer": bool(computer),
                "turn_id": interaction.user.id,
                "board": _new_board(),
                "created_at": _utc_now(),
            }

            message = await interaction.followup.send(
                content=active_content(game),
                view=TicTacToeView(self, game),
                ephemeral=False,
                wait=True,
            )
            game["message_id"] = message.id
            self._set_game(guild_id, channel_id, game)

    async def start_game(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
    ) -> None:
        if opponent.bot:
            await interaction.followup.send(
                "❌ Can’t use a Discord bot as the opponent. Choose Computer mode instead.",
                ephemeral=True,
            )
            return
        if opponent.id == interaction.user.id:
            await interaction.followup.send(
                "❌ You can’t play yourself.",
                ephemeral=True,
            )
            return
        await self._start(interaction, p2_id=opponent.id, computer=False)

    async def start_computer_game(self, interaction: discord.Interaction) -> None:
        bot_user = self.bot.user or interaction.client.user
        if bot_user is None:
            await interaction.followup.send(
                "❌ Computer mode is unavailable until the bot is fully connected.",
                ephemeral=True,
            )
            return
        await self._start(interaction, p2_id=int(bot_user.id), computer=True)

    async def _interaction_error(
        self,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        await interaction.followup.send(text, ephemeral=True)

    async def _finish(
        self,
        interaction: discord.Interaction,
        game: dict[str, Any],
        *,
        winner_id: int | None,
    ) -> None:
        guild_id = int(game["guild_id"])
        channel_id = int(game["channel_id"])
        self._record_result(guild_id, game, winner_id)
        self._remove_game(guild_id, channel_id)
        await interaction.message.edit(  # type: ignore[union-attr]
            content=final_content(game, winner_id=winner_id),
            view=TicTacToeView(self, game, finished=True),
        )

    async def handle_move(
        self,
        interaction: discord.Interaction,
        row_index: int,
        col_index: int,
    ) -> None:
        await interaction.response.defer()

        if interaction.guild_id is None or interaction.channel_id is None:
            await self._interaction_error(
                interaction,
                "This game is no longer active.",
            )
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._current_game_for_interaction(interaction)
            if not game:
                await self._interaction_error(
                    interaction,
                    "This Tic Tac Toe game is no longer active.",
                )
                return

            p1_id = int(game["p1_id"])
            p2_id = int(game["p2_id"])
            turn_id = int(game["turn_id"])
            computer = bool(game.get("computer"))

            allowed_players = (p1_id,) if computer else (p1_id, p2_id)
            if interaction.user.id not in allowed_players:
                await self._interaction_error(
                    interaction,
                    "❌ You aren’t playing this game.",
                )
                return

            if interaction.user.id != turn_id:
                await self._interaction_error(
                    interaction,
                    "⏳ Not your turn.",
                )
                return

            board = _valid_board(game.get("board"))
            if board[row_index][col_index] != EMPTY:
                await self._interaction_error(
                    interaction,
                    "❌ That spot is already taken.",
                )
                return

            board[row_index][col_index] = P1 if turn_id == p1_id else P2
            game["board"] = board

            winning_mark = check_winner(board)
            if winning_mark:
                winner_id = p1_id if winning_mark == P1 else p2_id
                await self._finish(interaction, game, winner_id=winner_id)
                return

            if is_full(board):
                await self._finish(interaction, game, winner_id=None)
                return

            if computer:
                game["turn_id"] = p2_id
                move = computer_move(board)
                if move is not None:
                    ai_row, ai_col = move
                    board[ai_row][ai_col] = P2
                    game["board"] = board

                winning_mark = check_winner(board)
                if winning_mark == P2:
                    await self._finish(interaction, game, winner_id=p2_id)
                    return

                if is_full(board):
                    await self._finish(interaction, game, winner_id=None)
                    return

                game["turn_id"] = p1_id
                self._set_game(guild_id, channel_id, game)
                await interaction.message.edit(  # type: ignore[union-attr]
                    content=active_content(game),
                    view=TicTacToeView(self, game),
                )
                return

            game["turn_id"] = p2_id if turn_id == p1_id else p1_id
            self._set_game(guild_id, channel_id, game)
            await interaction.message.edit(  # type: ignore[union-attr]
                content=active_content(game),
                view=TicTacToeView(self, game),
            )

    async def handle_resign(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer()
        if interaction.guild_id is None or interaction.channel_id is None:
            await self._interaction_error(
                interaction,
                "This game is no longer active.",
            )
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._current_game_for_interaction(interaction)
            if not game:
                await self._interaction_error(
                    interaction,
                    "This Tic Tac Toe game is no longer active.",
                )
                return

            p1_id = int(game["p1_id"])
            p2_id = int(game["p2_id"])
            computer = bool(game.get("computer"))
            allowed_players = (p1_id,) if computer else (p1_id, p2_id)
            if interaction.user.id not in allowed_players:
                await self._interaction_error(
                    interaction,
                    "❌ You aren’t playing this game.",
                )
                return

            winner_id = p2_id if interaction.user.id == p1_id else p1_id
            self._record_result(guild_id, game, winner_id)
            self._remove_game(guild_id, channel_id)

            winner_text = (
                "🤖 Computer"
                if computer and winner_id == p2_id
                else f"<@{winner_id}>"
            )
            await interaction.message.edit(  # type: ignore[union-attr]
                content=(
                    f"🏳️ <@{interaction.user.id}> resigned. "
                    f"{winner_text} wins!"
                ),
                view=TicTacToeView(self, game, finished=True),
            )

    async def handle_cancel(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer()
        if interaction.guild_id is None or interaction.channel_id is None:
            await self._interaction_error(
                interaction,
                "This game is no longer active.",
            )
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._current_game_for_interaction(interaction)
            if not game:
                await self._interaction_error(
                    interaction,
                    "This Tic Tac Toe game is no longer active.",
                )
                return

            if interaction.user.id != int(game["p1_id"]):
                await self._interaction_error(
                    interaction,
                    "❌ Only the game starter can cancel.",
                )
                return

            self._remove_game(guild_id, channel_id)
            await interaction.message.edit(  # type: ignore[union-attr]
                content=final_content(game, cancelled=True),
                view=TicTacToeView(self, game, finished=True),
            )

    @app_commands.command(
        name="tictactoe",
        description="Start a Tic Tac Toe game.",
    )
    @app_commands.describe(
        opponent="Who to play against — leave blank to play the Computer"
    )
    async def tictactoe(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None = None,
    ) -> None:
        log_cmd("tictactoe", interaction)
        if not self.settings.is_game_allowed(
            interaction.guild_id,
            interaction.channel_id,
            "tictactoe",
        ):
            await interaction.response.send_message(
                "❌ `/tictactoe` can only be used in the configured game channel(s).",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=False)
        if opponent is None:
            await self.start_computer_game(interaction)
        else:
            await self.start_game(interaction, opponent)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = TicTacToeCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
