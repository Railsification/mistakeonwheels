# cogs/othello.py
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.game_stats import record_head_to_head_result, register_game
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.storage import load_guild_json, save_guild_json
from core.utils import ensure_deferred


GAMES_FILENAME = "othello_games.json"
BOARD_SIZE = 8
EMPTY = 0
BLACK = 1
WHITE = 2
ROW_LABELS = "ABCDEFGH"
DIRECTIONS = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_board() -> list[list[int]]:
    board = [[EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
    board[3][3] = WHITE
    board[3][4] = BLACK
    board[4][3] = BLACK
    board[4][4] = WHITE
    return board


def _normalise_board(raw: Any) -> list[list[int]]:
    if not isinstance(raw, list) or len(raw) != BOARD_SIZE:
        return new_board()

    board: list[list[int]] = []
    for raw_row in raw:
        if not isinstance(raw_row, list) or len(raw_row) != BOARD_SIZE:
            return new_board()

        row: list[int] = []
        for raw_cell in raw_row:
            try:
                cell = int(raw_cell)
            except (TypeError, ValueError):
                return new_board()

            if cell not in (EMPTY, BLACK, WHITE):
                return new_board()
            row.append(cell)

        board.append(row)

    return board


def other_piece(piece: int) -> int:
    return WHITE if int(piece) == BLACK else BLACK


def _in_bounds(row: int, col: int) -> bool:
    return 0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE


def flips_for_move(
    board: list[list[int]],
    row: int,
    col: int,
    piece: int,
) -> list[tuple[int, int]]:
    if not _in_bounds(row, col) or board[row][col] != EMPTY:
        return []

    opponent = other_piece(piece)
    flips: list[tuple[int, int]] = []

    for dr, dc in DIRECTIONS:
        line: list[tuple[int, int]] = []
        rr = row + dr
        cc = col + dc

        while _in_bounds(rr, cc) and board[rr][cc] == opponent:
            line.append((rr, cc))
            rr += dr
            cc += dc

        if line and _in_bounds(rr, cc) and board[rr][cc] == piece:
            flips.extend(line)

    return flips


def legal_moves(
    board: list[list[int]],
    piece: int,
) -> dict[tuple[int, int], list[tuple[int, int]]]:
    moves: dict[tuple[int, int], list[tuple[int, int]]] = {}

    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            flips = flips_for_move(board, row, col, piece)
            if flips:
                moves[(row, col)] = flips

    return moves


def apply_move(
    board: list[list[int]],
    row: int,
    col: int,
    piece: int,
    flips: list[tuple[int, int]],
) -> None:
    board[row][col] = piece
    for rr, cc in flips:
        board[rr][cc] = piece


def count_discs(board: list[list[int]]) -> tuple[int, int]:
    black = sum(cell == BLACK for row in board for cell in row)
    white = sum(cell == WHITE for row in board for cell in row)
    return int(black), int(white)


def move_name(row: int, col: int) -> str:
    return f"{ROW_LABELS[row]}{col + 1}"


def _parse_move_value(value: str) -> tuple[int, int] | None:
    try:
        row_text, col_text = str(value).split(":", 1)
        row = int(row_text)
        col = int(col_text)
    except (TypeError, ValueError):
        return None

    if not _in_bounds(row, col):
        return None
    return row, col


def render_board(
    board: list[list[int]],
    *,
    moves: dict[tuple[int, int], list[tuple[int, int]]] | None = None,
) -> str:
    moves = moves or {}
    symbols = {
        EMPTY: "·",
        BLACK: "●",
        WHITE: "○",
    }

    lines = ["  1 2 3 4 5 6 7 8"]
    for row in range(BOARD_SIZE):
        cells: list[str] = []
        for col in range(BOARD_SIZE):
            if board[row][col] == EMPTY and (row, col) in moves:
                cells.append("✦")
            else:
                cells.append(symbols[board[row][col]])
        lines.append(f"{ROW_LABELS[row]} " + " ".join(cells))

    return "\n".join(lines)


class OthelloMoveSelect(discord.ui.Select):
    def __init__(
        self,
        service: "OthelloService",
        *,
        chunk_index: int,
        options: list[discord.SelectOption] | None = None,
        template: bool = False,
    ) -> None:
        self.service = service
        self.chunk_index = int(chunk_index)

        if template:
            select_options = [
                discord.SelectOption(label="Move", value="__template__")
            ]
            placeholder = "Choose a legal move..."
            disabled = False
        elif options:
            select_options = options
            if chunk_index == 0:
                placeholder = "Choose a legal move..."
            else:
                placeholder = f"More legal moves ({chunk_index + 1})..."
            disabled = False
        else:
            select_options = [
                discord.SelectOption(
                    label="No additional legal moves",
                    value="__none__",
                )
            ]
            placeholder = "No additional legal moves"
            disabled = True

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=select_options,
            custom_id=f"hotbot:othello:move:{chunk_index}:v1",
            row=chunk_index,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        data = interaction.data if isinstance(interaction.data, dict) else {}
        values = data.get("values") if isinstance(data, dict) else None
        value = str(values[0]) if isinstance(values, list) and values else ""

        if not value or value.startswith("__"):
            await interaction.response.send_message(
                "That move is not available.",
                ephemeral=True,
            )
            return

        await self.service.handle_move(interaction, value)


class OthelloResignButton(discord.ui.Button):
    def __init__(self, service: "OthelloService") -> None:
        super().__init__(
            label="Resign",
            emoji="🏳️",
            style=discord.ButtonStyle.danger,
            custom_id="hotbot:othello:resign:v1",
            row=3,
        )
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.resign(interaction)


class OthelloCancelButton(discord.ui.Button):
    def __init__(self, service: "OthelloService") -> None:
        super().__init__(
            label="Cancel",
            emoji="🛑",
            style=discord.ButtonStyle.secondary,
            custom_id="hotbot:othello:cancel:v1",
            row=3,
        )
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.cancel(interaction)


class OthelloView(discord.ui.View):
    def __init__(
        self,
        service: "OthelloService",
        game: dict[str, Any] | None = None,
        *,
        persistent_template: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service

        if persistent_template:
            for chunk_index in range(3):
                self.add_item(
                    OthelloMoveSelect(
                        service,
                        chunk_index=chunk_index,
                        template=True,
                    )
                )
        else:
            options: list[discord.SelectOption] = []
            if game is not None:
                board = _normalise_board(game.get("board"))
                turn = int(game.get("turn") or BLACK)
                moves = legal_moves(board, turn)

                for (row, col), flips in sorted(moves.items()):
                    flip_word = "disc" if len(flips) == 1 else "discs"
                    options.append(
                        discord.SelectOption(
                            label=f"{move_name(row, col)} — flip {len(flips)} {flip_word}",
                            value=f"{row}:{col}",
                            description=f"Place at {move_name(row, col)}",
                        )
                    )

            chunks = [
                options[0:25],
                options[25:50],
                options[50:75],
            ]
            for chunk_index, chunk in enumerate(chunks):
                self.add_item(
                    OthelloMoveSelect(
                        service,
                        chunk_index=chunk_index,
                        options=chunk,
                    )
                )

        self.add_item(OthelloResignButton(service))
        self.add_item(OthelloCancelButton(service))


class OthelloService:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    def _lock_for(self, guild_id: int, channel_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(channel_id))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def allowed(self, guild_id: int | None, channel_id: int | None) -> bool:
        return self.settings.is_game_allowed(guild_id, channel_id, "othello")

    def _load_games_blob(self, guild_id: int) -> dict[str, Any]:
        raw = load_guild_json(guild_id, GAMES_FILENAME, {"games": {}})
        if not isinstance(raw, dict):
            raw = {"games": {}}
        if not isinstance(raw.get("games"), dict):
            raw["games"] = {}
        return raw

    def _save_games_blob(self, guild_id: int, blob: dict[str, Any]) -> None:
        save_guild_json(guild_id, GAMES_FILENAME, blob)

    def _get_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        blob = self._load_games_blob(guild_id)
        game = blob["games"].get(str(channel_id))
        return game if isinstance(game, dict) else None

    def _set_game(
        self,
        guild_id: int,
        channel_id: int,
        game: dict[str, Any],
    ) -> None:
        blob = self._load_games_blob(guild_id)
        blob["games"][str(channel_id)] = game
        self._save_games_blob(guild_id, blob)

    def _remove_game(self, guild_id: int, channel_id: int) -> None:
        blob = self._load_games_blob(guild_id)
        blob["games"].pop(str(channel_id), None)
        self._save_games_blob(guild_id, blob)

    @staticmethod
    def _player_ids(game: dict[str, Any]) -> tuple[int, int]:
        return int(game.get("black_id") or 0), int(game.get("white_id") or 0)

    @staticmethod
    def _player_for_piece(game: dict[str, Any], piece: int) -> int:
        if int(piece) == BLACK:
            return int(game.get("black_id") or 0)
        return int(game.get("white_id") or 0)

    def is_active_game_message(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild_id is None
            or interaction.channel_id is None
            or interaction.message is None
        ):
            return False

        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if not game:
            return False

        return int(game.get("message_id") or 0) == interaction.message.id

    def _build_embed(
        self,
        game: dict[str, Any],
        *,
        status: str = "active",
        winner_id: int | None = None,
        resigned_by: int | None = None,
        cancelled_by: int | None = None,
    ) -> discord.Embed:
        board = _normalise_board(game.get("board"))
        black_id, white_id = self._player_ids(game)
        black_score, white_score = count_discs(board)

        moves: dict[tuple[int, int], list[tuple[int, int]]] = {}
        if status == "active":
            turn = int(game.get("turn") or BLACK)
            moves = legal_moves(board, turn)

        title = "⚫ Othello"
        if status != "active":
            title += " — Game Over"

        embed = discord.Embed(
            title=title,
            description=f"```text\n{render_board(board, moves=moves)}\n```",
        )
        embed.add_field(
            name="Players",
            value=f"● Black — <@{black_id}>\n○ White — <@{white_id}>",
            inline=True,
        )
        embed.add_field(
            name="Score",
            value=f"● **{black_score}**  |  ○ **{white_score}**",
            inline=True,
        )

        if status == "active":
            turn = int(game.get("turn") or BLACK)
            current_id = self._player_for_piece(game, turn)
            piece_name = "Black ●" if turn == BLACK else "White ○"
            last_move = str(game.get("last_move") or "").strip()
            turn_text = f"{piece_name}\n<@{current_id}>"
            if last_move:
                turn_text += f"\nLast move: **{last_move}**"

            embed.add_field(name="Turn", value=turn_text, inline=False)

            notice = str(game.get("notice") or "").strip()
            if notice:
                embed.add_field(name="Pass", value=notice, inline=False)

            embed.add_field(
                name="Legal moves",
                value=(
                    f"**{len(moves)}** available. `✦` marks every legal square. "
                    "Choose one from the dropdown below."
                ),
                inline=False,
            )
            embed.set_footer(
                text="Trap the opponent's discs between yours to flip them. Most discs at the end wins. No timeout."
            )
        elif status == "finished":
            if winner_id is None:
                embed.add_field(
                    name="Result",
                    value=f"🤝 **Draw** — {black_score} to {white_score}",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Result",
                    value=f"🏆 <@{winner_id}> wins **{max(black_score, white_score)}–{min(black_score, white_score)}**!",
                    inline=False,
                )
            embed.set_footer(text="Use /othello or /games to start another game.")
        elif status == "resigned":
            embed.add_field(
                name="Result",
                value=f"🏳️ <@{resigned_by}> resigned. <@{winner_id}> wins!",
                inline=False,
            )
            embed.set_footer(text="Use /othello or /games to start another game.")
        elif status == "cancelled":
            embed.add_field(
                name="Result",
                value=f"🛑 Game cancelled by <@{cancelled_by}>. No result recorded.",
                inline=False,
            )
            embed.set_footer(text="Use /othello or /games to start another game.")

        return embed

    def _record_result(
        self,
        guild_id: int,
        game: dict[str, Any],
        winner_id: int | None,
    ) -> None:
        black_id, white_id = self._player_ids(game)
        game_id = str(game.get("game_id") or "").strip()
        if not game_id or not black_id or not white_id:
            return

        try:
            record_head_to_head_result(
                guild_id,
                "othello",
                black_id,
                white_id,
                winner_id=winner_id,
                result_id=f"othello:{game_id}",
            )
        except Exception as exc:
            warn(f"othello stats record failed: {exc!r}")

    async def _edit_active_message(
        self,
        interaction: discord.Interaction,
        game: dict[str, Any],
    ) -> None:
        if interaction.message is None:
            return

        try:
            await interaction.message.edit(
                embed=self._build_embed(game),
                view=OthelloView(self, game),
            )
        except Exception as exc:
            warn(f"othello active message edit failed: {exc!r}")

    async def _edit_finished_message(
        self,
        interaction: discord.Interaction,
        game: dict[str, Any],
        *,
        status: str,
        winner_id: int | None = None,
        resigned_by: int | None = None,
        cancelled_by: int | None = None,
    ) -> None:
        if interaction.message is None:
            return

        try:
            await interaction.message.edit(
                embed=self._build_embed(
                    game,
                    status=status,
                    winner_id=winner_id,
                    resigned_by=resigned_by,
                    cancelled_by=cancelled_by,
                ),
                view=None,
            )
        except Exception as exc:
            warn(f"othello final message edit failed: {exc!r}")

    async def start_game(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
    ) -> None:
        guild = interaction.guild
        channel_id = interaction.channel_id

        if guild is None or channel_id is None:
            await interaction.followup.send(
                "❌ Othello must be started in a server channel.",
                ephemeral=True,
            )
            return

        if not self.allowed(guild.id, channel_id):
            await interaction.followup.send(
                "❌ Othello can only be used in the configured Othello/game channel(s).",
                ephemeral=True,
            )
            return

        if opponent.bot or opponent.id == interaction.user.id:
            await interaction.followup.send(
                "❌ Pick a real opponent.",
                ephemeral=True,
            )
            return

        async with self._lock_for(guild.id, channel_id):
            existing = self._get_game(guild.id, channel_id)
            if existing:
                message_id = int(existing.get("message_id") or 0)
                jump = (
                    f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"
                    if message_id
                    else ""
                )
                text = "An Othello game is already running in this channel."
                if jump:
                    text += f" [Open it]({jump})"
                await interaction.followup.send(text, ephemeral=True)
                return

            game: dict[str, Any] = {
                "game_id": uuid.uuid4().hex,
                "guild_id": guild.id,
                "channel_id": channel_id,
                "message_id": 0,
                "black_id": interaction.user.id,
                "white_id": opponent.id,
                "turn": BLACK,
                "board": new_board(),
                "last_move": "",
                "notice": "",
                "move_number": 0,
                "created_at": _utc_now(),
            }

            message = await interaction.followup.send(
                embed=self._build_embed(game),
                view=OthelloView(self, game),
                ephemeral=False,
                wait=True,
            )
            game["message_id"] = int(message.id)
            self._set_game(guild.id, channel_id, game)

    async def handle_move(
        self,
        interaction: discord.Interaction,
        raw_move: str,
    ) -> None:
        if (
            interaction.guild_id is None
            or interaction.channel_id is None
            or interaction.message is None
        ):
            await interaction.response.send_message(
                "That Othello game is no longer available.",
                ephemeral=True,
            )
            return

        parsed = _parse_move_value(raw_move)
        if parsed is None:
            await interaction.response.send_message(
                "That move is invalid.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        row, col = parsed

        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != interaction.message.id:
                await interaction.response.send_message(
                    "That is not the current Othello game in this channel.",
                    ephemeral=True,
                )
                return

            board = _normalise_board(game.get("board"))
            turn = int(game.get("turn") or BLACK)
            current_id = self._player_for_piece(game, turn)
            black_id, white_id = self._player_ids(game)

            if interaction.user.id not in (black_id, white_id):
                await interaction.response.send_message(
                    "This isn't your game.",
                    ephemeral=True,
                )
                return

            if interaction.user.id != current_id:
                await interaction.response.send_message(
                    "Not your turn.",
                    ephemeral=True,
                )
                return

            moves = legal_moves(board, turn)
            flips = moves.get((row, col))
            if not flips:
                await interaction.response.send_message(
                    "That square is not a legal move anymore. Pick one marked `✦`.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()

            apply_move(board, row, col, turn, flips)
            game["board"] = board
            game["last_move"] = move_name(row, col)
            game["move_number"] = int(game.get("move_number") or 0) + 1
            game["notice"] = ""

            next_turn = other_piece(turn)
            next_moves = legal_moves(board, next_turn)

            if next_moves:
                game["turn"] = next_turn
                self._set_game(guild_id, channel_id, game)
                await self._edit_active_message(interaction, game)
                return

            same_player_moves = legal_moves(board, turn)
            if same_player_moves:
                skipped_id = self._player_for_piece(game, next_turn)
                game["turn"] = turn
                game["notice"] = (
                    f"<@{skipped_id}> has no legal moves, so their turn was skipped."
                )
                self._set_game(guild_id, channel_id, game)
                await self._edit_active_message(interaction, game)
                return

            black_score, white_score = count_discs(board)
            if black_score > white_score:
                winner_id: int | None = black_id
            elif white_score > black_score:
                winner_id = white_id
            else:
                winner_id = None

            self._remove_game(guild_id, channel_id)
            self._record_result(guild_id, game, winner_id)
            await self._edit_finished_message(
                interaction,
                game,
                status="finished",
                winner_id=winner_id,
            )

    async def resign(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild_id is None
            or interaction.channel_id is None
            or interaction.message is None
        ):
            await interaction.response.send_message(
                "That Othello game is no longer available.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != interaction.message.id:
                await interaction.response.send_message(
                    "That is not the current Othello game in this channel.",
                    ephemeral=True,
                )
                return

            black_id, white_id = self._player_ids(game)
            if interaction.user.id not in (black_id, white_id):
                await interaction.response.send_message(
                    "This isn't your game.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()

            winner_id = white_id if interaction.user.id == black_id else black_id
            self._remove_game(guild_id, channel_id)
            self._record_result(guild_id, game, winner_id)
            await self._edit_finished_message(
                interaction,
                game,
                status="resigned",
                winner_id=winner_id,
                resigned_by=interaction.user.id,
            )

    async def cancel(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild_id is None
            or interaction.channel_id is None
            or interaction.message is None
        ):
            await interaction.response.send_message(
                "That Othello game is no longer available.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != interaction.message.id:
                await interaction.response.send_message(
                    "That is not the current Othello game in this channel.",
                    ephemeral=True,
                )
                return

            black_id, white_id = self._player_ids(game)
            if interaction.user.id not in (black_id, white_id):
                await interaction.response.send_message(
                    "This isn't your game.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()

            self._remove_game(guild_id, channel_id)
            await self._edit_finished_message(
                interaction,
                game,
                status="cancelled",
                cancelled_by=interaction.user.id,
            )


class OthelloCog(commands.Cog):
    GAME_META = {
        "key": "othello",
        "label": "Othello",
        "kind": "head_to_head",
        "result_word": "win",
        "description": "Flip your opponent's discs on an 8×8 Reversi board",
        "emoji": "⚫",
        "requires_opponent": True,
    }

    HELP_META = {
        "title": "Othello",
        "summary": "A persistent two-player Othello/Reversi game with legal-move dropdowns and leaderboard results.",
        "details": "Use /othello or choose Othello from /games, pick an opponent, then choose a legal square from the move dropdown. The board marks legal moves with ✦. Games have no timeout and survive normal bot restarts.",
    }

    def __init__(self, bot: commands.Bot, service: OthelloService) -> None:
        self.bot = bot
        self.service = service

        register_game(
            "othello",
            label="Othello",
            kind="head_to_head",
            result_word="win",
        )

    async def start_game(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
    ) -> None:
        await self.service.start_game(interaction, opponent)

    @app_commands.command(name="othello", description="Play Othello / Reversi")
    @app_commands.describe(opponent="Who you want to play against")
    async def othello(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
    ) -> None:
        log_cmd("othello", interaction)
        if not await ensure_deferred(interaction, ephemeral=False):
            return
        await self.start_game(interaction, opponent)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    service = OthelloService(bot)

    # One persistent template handles every active Othello message after a
    # Railway restart. The real move options are rebuilt whenever a move is made.
    bot.add_view(OthelloView(service, persistent_template=True))

    cog = OthelloCog(bot, service)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
