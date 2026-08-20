# cogs/connect4.py
from __future__ import annotations

import asyncio
import math
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

ROWS = 6
COLS = 7
EMPTY = "⚪"
P1 = "🔴"
P2 = "🟡"
GAMES_FILENAME = "connect4_games.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_board() -> list[list[str]]:
    return [[EMPTY for _ in range(COLS)] for _ in range(ROWS)]


def valid_board(raw: Any) -> list[list[str]]:
    if not isinstance(raw, list) or len(raw) != ROWS:
        return new_board()

    board: list[list[str]] = []
    for raw_row in raw:
        if not isinstance(raw_row, list) or len(raw_row) != COLS:
            return new_board()
        row = [
            cell if cell in (EMPTY, P1, P2) else EMPTY
            for cell in raw_row
        ]
        board.append(row)
    return board


def render(board: list[list[str]]) -> str:
    numbers = "1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"
    return "\n".join("".join(row) for row in board) + "\n" + numbers


def check_dir(
    board: list[list[str]],
    row: int,
    col: int,
    row_step: int,
    col_step: int,
    piece: str,
) -> bool:
    for index in range(4):
        check_row = row + row_step * index
        check_col = col + col_step * index
        if not (0 <= check_row < ROWS and 0 <= check_col < COLS):
            return False
        if board[check_row][check_col] != piece:
            return False
    return True


def check_win(board: list[list[str]], piece: str) -> bool:
    for row in range(ROWS):
        for col in range(COLS):
            if (
                check_dir(board, row, col, 1, 0, piece)
                or check_dir(board, row, col, 0, 1, piece)
                or check_dir(board, row, col, 1, 1, piece)
                or check_dir(board, row, col, 1, -1, piece)
            ):
                return True
    return False


def is_draw(board: list[list[str]]) -> bool:
    return all(board[0][col] != EMPTY for col in range(COLS))


def drop_piece(
    board: list[list[str]],
    column: int,
    piece: str,
) -> bool:
    if not 1 <= int(column) <= COLS:
        return False

    col = int(column) - 1
    for row in reversed(range(ROWS)):
        if board[row][col] == EMPTY:
            board[row][col] = piece
            return True
    return False


def available_columns(board: list[list[str]]) -> list[int]:
    return [col + 1 for col in range(COLS) if board[0][col] == EMPTY]


def _copy_board(board: list[list[str]]) -> list[list[str]]:
    return [row[:] for row in board]


def _window_score(window: list[str], piece: str) -> int:
    opponent = P1 if piece == P2 else P2
    own = window.count(piece)
    opp = window.count(opponent)
    blanks = window.count(EMPTY)

    score = 0
    if own == 4:
        score += 100_000
    elif own == 3 and blanks == 1:
        score += 120
    elif own == 2 and blanks == 2:
        score += 18

    if opp == 4:
        score -= 100_000
    elif opp == 3 and blanks == 1:
        score -= 150
    elif opp == 2 and blanks == 2:
        score -= 12
    return score


