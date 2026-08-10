# cogs/checkers.py
from __future__ import annotations

import asyncio
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.game_stats import record_head_to_head_result, register_game
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.storage import known_guild_dirs, load_guild_json, save_guild_json
from core.utils import ensure_deferred


GAMES_FILENAME = "checkers_games.json"
FILES = "abcdefgh"
RED = "r"
WHITE = "w"

PIECE_SYMBOLS = {
    "r": "●",
    "R": "◆",
    "w": "○",
    "W": "◇",
}


@dataclass(frozen=True)
class CheckersMove:
    path: tuple[tuple[int, int], ...]
    captures: tuple[tuple[int, int], ...] = ()

    def notation(self) -> str:
        sep = "x" if self.captures else "-"
        return sep.join(rc_to_square(row, col) for row, col in self.path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_board() -> list[list[str]]:
    board = [["." for _ in range(8)] for _ in range(8)]
    for row in range(3):
        for col in range(8):
            if (row + col) % 2 == 1:
                board[row][col] = "w"
    for row in range(5, 8):
        for col in range(8):
            if (row + col) % 2 == 1:
                board[row][col] = "r"
    return board


def valid_board(raw: Any) -> list[list[str]]:
    allowed = {".", "r", "R", "w", "W"}
    if not isinstance(raw, list) or len(raw) != 8:
        return initial_board()
    out: list[list[str]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) != 8:
            return initial_board()
        out.append([str(cell) if str(cell) in allowed else "." for cell in row])
    return out


def square_to_rc(square: str) -> tuple[int, int]:
    value = str(square or "").strip().lower()
    if not re.fullmatch(r"[a-h][1-8]", value):
        raise ValueError(f"Invalid checkers square: {square!r}")
    col = FILES.index(value[0])
    row = 8 - int(value[1])
    return row, col


def rc_to_square(row: int, col: int) -> str:
    if not (0 <= row < 8 and 0 <= col < 8):
        raise ValueError("Checkers row/column out of range")
    return f"{FILES[col]}{8 - row}"


def piece_side(piece: str) -> str | None:
    if piece in ("r", "R"):
        return RED
    if piece in ("w", "W"):
        return WHITE
    return None


def opponent(side: str) -> str:
    return WHITE if side == RED else RED


def _directions(piece: str) -> tuple[tuple[int, int], ...]:
    if piece == "r":
        return ((-1, -1), (-1, 1))
    if piece == "w":
        return ((1, -1), (1, 1))
    return ((-1, -1), (-1, 1), (1, -1), (1, 1))


def _capture_sequences_from(
    board: list[list[str]],
    row: int,
    col: int,
    piece: str,
    captured: tuple[tuple[int, int], ...] = (),
    path: tuple[tuple[int, int], ...] | None = None,
) -> list[CheckersMove]:
    if path is None:
        path = ((row, col),)

    side = piece_side(piece)
    if side is None:
        return []

    found: list[CheckersMove] = []
    made_jump = False
    captured_set = set(captured)

    for dr, dc in _directions(piece):
        mid_r, mid_c = row + dr, col + dc
        land_r, land_c = row + 2 * dr, col + 2 * dc
        if not (0 <= mid_r < 8 and 0 <= mid_c < 8 and 0 <= land_r < 8 and 0 <= land_c < 8):
            continue
        if piece_side(board[mid_r][mid_c]) != opponent(side):
            continue
        if (mid_r, mid_c) in captured_set:
            continue
        if board[land_r][land_c] != ".":
            continue

        made_jump = True
        next_captured = captured + ((mid_r, mid_c),)
        next_path = path + ((land_r, land_c),)

        # Under English/American checkers rules, reaching the king-row ends a man's
        # capturing turn; it is crowned for its next turn.
        crown_row = 0 if side == RED else 7
        if piece.islower() and land_r == crown_row:
            found.append(CheckersMove(next_path, next_captured))
            continue

        # Captured pieces remain on the board until the capture sequence completes.
        # We move only the jumping piece while remembering which enemy pieces have
        # already been jumped so none can be captured twice.
        temp = [r[:] for r in board]
        temp[row][col] = "."
        temp[land_r][land_c] = piece
        continuations = _capture_sequences_from(
            temp,
            land_r,
            land_c,
            piece,
            next_captured,
            next_path,
        )
        if continuations:
            found.extend(continuations)
        else:
            found.append(CheckersMove(next_path, next_captured))

    if not made_jump and captured:
        return [CheckersMove(path, captured)]
    return found


def legal_moves(board: list[list[str]], side: str) -> list[CheckersMove]:
    captures: list[CheckersMove] = []
    for row in range(8):
        for col in range(8):
            piece = board[row][col]
            if piece_side(piece) == side:
                captures.extend(_capture_sequences_from(board, row, col, piece))
    if captures:
        # Remove duplicate paths if a recursive branch returned the same completed jump.
        unique: dict[tuple[tuple[int, int], ...], CheckersMove] = {}
        for move in captures:
            unique[move.path] = move
        return list(unique.values())

    moves: list[CheckersMove] = []
    for row in range(8):
        for col in range(8):
            piece = board[row][col]
            if piece_side(piece) != side:
                continue
            for dr, dc in _directions(piece):
                rr, cc = row + dr, col + dc
                if 0 <= rr < 8 and 0 <= cc < 8 and board[rr][cc] == ".":
                    moves.append(CheckersMove(((row, col), (rr, cc))))
    return moves


def apply_move(
    board: list[list[str]],
    move: CheckersMove,
) -> tuple[list[list[str]], bool, bool]:
    new_board = [row[:] for row in board]
    start_r, start_c = move.path[0]
    end_r, end_c = move.path[-1]
    piece = new_board[start_r][start_c]
    side = piece_side(piece)
    if side is None:
        return new_board, False, False

    new_board[start_r][start_c] = "."
    for rr, cc in move.captures:
        new_board[rr][cc] = "."

    crowned = False
    if piece == "r" and end_r == 0:
        piece = "R"
        crowned = True
    elif piece == "w" and end_r == 7:
        piece = "W"
        crowned = True
    new_board[end_r][end_c] = piece
    return new_board, bool(move.captures), crowned


def normalise_move_path(raw: str) -> tuple[tuple[int, int], ...] | None:
    squares = re.findall(r"[a-hA-H][1-8]", str(raw or ""))
    if len(squares) < 2:
        return None
    try:
        return tuple(square_to_rc(square) for square in squares)
    except ValueError:
        return None


def find_legal_move(raw: str, moves: list[CheckersMove]) -> CheckersMove | None:
    path = normalise_move_path(raw)
    if path is None:
        return None
    return next((move for move in moves if move.path == path), None)


def _position_key(board: list[list[str]], side: str) -> str:
    return "/".join("".join(row) for row in board) + f"|{side}"


def evaluate_board(board: list[list[str]]) -> int:
    score = 0
    for row in range(8):
        for col in range(8):
            piece = board[row][col]
            if piece == ".":
                continue
            if piece == "w":
                value = 100 + row * 3
            elif piece == "W":
                value = 175
            elif piece == "r":
                value = -(100 + (7 - row) * 3)
            else:
                value = -175
            # Small centre bonus.
            centre = int(6 - abs(3.5 - row) - abs(3.5 - col))
            value += centre if value > 0 else -centre
            score += value
    return score


def _minimax(
    board: list[list[str]],
    side: str,
    depth: int,
    alpha: int,
    beta: int,
) -> int:
    moves = legal_moves(board, side)
    if not moves:
        return -100000 if side == WHITE else 100000
    if depth <= 0:
        return evaluate_board(board)

    if side == WHITE:
        value = -10**9
        for move in moves:
            board2, _capture, _crowned = apply_move(board, move)
            value = max(value, _minimax(board2, RED, depth - 1, alpha, beta))
            alpha = max(alpha, value)
            if alpha >= beta:
                break
        return value

    value = 10**9
    for move in moves:
        board2, _capture, _crowned = apply_move(board, move)
        value = min(value, _minimax(board2, WHITE, depth - 1, alpha, beta))
        beta = min(beta, value)
        if alpha >= beta:
            break
    return value


def choose_computer_move(board: list[list[str]]) -> CheckersMove | None:
    moves = legal_moves(board, WHITE)
    if not moves:
        return None
    random.shuffle(moves)

    best_score = -10**9
    best: list[CheckersMove] = []
    for move in moves:
        board2, capture, crowned = apply_move(board, move)
        score = _minimax(board2, RED, 3, -10**9, 10**9)
        if capture:
            score += len(move.captures) * 2
        if crowned:
            score += 8
        if score > best_score:
            best_score = score
            best = [move]
        elif score == best_score:
            best.append(move)
    return random.choice(best)


def render_board(board: list[list[str]]) -> str:
    lines = ["    a b c d e f g h"]
    for row in range(8):
        cells: list[str] = []
        for col in range(8):
            piece = board[row][col]
            if piece != ".":
                cells.append(PIECE_SYMBOLS[piece])
            else:
                cells.append("·" if (row + col) % 2 == 0 else "_")
        rank = 8 - row
        lines.append(f"{rank}   " + " ".join(cells) + f"   {rank}")
    lines.append("    a b c d e f g h")
    return "\n".join(lines)


def render_picker_board(
    board: list[list[str]],
    hints: set[tuple[int, int]] | None = None,
) -> str:
    """Render the board with legal landing squares marked by * for the picker UI."""
    hints = hints or set()
    lines = ["    a b c d e f g h"]
    for row in range(8):
        cells: list[str] = []
        for col in range(8):
            if (row, col) in hints:
                cells.append("*")
                continue
            piece = board[row][col]
            if piece != ".":
                cells.append(PIECE_SYMBOLS[piece])
            else:
                cells.append("·" if (row + col) % 2 == 0 else "_")
        rank = 8 - row
        lines.append(f"{rank}   " + " ".join(cells) + f"   {rank}")
    lines.append("    a b c d e f g h")
    return "\n".join(lines)


def _moves_with_prefix(
    moves: list[CheckersMove],
    prefix: tuple[tuple[int, int], ...],
) -> list[CheckersMove]:
    if not prefix:
        return list(moves)
    return [
        move
        for move in moves
        if len(move.path) >= len(prefix) and move.path[: len(prefix)] == prefix
    ]


def _next_squares(
    moves: list[CheckersMove],
    prefix: tuple[tuple[int, int], ...],
) -> list[tuple[int, int]]:
    choices = {
        move.path[len(prefix)]
        for move in _moves_with_prefix(moves, prefix)
        if len(move.path) > len(prefix)
    }
    return sorted(choices, key=lambda rc: rc_to_square(*rc))


def _player_label(game: dict[str, Any], side: str) -> str:
    player_id = int(game["p1_id"] if side == RED else game["p2_id"])
    if bool(game.get("computer")) and side == WHITE:
        return "🤖 Computer"
    return f"<@{player_id}>"


def active_content(game: dict[str, Any]) -> str:
    board = valid_board(game.get("board"))
    side = str(game.get("turn") or RED)
    last = str(game.get("last_move") or "").strip()
    moves = legal_moves(board, side)
    capture_required = bool(moves and moves[0].captures)
    status = f"{_player_label(game, side)} to move"
    if capture_required:
        status += " — **jump required**"
    if bool(game.get("computer")) and side == WHITE:
        status = "🤖 Computer is thinking..."

    last_line = f"\nLast move: `{last}`" if last else ""
    return (
        "🔴 **Checkers**\n"
        f"Red: {_player_label(game, RED)}  •  White: {_player_label(game, WHITE)}\n"
        f"{status}{last_line}\n"
        f"```\n{render_board(board)}\n```"
        "`●/○` = man, `◆/◇` = king. Press **Move**, choose one of your movable "
        "pieces, then choose one of the legal landing squares shown. Forced multi-jumps "
        "are stepped through automatically."
    )


def final_content(
    game: dict[str, Any],
    *,
    winner_side: str | None = None,
    draw_reason: str | None = None,
    cancelled: bool = False,
) -> str:
    board = valid_board(game.get("board"))
    if cancelled:
        result = "🛑 Game cancelled."
    elif winner_side:
        result = f"🏁 **{_player_label(game, winner_side)} wins!**"
    else:
        result = f"🤝 **Draw**{f' — {draw_reason}' if draw_reason else '.'}"
    return (
        "🔴 **Checkers — Game Over**\n"
        f"{result}\n"
        f"```\n{render_board(board)}\n```"
    )


class CheckersMoveModal(discord.ui.Modal, title="Checkers move"):
    move_text = discord.ui.TextInput(
        label="Move",
        placeholder="b6-a5   or   c3-e5-g7",
        required=True,
        max_length=50,
    )

    def __init__(
        self,
        cog: "CheckersCog",
        guild_id: int,
        channel_id: int,
        message_id: int,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.message_id = int(message_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_move(
            interaction,
            str(self.move_text.value),
            self.guild_id,
            self.channel_id,
            self.message_id,
        )


class CheckersMoveButton(discord.ui.Button):
    def __init__(self, cog: "CheckersCog"):
        super().__init__(
            label="Move",
            emoji="🔴",
            style=discord.ButtonStyle.primary,
            custom_id="hotbot:checkers:move:v1",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.open_move_picker(interaction)


class CheckersPickerSelect(discord.ui.Select):
    def __init__(
        self,
        picker: "CheckersMovePickerView",
        *,
        mode: str,
        options: list[discord.SelectOption],
    ):
        placeholder = "Choose a piece to move…" if mode == "piece" else "Choose a landing square…"
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )
        self.picker = picker
        self.mode = mode

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.picker.user_id:
            await interaction.response.send_message(
                "❌ This move picker belongs to the player whose turn it is.",
                ephemeral=True,
            )
            return
        try:
            chosen = square_to_rc(self.values[0])
        except ValueError:
            await interaction.response.send_message(
                "That square is no longer available. Open **Move** again.",
                ephemeral=True,
            )
            return
        await self.picker.cog.handle_picker_choice(
            interaction,
            guild_id=self.picker.guild_id,
            channel_id=self.picker.channel_id,
            message_id=self.picker.message_id,
            prefix=self.picker.prefix,
            chosen=chosen,
            mode=self.mode,
            user_id=self.picker.user_id,
        )


class CheckersPickerBackButton(discord.ui.Button):
    def __init__(self, picker: "CheckersMovePickerView"):
        super().__init__(label="Back", emoji="↩️", style=discord.ButtonStyle.secondary)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.picker.user_id:
            await interaction.response.send_message(
                "❌ This move picker belongs to the player whose turn it is.",
                ephemeral=True,
            )
            return
        await self.picker.cog.refresh_move_picker(
            interaction,
            guild_id=self.picker.guild_id,
            channel_id=self.picker.channel_id,
            message_id=self.picker.message_id,
            prefix=(),
            user_id=self.picker.user_id,
        )


class CheckersPickerCancelButton(discord.ui.Button):
    def __init__(self, picker: "CheckersMovePickerView"):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.picker.user_id:
            await interaction.response.send_message(
                "❌ This move picker belongs to the player whose turn it is.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(content="Move selection cancelled.", view=None)


class CheckersMovePickerView(discord.ui.View):
    def __init__(
        self,
        cog: "CheckersCog",
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
        board: list[list[str]],
        moves: list[CheckersMove],
        prefix: tuple[tuple[int, int], ...] = (),
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.message_id = int(message_id)
        self.user_id = int(user_id)
        self.board = valid_board(board)
        self.moves = list(moves)
        self.prefix = tuple(prefix)

        if not self.prefix:
            starts = sorted(
                {move.path[0] for move in self.moves},
                key=lambda rc: rc_to_square(*rc),
            )
            options: list[discord.SelectOption] = []
            for rc in starts:
                square = rc_to_square(*rc)
                piece = self.board[rc[0]][rc[1]]
                branch_count = sum(1 for move in self.moves if move.path[0] == rc)
                capture_count = max(
                    (len(move.captures) for move in self.moves if move.path[0] == rc),
                    default=0,
                )
                kind = "king" if piece.isupper() else "man"
                if capture_count:
                    description = f"{kind} • jump available"
                else:
                    description = f"{kind} • {branch_count} legal move{'s' if branch_count != 1 else ''}"
                options.append(
                    discord.SelectOption(
                        label=square.upper(),
                        value=square,
                        description=description[:100],
                    )
                )
            if options:
                self.add_item(CheckersPickerSelect(self, mode="piece", options=options[:25]))
        else:
            options = []
            current_moves = _moves_with_prefix(self.moves, self.prefix)
            for rc in _next_squares(self.moves, self.prefix):
                square = rc_to_square(*rc)
                extended = self.prefix + (rc,)
                branches = _moves_with_prefix(current_moves, extended)
                continues = any(len(move.path) > len(extended) for move in branches)
                total_captures = max((len(move.captures) for move in branches), default=0)
                description = "continue jump" if continues else "finish move"
                if total_captures:
                    description += f" • {total_captures} capture{'s' if total_captures != 1 else ''}"
                options.append(
                    discord.SelectOption(
                        label=square.upper(),
                        value=square,
                        description=description[:100],
                    )
                )
            if options:
                self.add_item(CheckersPickerSelect(self, mode="landing", options=options[:25]))
            self.add_item(CheckersPickerBackButton(self))

        self.add_item(CheckersPickerCancelButton(self))

    def content(self) -> str:
        jump_required = bool(self.moves and self.moves[0].captures)
        if not self.prefix:
            hints: set[tuple[int, int]] = set()
            instruction = "Choose one of your movable pieces below."
            if jump_required:
                instruction += " **A capture is compulsory**, so only pieces that can jump are listed."
        else:
            hints = set(_next_squares(self.moves, self.prefix))
            path_text = " → ".join(rc_to_square(*rc).upper() for rc in self.prefix)
            instruction = (
                f"Selected: **{path_text}**\n"
                "Choose one of the `*` landing squares below."
            )
            if jump_required:
                instruction += " If another jump is required, the picker will continue automatically."
        text = (
            f"🔴 **Choose your Checkers move**\n{instruction}\n"
            f"```\n{render_picker_board(self.board, hints)}\n```"
        )
        if self.prefix:
            text += "`*` = available landing square"
        return text


class CheckersResignButton(discord.ui.Button):
    def __init__(self, cog: "CheckersCog"):
        super().__init__(
            label="Resign",
            style=discord.ButtonStyle.danger,
            custom_id="hotbot:checkers:resign:v1",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_resign(interaction)


class CheckersCancelButton(discord.ui.Button):
    def __init__(self, cog: "CheckersCog"):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="hotbot:checkers:cancel:v1",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_cancel(interaction)


class CheckersView(discord.ui.View):
    def __init__(self, cog: "CheckersCog", *, finished: bool = False):
        super().__init__(timeout=None)
        self.add_item(CheckersMoveButton(cog))
        self.add_item(CheckersResignButton(cog))
        self.add_item(CheckersCancelButton(cog))
        if finished:
            for child in self.children:
                child.disabled = True


class CheckersCog(commands.Cog):
    GAME_META = {
        "key": "checkers",
        "label": "Checkers",
        "kind": "head_to_head",
        "result_word": "win",
        "description": "English/American Checkers with persistent games and Computer mode",
        "emoji": "🔴",
        "requires_opponent": True,
    }

    HELP_META = {
        "title": "Checkers",
        "summary": "Persistent English/American Checkers for PvP or Computer mode.",
        "details": (
            "Use /checkers and optionally choose an opponent. Leave opponent blank for "
            "Computer mode. Press Move, choose one of your movable pieces, then choose "
            "one of its legal landing squares. Captures are compulsory and forced "
            "multi-jumps are stepped through automatically."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._restored_once = False
        register_game("checkers", label="Checkers", kind="head_to_head", result_word="win")

    async def cog_load(self) -> None:
        self.bot.add_view(CheckersView(self))

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
        raw = load_guild_json(guild_id, GAMES_FILENAME, {"games": {}})
        if not isinstance(raw, dict):
            raw = {"games": {}}
        if not isinstance(raw.get("games"), dict):
            raw["games"] = {}
        return raw

    def _save_blob(self, guild_id: int, blob: dict[str, Any]) -> None:
        save_guild_json(guild_id, GAMES_FILENAME, blob)

    def _get_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        game = self._load_blob(guild_id)["games"].get(str(channel_id))
        if not isinstance(game, dict):
            return None
        game["board"] = valid_board(game.get("board"))
        game["turn"] = RED if game.get("turn") != WHITE else WHITE
        game["computer"] = bool(game.get("computer"))
        if not isinstance(game.get("position_history"), list):
            game["position_history"] = []
        return game

    def _set_game(self, guild_id: int, channel_id: int, game: dict[str, Any]) -> None:
        blob = self._load_blob(guild_id)
        blob["games"][str(channel_id)] = game
        self._save_blob(guild_id, blob)

    def _remove_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        blob = self._load_blob(guild_id)
        old = blob["games"].pop(str(channel_id), None)
        self._save_blob(guild_id, blob)
        return old if isinstance(old, dict) else None

    def _jump_link(self, game: dict[str, Any]) -> str:
        gid = int(game.get("guild_id") or 0)
        cid = int(game.get("channel_id") or 0)
        mid = int(game.get("message_id") or 0)
        return f"https://discord.com/channels/{gid}/{cid}/{mid}" if gid and cid and mid else ""

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
        if p1_id and p2_id and game_id:
            record_head_to_head_result(
                guild_id,
                "checkers",
                p1_id,
                p2_id,
                winner_id=winner_id,
                result_id=f"checkers:{game_id}",
            )

    async def _fetch_message(self, channel_id: int, message_id: int) -> discord.Message | None:
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            return await channel.fetch_message(message_id)  # type: ignore[attr-defined]
        except Exception:
            return None

    async def restore_saved_games(self) -> None:
        for guild_id in known_guild_dirs():
            blob = self._load_blob(guild_id)
            stale: list[str] = []
            for channel_key, game in list(blob["games"].items()):
                if not isinstance(game, dict):
                    stale.append(channel_key)
                    continue
                channel_id = int(game.get("channel_id") or channel_key or 0)
                message_id = int(game.get("message_id") or 0)
                if not channel_id or not message_id:
                    stale.append(channel_key)
                    continue
                try:
                    message = await self._fetch_message(channel_id, message_id)
                    if message is None:
                        stale.append(channel_key)
                    else:
                        await message.edit(content=active_content(game), view=CheckersView(self))
                except discord.NotFound:
                    stale.append(channel_key)
                except Exception as exc:
                    warn(f"Checkers restore failed for {guild_id}/{channel_id}/{message_id}: {exc!r}")
            if stale:
                for key in stale:
                    blob["games"].pop(key, None)
                self._save_blob(guild_id, blob)

    async def _start(
        self,
        interaction: discord.Interaction,
        *,
        p2_id: int,
        computer: bool,
    ) -> None:
        if interaction.guild is None or interaction.channel_id is None:
            await interaction.followup.send("This game must be started in a server channel.", ephemeral=True)
            return
        if not self.settings.is_game_allowed(interaction.guild_id, interaction.channel_id, "checkers"):
            await interaction.followup.send("❌ Checkers is not enabled in this channel.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            existing = self._get_game(guild_id, channel_id)
            if existing:
                text = "A Checkers game is already running here."
                jump = self._jump_link(existing)
                if jump:
                    text += f" [Open it]({jump})"
                await interaction.followup.send(text, ephemeral=True)
                return

            board = initial_board()
            game: dict[str, Any] = {
                "game_id": uuid.uuid4().hex,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "message_id": 0,
                "p1_id": interaction.user.id,
                "p2_id": int(p2_id),
                "computer": bool(computer),
                "turn": RED,
                "board": board,
                "draw_clock": 0,
                "position_history": [_position_key(board, RED)],
                "last_move": "",
                "created_at": _utc_now(),
            }
            message = await interaction.followup.send(
                content=active_content(game),
                view=CheckersView(self),
                ephemeral=False,
                wait=True,
            )
            game["message_id"] = message.id
            self._set_game(guild_id, channel_id, game)

    async def start_game(self, interaction: discord.Interaction, opponent_member: discord.Member) -> None:
        if opponent_member.bot:
            await interaction.followup.send(
                "❌ Can’t use a Discord bot as the opponent. Choose Computer mode instead.",
                ephemeral=True,
            )
            return
        if opponent_member.id == interaction.user.id:
            await interaction.followup.send("❌ You can’t play yourself.", ephemeral=True)
            return
        await self._start(interaction, p2_id=opponent_member.id, computer=False)

    async def start_computer_game(self, interaction: discord.Interaction) -> None:
        bot_user = self.bot.user or interaction.client.user
        if bot_user is None:
            await interaction.followup.send(
                "❌ Computer mode is unavailable until the bot is fully connected.",
                ephemeral=True,
            )
            return
        await self._start(interaction, p2_id=int(bot_user.id), computer=True)

    def _picker_game_state(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
    ) -> tuple[dict[str, Any] | None, list[CheckersMove], str | None]:
        game = self._get_game(guild_id, channel_id)
        if not game or int(game.get("message_id") or 0) != int(message_id):
            return None, [], "This Checkers game is no longer active."

        turn = str(game.get("turn") or RED)
        p1_id = int(game["p1_id"])
        p2_id = int(game["p2_id"])
        computer = bool(game.get("computer"))
        allowed = (p1_id,) if computer else (p1_id, p2_id)
        expected_id = p1_id if turn == RED else p2_id
        if int(user_id) not in allowed:
            return None, [], "❌ You aren’t playing this game."
        if int(user_id) != expected_id:
            return None, [], "⏳ Not your turn."

        board = valid_board(game.get("board"))
        moves = legal_moves(board, turn)
        if not moves:
            return game, [], "There are no legal moves available."
        return game, moves, None

    async def open_move_picker(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.response.send_message(
                "This Checkers game is no longer active.",
                ephemeral=True,
            )
            return

        game, moves, error = self._picker_game_state(
            interaction.guild_id,
            interaction.channel_id,
            interaction.message.id,
            interaction.user.id,
        )
        if error or game is None:
            await interaction.response.send_message(error or "This Checkers game is no longer active.", ephemeral=True)
            return

        view = CheckersMovePickerView(
            self,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            message_id=interaction.message.id,
            user_id=interaction.user.id,
            board=valid_board(game.get("board")),
            moves=moves,
        )
        await interaction.response.send_message(view.content(), view=view, ephemeral=True)

    async def refresh_move_picker(
        self,
        interaction: discord.Interaction,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        prefix: tuple[tuple[int, int], ...],
        user_id: int,
    ) -> None:
        game, moves, error = self._picker_game_state(
            guild_id, channel_id, message_id, user_id
        )
        if error or game is None:
            await interaction.response.edit_message(
                content=error or "This Checkers game is no longer active.",
                view=None,
            )
            return

        if prefix and not _moves_with_prefix(moves, prefix):
            prefix = ()
        view = CheckersMovePickerView(
            self,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            user_id=user_id,
            board=valid_board(game.get("board")),
            moves=moves,
            prefix=prefix,
        )
        await interaction.response.edit_message(content=view.content(), view=view)

    async def handle_picker_choice(
        self,
        interaction: discord.Interaction,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        prefix: tuple[tuple[int, int], ...],
        chosen: tuple[int, int],
        mode: str,
        user_id: int,
    ) -> None:
        game, moves, error = self._picker_game_state(
            guild_id, channel_id, message_id, user_id
        )
        if error or game is None:
            await interaction.response.edit_message(
                content=error or "This Checkers game is no longer active.",
                view=None,
            )
            return

        if mode == "piece":
            new_prefix = (chosen,)
        else:
            new_prefix = tuple(prefix) + (chosen,)

        matching = _moves_with_prefix(moves, new_prefix)
        if not matching:
            await interaction.response.edit_message(
                content="That option is no longer legal. Press **Move** again to refresh your choices.",
                view=None,
            )
            return

        completed = next((move for move in matching if move.path == new_prefix), None)
        has_continuation = any(len(move.path) > len(new_prefix) for move in matching)
        if completed is not None and not has_continuation:
            await self.handle_move(
                interaction,
                completed.notation(),
                guild_id,
                channel_id,
                message_id,
            )
            try:
                await interaction.delete_original_response()
            except Exception:
                pass
            return

        view = CheckersMovePickerView(
            self,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            user_id=user_id,
            board=valid_board(game.get("board")),
            moves=moves,
            prefix=new_prefix,
        )
        await interaction.response.edit_message(content=view.content(), view=view)

    def _state_after_move(self, game: dict[str, Any], move: CheckersMove, side: str) -> None:
        board = valid_board(game.get("board"))
        start_r, start_c = move.path[0]
        moving_piece = board[start_r][start_c]
        board2, capture, _crowned = apply_move(board, move)
        next_side = opponent(side)
        game["board"] = board2
        game["turn"] = next_side
        game["last_move"] = move.notation()

        # WCDF draw rule uses no captures and no uncrowned-man advances for 40 moves
        # per player. With alternating play, 80 plies is the equivalent counter here.
        if capture or moving_piece.islower():
            game["draw_clock"] = 0
        else:
            game["draw_clock"] = int(game.get("draw_clock") or 0) + 1

        history = game.get("position_history")
        if not isinstance(history, list):
            history = []
        history.append(_position_key(board2, next_side))
        game["position_history"] = history[-300:]

    def _outcome(self, game: dict[str, Any]) -> tuple[str | None, str | None]:
        board = valid_board(game.get("board"))
        side = str(game.get("turn") or RED)
        if not legal_moves(board, side):
            return opponent(side), None
        history = game.get("position_history")
        if isinstance(history, list) and history:
            current = history[-1]
            if sum(1 for key in history if key == current) >= 3:
                return None, "threefold repetition"
        if int(game.get("draw_clock") or 0) >= 80:
            return None, "40-move draw rule"
        return "", None

    async def _finish(
        self,
        message: discord.Message,
        game: dict[str, Any],
        *,
        winner_side: str | None = None,
        draw_reason: str | None = None,
    ) -> None:
        guild_id = int(game["guild_id"])
        channel_id = int(game["channel_id"])
        winner_id: int | None = None
        if winner_side == RED:
            winner_id = int(game["p1_id"])
        elif winner_side == WHITE:
            winner_id = int(game["p2_id"])
        self._record_result(guild_id, game, winner_id)
        self._remove_game(guild_id, channel_id)
        await message.edit(
            content=final_content(game, winner_side=winner_side, draw_reason=draw_reason),
            view=CheckersView(self, finished=True),
        )

    async def handle_move(
        self,
        interaction: discord.Interaction,
        raw_move: str,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != message_id:
                await interaction.followup.send("This Checkers game is no longer active.", ephemeral=True)
                return

            turn = str(game.get("turn") or RED)
            p1_id = int(game["p1_id"])
            p2_id = int(game["p2_id"])
            computer = bool(game.get("computer"))
            allowed = (p1_id,) if computer else (p1_id, p2_id)
            expected_id = p1_id if turn == RED else p2_id
            if interaction.user.id not in allowed:
                await interaction.followup.send("❌ You aren’t playing this game.", ephemeral=True)
                return
            if interaction.user.id != expected_id:
                await interaction.followup.send("⏳ Not your turn.", ephemeral=True)
                return

            board = valid_board(game.get("board"))
            moves = legal_moves(board, turn)
            move = find_legal_move(raw_move, moves)
            if move is None:
                if moves and moves[0].captures:
                    samples = ", ".join(f"`{m.notation()}`" for m in moves[:4])
                    text = (
                        "❌ That jump isn’t legal. A capture is compulsory, and a multiple jump "
                        "must include the whole chain."
                    )
                    if samples:
                        text += f" Legal option{'s' if len(moves) != 1 else ''}: {samples}"
                else:
                    text = "❌ Illegal move. Enter it like `b6-a5`."
                await interaction.followup.send(text, ephemeral=True)
                return

            self._state_after_move(game, move, turn)
            message = await self._fetch_message(channel_id, message_id)
            if message is None:
                self._remove_game(guild_id, channel_id)
                await interaction.followup.send("The Checkers message could not be found, so the saved game was cleared.", ephemeral=True)
                return

            winner_side, draw_reason = self._outcome(game)
            if winner_side or draw_reason:
                await self._finish(message, game, winner_side=winner_side or None, draw_reason=draw_reason)
                return

            if computer and game.get("turn") == WHITE:
                board = valid_board(game.get("board"))
                ai_move = choose_computer_move(board)
                if ai_move is None:
                    winner_side, draw_reason = self._outcome(game)
                    await self._finish(message, game, winner_side=winner_side or None, draw_reason=draw_reason)
                    return
                self._state_after_move(game, ai_move, WHITE)
                winner_side, draw_reason = self._outcome(game)
                if winner_side or draw_reason:
                    await self._finish(message, game, winner_side=winner_side or None, draw_reason=draw_reason)
                    return

            self._set_game(guild_id, channel_id, game)
            await message.edit(content=active_content(game), view=CheckersView(self))

    async def handle_resign(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.followup.send("This Checkers game is no longer active.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != interaction.message.id:
                await interaction.followup.send("This Checkers game is no longer active.", ephemeral=True)
                return
            p1_id = int(game["p1_id"])
            p2_id = int(game["p2_id"])
            computer = bool(game.get("computer"))
            allowed = (p1_id,) if computer else (p1_id, p2_id)
            if interaction.user.id not in allowed:
                await interaction.followup.send("❌ You aren’t playing this game.", ephemeral=True)
                return
            winner_side = WHITE if interaction.user.id == p1_id else RED
            await self._finish(interaction.message, game, winner_side=winner_side)

    async def handle_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.followup.send("This Checkers game is no longer active.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != interaction.message.id:
                await interaction.followup.send("This Checkers game is no longer active.", ephemeral=True)
                return
            if interaction.user.id != int(game["p1_id"]):
                await interaction.followup.send("❌ Only the game starter can cancel.", ephemeral=True)
                return
            self._remove_game(guild_id, channel_id)
            await interaction.message.edit(
                content=final_content(game, cancelled=True),
                view=CheckersView(self, finished=True),
            )

    @app_commands.command(name="checkers", description="Play Checkers")
    @app_commands.describe(opponent="Who to play against — leave blank to play the Computer")
    async def checkers(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None = None,
    ) -> None:
        log_cmd("checkers", interaction)
        if not self.settings.is_game_allowed(interaction.guild_id, interaction.channel_id, "checkers"):
            await interaction.response.send_message(
                "❌ `/checkers` can only be used in the configured game channel(s).",
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
    cog = CheckersCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