def _position_score(board: list[list[str]], piece: str) -> int:
    score = 0

    # Centre control produces more possible four-in-a-row lines.
    centre = [board[row][COLS // 2] for row in range(ROWS)]
    score += centre.count(piece) * 8

    for row in range(ROWS):
        for col in range(COLS - 3):
            score += _window_score(board[row][col : col + 4], piece)

    for col in range(COLS):
        for row in range(ROWS - 3):
            window = [board[row + offset][col] for offset in range(4)]
            score += _window_score(window, piece)

    for row in range(ROWS - 3):
        for col in range(COLS - 3):
            window = [board[row + offset][col + offset] for offset in range(4)]
            score += _window_score(window, piece)

    for row in range(3, ROWS):
        for col in range(COLS - 3):
            window = [board[row - offset][col + offset] for offset in range(4)]
            score += _window_score(window, piece)

    return score


def _ordered_columns(columns: list[int]) -> list[int]:
    # Search centre-first for stronger pruning and more natural play.
    preferred = [4, 3, 5, 2, 6, 1, 7]
    return [column for column in preferred if column in columns]


def _minimax(
    board: list[list[str]],
    depth: int,
    alpha: float,
    beta: float,
    maximizing: bool,
) -> tuple[int | None, float]:
    valid = available_columns(board)
    terminal = check_win(board, P1) or check_win(board, P2) or not valid

    if depth <= 0 or terminal:
        if check_win(board, P2):
            return None, 1_000_000 + depth
        if check_win(board, P1):
            return None, -1_000_000 - depth
        if not valid:
            return None, 0
        return None, float(_position_score(board, P2))

    ordered = _ordered_columns(valid)

    if maximizing:
        best_score = -math.inf
        best_columns: list[int] = []
        for column in ordered:
            trial = _copy_board(board)
            drop_piece(trial, column, P2)
            _unused, score = _minimax(trial, depth - 1, alpha, beta, False)
            if score > best_score:
                best_score = score
                best_columns = [column]
            elif score == best_score:
                best_columns.append(column)
            alpha = max(alpha, best_score)
            if alpha >= beta:
                break
        return random.choice(best_columns), best_score

    best_score = math.inf
    best_columns = []
    for column in ordered:
        trial = _copy_board(board)
        drop_piece(trial, column, P1)
        _unused, score = _minimax(trial, depth - 1, alpha, beta, True)
        if score < best_score:
            best_score = score
            best_columns = [column]
        elif score == best_score:
            best_columns.append(column)
        beta = min(beta, best_score)
        if alpha >= beta:
            break
    return random.choice(best_columns), best_score


def choose_computer_column(board: list[list[str]]) -> int | None:
    valid = available_columns(board)
    if not valid:
        return None

    # Always take a win immediately.
    for column in _ordered_columns(valid):
        trial = _copy_board(board)
        drop_piece(trial, column, P2)
        if check_win(trial, P2):
            return column

    # Always stop an immediate human win.
    for column in _ordered_columns(valid):
        trial = _copy_board(board)
        drop_piece(trial, column, P1)
        if check_win(trial, P1):
            return column

    column, _score = _minimax(
        board,
        depth=5,
        alpha=-math.inf,
        beta=math.inf,
        maximizing=True,
    )
    return column if column in valid else random.choice(valid)


def _player_label(game: dict[str, Any], player_number: int) -> str:
    if bool(game.get("computer")) and player_number == 2:
        return "🤖 Computer"
    key = "p1_id" if player_number == 1 else "p2_id"
    return f"<@{int(game.get(key) or 0)}>"


def _label_for_id(game: dict[str, Any], user_id: int | None) -> str:
    if user_id is None:
        return "Nobody"
    if bool(game.get("computer")) and int(user_id) == int(game.get("p2_id") or 0):
        return "🤖 Computer"
    return f"<@{int(user_id)}>"


def active_content(game: dict[str, Any]) -> str:
    board = valid_board(game.get("board"))
    turn = int(game.get("turn") or 0)
    player_number = 1 if turn == 0 else 2
    return (
        f"🎮 **Connect Four**\n"
        f"{render(board)}\n"
        f"{_player_label(game, player_number)}, your turn!"
    )


def final_content(
    game: dict[str, Any],
    *,
    winner_id: int | None = None,
    resigned_id: int | None = None,
    cancelled_id: int | None = None,
) -> str:
    board = valid_board(game.get("board"))
    if winner_id and resigned_id:
        result = (
            f"🏳️ {_label_for_id(game, resigned_id)} resigned. "
            f"{_label_for_id(game, winner_id)} wins!"
        )
    elif winner_id:
        result = f"🏆 {_label_for_id(game, winner_id)} wins!"
    elif cancelled_id:
        result = f"🛑 Game cancelled by {_label_for_id(game, cancelled_id)}."
    else:
        result = "🤝 Draw!"

    if bool(game.get("computer")) and not cancelled_id:
        result += "  _Practice game — leaderboard unchanged._"

    return f"🎮 **Connect Four**\n{render(board)}\n{result}"


class Connect4ColumnButton(discord.ui.Button):
    def __init__(self, cog: "Connect4Cog", column: int):
        super().__init__(
            label=str(column),
            style=discord.ButtonStyle.secondary,
            custom_id=f"hotbot:connect4:column:{column}:v2",
            row=0 if column <= 5 else 1,
        )
        self.cog = cog
        self.column = column

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_move(interaction, self.column)


class Connect4ResignButton(discord.ui.Button):
    def __init__(self, cog: "Connect4Cog"):
        super().__init__(
            label="Resign",
            style=discord.ButtonStyle.danger,
            custom_id="hotbot:connect4:resign:v2",
            row=2,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_resign(interaction)


class Connect4CancelButton(discord.ui.Button):
    def __init__(self, cog: "Connect4Cog"):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="hotbot:connect4:cancel:v2",
            row=2,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_cancel(interaction)


class Connect4View(discord.ui.View):
    def __init__(
        self,
        cog: "Connect4Cog",
        game: dict[str, Any] | None = None,
        *,
        finished: bool = False,
    ):
        # No timeout: buttons remain routeable after a bot restart.
        super().__init__(timeout=None)
        self.cog = cog
        for column in range(1, COLS + 1):
            self.add_item(Connect4ColumnButton(cog, column))

        self.add_item(Connect4ResignButton(cog))
        self.add_item(Connect4CancelButton(cog))

        if game is not None:
            self.apply_game(game, finished=finished)

    def apply_game(
        self,
        game: dict[str, Any],
        *,
        finished: bool,
    ) -> None:
        board = valid_board(game.get("board"))
        for item in self.children:
            if isinstance(item, Connect4ColumnButton):
                item.disabled = finished or board[0][item.column - 1] != EMPTY
            elif finished:
                item.disabled = True


class Connect4Cog(commands.Cog):
    GAME_META = {
        "key": "connect4",
        "label": "Connect Four",
        "kind": "head_to_head",
        "result_word": "win",
        "description": "Drop four in a row against a player or the computer",
        "emoji": "🔴",
        "requires_opponent": True,
    }

    HELP_META = {
        "title": "Connect Four",
        "summary": "Persistent Connect Four for PvP or solo play against the computer.",
        "details": "Use /connect4 with an opponent for PvP, leave the opponent blank for Computer, or start it from /games. Computer games are practice and do not alter the leaderboard.",
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._restored_once = False

        register_game(
            "connect4",
            label="Connect Four",
            kind="head_to_head",
            result_word="win",
        )

    async def cog_load(self) -> None:
        self.bot.add_view(Connect4View(self))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored_once:
            return
        await self.restore_saved_games()
        self._restored_once = True

    def allowed(self, interaction: discord.Interaction) -> bool:
        return self.settings.is_game_allowed(
            interaction.guild_id,
            interaction.channel_id,
            "connect4",
        )

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
        game["board"] = valid_board(game.get("board"))
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

        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if not game:
            return None
        if int(game.get("message_id") or 0) != interaction.message.id:
            return None
        return game

    @staticmethod
    def _is_computer_game(game: dict[str, Any]) -> bool:
        return bool(game.get("computer"))

    def _record_result(
        self,
        guild_id: int,
        game: dict[str, Any],
        winner_id: int | None,
    ) -> None:
        if self._is_computer_game(game):
            return

        record_head_to_head_result(
            guild_id,
            "connect4",
            int(game["p1_id"]),
            int(game["p2_id"]),
            winner_id=winner_id,
            result_id=f"connect4:{game['game_id']}",
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
                        view=Connect4View(self, game),
                    )
                except discord.NotFound:
                    stale_channels.append(channel_key)
                except Exception as exc:
                    warn(
                        f"Connect Four restore failed for "
                        f"{guild_id}/{channel_id}/{message_id}: {exc!r}"
                    )

            if stale_channels:
                for channel_key in stale_channels:
                    blob["games"].pop(channel_key, None)
                self._save_blob(guild_id, blob)

    async def _start_game(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None,
        *,
        computer: bool,
    ) -> None:
        if interaction.guild is None or interaction.channel_id is None:
            await interaction.followup.send(
                "This game must be started in a server channel.",
                ephemeral=True,
            )
            return

        if not self.allowed(interaction):
            await interaction.followup.send(
                "❌ `/connect4` can only be used in the configured game channel(s).",
                ephemeral=True,
            )
            return

        if not computer:
            if opponent is None or opponent.bot or opponent.id == interaction.user.id:
                await interaction.followup.send(
                    "Pick a real opponent.",
                    ephemeral=True,
                )
                return

        bot_user = self.bot.user or interaction.client.user
        if computer and bot_user is None:
            await interaction.followup.send(
                "❌ Computer mode is unavailable until the bot is fully connected.",
                ephemeral=True,
            )
            return

        p2_id = int(bot_user.id) if computer else int(opponent.id)  # type: ignore[union-attr]
        guild_id = interaction.guild.id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            existing = self._get_game(guild_id, channel_id)
            if existing:
                text = "A Connect Four game is already running here."
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
                "p2_id": p2_id,
                "computer": computer,
                "turn": 0,
                "board": new_board(),
                "created_at": _utc_now(),
            }

            message = await interaction.followup.send(
                content=active_content(game),
                view=Connect4View(self, game),
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
        await self._start_game(interaction, opponent, computer=False)

    async def start_computer_game(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._start_game(interaction, None, computer=True)

    async def _interaction_error(
        self,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        await interaction.followup.send(text, ephemeral=True)

    async def handle_move(
        self,
        interaction: discord.Interaction,
        column: int,
    ) -> None:
        await interaction.response.defer()

        if interaction.guild_id is None or interaction.channel_id is None:
            await self._interaction_error(interaction, "This game is no longer active.")
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._current_game_for_interaction(interaction)
            if not game:
                await self._interaction_error(
                    interaction,
                    "This Connect Four game is no longer active.",
                )
                return

            p1_id = int(game["p1_id"])
            p2_id = int(game["p2_id"])
            turn = int(game.get("turn") or 0)
            current_id = p1_id if turn == 0 else p2_id
            piece = P1 if turn == 0 else P2

            if self._is_computer_game(game):
                if interaction.user.id != p1_id:
                    await self._interaction_error(interaction, "Not your game.")
                    return
                if turn != 0:
                    await self._interaction_error(interaction, "🤖 Computer is taking its turn.")
                    return
            else:
                if interaction.user.id not in (p1_id, p2_id):
                    await self._interaction_error(interaction, "Not your game.")
                    return
                if interaction.user.id != current_id:
                    await self._interaction_error(interaction, "Not your turn.")
                    return

            board = valid_board(game.get("board"))
            if not drop_piece(board, column, piece):
                await self._interaction_error(interaction, "Column full.")
                return

            game["board"] = board
            if check_win(board, piece):
                winner_id = interaction.user.id
                self._record_result(guild_id, game, winner_id)
                self._remove_game(guild_id, channel_id)
                await interaction.message.edit(  # type: ignore[union-attr]
                    content=final_content(game, winner_id=winner_id),
                    view=Connect4View(self, game, finished=True),
                )
                return

            if is_draw(board):
                self._record_result(guild_id, game, None)
                self._remove_game(guild_id, channel_id)
                await interaction.message.edit(  # type: ignore[union-attr]
                    content=final_content(game),
                    view=Connect4View(self, game, finished=True),
                )
                return

            if self._is_computer_game(game):
                computer_column = choose_computer_column(board)
                if computer_column is not None:
                    drop_piece(board, computer_column, P2)
                    game["board"] = board

                    if check_win(board, P2):
                        winner_id = p2_id
                        self._remove_game(guild_id, channel_id)
                        await interaction.message.edit(  # type: ignore[union-attr]
                            content=final_content(game, winner_id=winner_id),
                            view=Connect4View(self, game, finished=True),
                        )
                        return

                    if is_draw(board):
                        self._remove_game(guild_id, channel_id)
                        await interaction.message.edit(  # type: ignore[union-attr]
                            content=final_content(game),
                            view=Connect4View(self, game, finished=True),
                        )
                        return

                # Computer move is completed in the same interaction, so only a
                # human-turn state is persisted and restored after restarts.
                game["turn"] = 0
            else:
                game["turn"] = 1 - turn

            self._set_game(guild_id, channel_id, game)
            await interaction.message.edit(  # type: ignore[union-attr]
                content=active_content(game),
                view=Connect4View(self, game),
            )

    async def handle_resign(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer()
        if interaction.guild_id is None or interaction.channel_id is None:
            await self._interaction_error(interaction, "This game is no longer active.")
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._current_game_for_interaction(interaction)
            if not game:
                await self._interaction_error(
                    interaction,
                    "This Connect Four game is no longer active.",
                )
                return

            p1_id = int(game["p1_id"])
            p2_id = int(game["p2_id"])
            allowed_ids = (p1_id,) if self._is_computer_game(game) else (p1_id, p2_id)
            if interaction.user.id not in allowed_ids:
                await self._interaction_error(interaction, "Not your game.")
                return

            winner_id = p2_id if interaction.user.id == p1_id else p1_id
            self._record_result(guild_id, game, winner_id)
            self._remove_game(guild_id, channel_id)
            await interaction.message.edit(  # type: ignore[union-attr]
                content=final_content(
                    game,
                    winner_id=winner_id,
                    resigned_id=interaction.user.id,
                ),
                view=Connect4View(self, game, finished=True),
            )

    async def handle_cancel(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer()
        if interaction.guild_id is None or interaction.channel_id is None:
            await self._interaction_error(interaction, "This game is no longer active.")
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._current_game_for_interaction(interaction)
            if not game:
                await self._interaction_error(
                    interaction,
                    "This Connect Four game is no longer active.",
                )
                return

            p1_id = int(game["p1_id"])
            p2_id = int(game["p2_id"])
            allowed_ids = (p1_id,) if self._is_computer_game(game) else (p1_id, p2_id)
            if interaction.user.id not in allowed_ids:
                await self._interaction_error(interaction, "Not your game.")
                return

            self._remove_game(guild_id, channel_id)
            await interaction.message.edit(  # type: ignore[union-attr]
                content=final_content(game, cancelled_id=interaction.user.id),
                view=Connect4View(self, game, finished=True),
            )

    @app_commands.command(
        name="connect4",
        description="Play Connect Four",
    )
    @app_commands.describe(opponent="Opponent (leave blank to play the computer)")
    async def connect4(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None = None,
    ) -> None:
        log_cmd("connect4", interaction)
        await ensure_deferred(interaction, ephemeral=False)
        if opponent is None:
            await self.start_computer_game(interaction)
        else:
            await self.start_game(interaction, opponent)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = Connect4Cog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
