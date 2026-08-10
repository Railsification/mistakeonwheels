# cogs/chess.py
from __future__ import annotations

import asyncio
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.game_stats import record_head_to_head_result, register_game
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.storage import known_guild_dirs, load_guild_json, save_guild_json
from core.utils import ensure_deferred


GAMES_FILENAME = "chess_games.json"
FILES = "abcdefgh"
RANKS = "12345678"
WHITE = "w"
BLACK = "b"

PIECE_SYMBOLS = {
    "K": "♔",
    "Q": "♕",
    "R": "♖",
    "B": "♗",
    "N": "♘",
    "P": "♙",
    "k": "♚",
    "q": "♛",
    "r": "♜",
    "b": "♝",
    "n": "♞",
    "p": "♟",
}
PIECE_VALUES = {
    "p": 100,
    "n": 320,
    "b": 330,
    "r": 500,
    "q": 900,
    "k": 20000,
}


@dataclass(frozen=True)
class ChessMove:
    sr: int
    sc: int
    tr: int
    tc: int
    promotion: str | None = None
    en_passant: bool = False
    castle: str | None = None

    def uci(self) -> str:
        text = rc_to_square(self.sr, self.sc) + rc_to_square(self.tr, self.tc)
        if self.promotion:
            text += self.promotion.lower()
        return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_board() -> list[list[str]]:
    return [
        list("rnbqkbnr"),
        list("pppppppp"),
        list("........"),
        list("........"),
        list("........"),
        list("........"),
        list("PPPPPPPP"),
        list("RNBQKBNR"),
    ]


def valid_board(raw: Any) -> list[list[str]]:
    allowed = set("prnbqkPRNBQK.")
    if not isinstance(raw, list) or len(raw) != 8:
        return initial_board()
    out: list[list[str]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) != 8:
            return initial_board()
        clean = [str(cell) if str(cell) in allowed else "." for cell in row]
        out.append(clean)
    if sum(cell == "K" for row in out for cell in row) != 1:
        return initial_board()
    if sum(cell == "k" for row in out for cell in row) != 1:
        return initial_board()
    return out


def square_to_rc(square: str) -> tuple[int, int]:
    value = str(square or "").strip().lower()
    if not re.fullmatch(r"[a-h][1-8]", value):
        raise ValueError(f"Invalid chess square: {square!r}")
    col = FILES.index(value[0])
    row = 8 - int(value[1])
    return row, col


def rc_to_square(row: int, col: int) -> str:
    if not (0 <= row < 8 and 0 <= col < 8):
        raise ValueError("Chess row/column out of range")
    return f"{FILES[col]}{8 - row}"


def piece_side(piece: str) -> str | None:
    if piece == ".":
        return None
    return WHITE if piece.isupper() else BLACK


def opponent(side: str) -> str:
    return BLACK if side == WHITE else WHITE


def _normalise_castling(raw: Any) -> dict[str, bool]:
    source = raw if isinstance(raw, dict) else {}
    return {key: bool(source.get(key, False)) for key in ("K", "Q", "k", "q")}


def _normalise_en_passant(raw: Any) -> str | None:
    if raw in (None, "", False):
        return None
    try:
        square_to_rc(str(raw))
    except ValueError:
        return None
    return str(raw).lower()


def king_position(board: list[list[str]], side: str) -> tuple[int, int] | None:
    target = "K" if side == WHITE else "k"
    for row in range(8):
        for col in range(8):
            if board[row][col] == target:
                return row, col
    return None


def is_square_attacked(
    board: list[list[str]],
    row: int,
    col: int,
    by_side: str,
) -> bool:
    pawn = "P" if by_side == WHITE else "p"
    pawn_source_delta = 1 if by_side == WHITE else -1
    source_row = row + pawn_source_delta
    if 0 <= source_row < 8:
        for dc in (-1, 1):
            cc = col + dc
            if 0 <= cc < 8 and board[source_row][cc] == pawn:
                return True

    knight = "N" if by_side == WHITE else "n"
    for dr, dc in (
        (-2, -1), (-2, 1), (-1, -2), (-1, 2),
        (1, -2), (1, 2), (2, -1), (2, 1),
    ):
        rr, cc = row + dr, col + dc
        if 0 <= rr < 8 and 0 <= cc < 8 and board[rr][cc] == knight:
            return True

    king = "K" if by_side == WHITE else "k"
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            rr, cc = row + dr, col + dc
            if 0 <= rr < 8 and 0 <= cc < 8 and board[rr][cc] == king:
                return True

    bishop = "B" if by_side == WHITE else "b"
    rook = "R" if by_side == WHITE else "r"
    queen = "Q" if by_side == WHITE else "q"

    for dr, dc in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
        rr, cc = row + dr, col + dc
        while 0 <= rr < 8 and 0 <= cc < 8:
            piece = board[rr][cc]
            if piece != ".":
                if piece in (bishop, queen):
                    return True
                break
            rr += dr
            cc += dc

    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        rr, cc = row + dr, col + dc
        while 0 <= rr < 8 and 0 <= cc < 8:
            piece = board[rr][cc]
            if piece != ".":
                if piece in (rook, queen):
                    return True
                break
            rr += dr
            cc += dc

    return False


def in_check(board: list[list[str]], side: str) -> bool:
    pos = king_position(board, side)
    if pos is None:
        return True
    return is_square_attacked(board, pos[0], pos[1], opponent(side))


def _slide_moves(
    board: list[list[str]],
    side: str,
    row: int,
    col: int,
    directions: Iterable[tuple[int, int]],
) -> list[ChessMove]:
    moves: list[ChessMove] = []
    for dr, dc in directions:
        rr, cc = row + dr, col + dc
        while 0 <= rr < 8 and 0 <= cc < 8:
            target = board[rr][cc]
            if target == ".":
                moves.append(ChessMove(row, col, rr, cc))
            else:
                if piece_side(target) != side:
                    moves.append(ChessMove(row, col, rr, cc))
                break
            rr += dr
            cc += dc
    return moves


def pseudo_moves_for_piece(
    board: list[list[str]],
    side: str,
    row: int,
    col: int,
    castling: dict[str, bool],
    en_passant: str | None,
) -> list[ChessMove]:
    piece = board[row][col]
    if piece_side(piece) != side:
        return []

    kind = piece.lower()
    moves: list[ChessMove] = []

    if kind == "p":
        direction = -1 if side == WHITE else 1
        start_row = 6 if side == WHITE else 1
        promotion_row = 0 if side == WHITE else 7

        one = row + direction
        if 0 <= one < 8 and board[one][col] == ".":
            if one == promotion_row:
                for promo in "qrbn":
                    moves.append(ChessMove(row, col, one, col, promotion=promo))
            else:
                moves.append(ChessMove(row, col, one, col))
                two = row + 2 * direction
                if row == start_row and board[two][col] == ".":
                    moves.append(ChessMove(row, col, two, col))

        ep_rc = square_to_rc(en_passant) if en_passant else None
        for dc in (-1, 1):
            rr, cc = row + direction, col + dc
            if not (0 <= rr < 8 and 0 <= cc < 8):
                continue
            target = board[rr][cc]
            if target != "." and piece_side(target) == opponent(side):
                if rr == promotion_row:
                    for promo in "qrbn":
                        moves.append(ChessMove(row, col, rr, cc, promotion=promo))
                else:
                    moves.append(ChessMove(row, col, rr, cc))
            elif ep_rc == (rr, cc):
                moves.append(ChessMove(row, col, rr, cc, en_passant=True))

    elif kind == "n":
        for dr, dc in (
            (-2, -1), (-2, 1), (-1, -2), (-1, 2),
            (1, -2), (1, 2), (2, -1), (2, 1),
        ):
            rr, cc = row + dr, col + dc
            if 0 <= rr < 8 and 0 <= cc < 8:
                target = board[rr][cc]
                if target == "." or piece_side(target) == opponent(side):
                    moves.append(ChessMove(row, col, rr, cc))

    elif kind == "b":
        moves.extend(_slide_moves(
            board, side, row, col,
            ((-1, -1), (-1, 1), (1, -1), (1, 1)),
        ))

    elif kind == "r":
        moves.extend(_slide_moves(
            board, side, row, col,
            ((-1, 0), (1, 0), (0, -1), (0, 1)),
        ))

    elif kind == "q":
        moves.extend(_slide_moves(
            board, side, row, col,
            (
                (-1, -1), (-1, 1), (1, -1), (1, 1),
                (-1, 0), (1, 0), (0, -1), (0, 1),
            ),
        ))

    elif kind == "k":
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                rr, cc = row + dr, col + dc
                if 0 <= rr < 8 and 0 <= cc < 8:
                    target = board[rr][cc]
                    if target == "." or piece_side(target) == opponent(side):
                        moves.append(ChessMove(row, col, rr, cc))

        enemy = opponent(side)
        if side == WHITE and (row, col) == (7, 4) and piece == "K":
            if (
                castling.get("K")
                and board[7][5] == board[7][6] == "."
                and board[7][7] == "R"
                and not is_square_attacked(board, 7, 4, enemy)
                and not is_square_attacked(board, 7, 5, enemy)
                and not is_square_attacked(board, 7, 6, enemy)
            ):
                moves.append(ChessMove(7, 4, 7, 6, castle="K"))
            if (
                castling.get("Q")
                and board[7][1] == board[7][2] == board[7][3] == "."
                and board[7][0] == "R"
                and not is_square_attacked(board, 7, 4, enemy)
                and not is_square_attacked(board, 7, 3, enemy)
                and not is_square_attacked(board, 7, 2, enemy)
            ):
                moves.append(ChessMove(7, 4, 7, 2, castle="Q"))
        elif side == BLACK and (row, col) == (0, 4) and piece == "k":
            if (
                castling.get("k")
                and board[0][5] == board[0][6] == "."
                and board[0][7] == "r"
                and not is_square_attacked(board, 0, 4, enemy)
                and not is_square_attacked(board, 0, 5, enemy)
                and not is_square_attacked(board, 0, 6, enemy)
            ):
                moves.append(ChessMove(0, 4, 0, 6, castle="k"))
            if (
                castling.get("q")
                and board[0][1] == board[0][2] == board[0][3] == "."
                and board[0][0] == "r"
                and not is_square_attacked(board, 0, 4, enemy)
                and not is_square_attacked(board, 0, 3, enemy)
                and not is_square_attacked(board, 0, 2, enemy)
            ):
                moves.append(ChessMove(0, 4, 0, 2, castle="q"))

    return moves


def transition_position(
    board: list[list[str]],
    castling: dict[str, bool],
    en_passant: str | None,
    move: ChessMove,
    side: str,
) -> tuple[list[list[str]], dict[str, bool], str | None, bool, bool]:
    new_board = [row[:] for row in board]
    rights = dict(castling)
    piece = new_board[move.sr][move.sc]
    target = new_board[move.tr][move.tc]
    capture = target != "."
    pawn_move = piece.lower() == "p"

    new_board[move.sr][move.sc] = "."

    if move.en_passant:
        captured_row = move.tr + (1 if side == WHITE else -1)
        if 0 <= captured_row < 8:
            capture = new_board[captured_row][move.tc] != "."
            new_board[captured_row][move.tc] = "."

    placed = piece
    if move.promotion:
        placed = move.promotion.upper() if side == WHITE else move.promotion.lower()
    new_board[move.tr][move.tc] = placed

    if move.castle:
        if move.tc == 6:
            rook_from = 7
            rook_to = 5
        else:
            rook_from = 0
            rook_to = 3
        new_board[move.tr][rook_to] = new_board[move.tr][rook_from]
        new_board[move.tr][rook_from] = "."

    if piece == "K":
        rights["K"] = False
        rights["Q"] = False
    elif piece == "k":
        rights["k"] = False
        rights["q"] = False
    elif piece == "R":
        if (move.sr, move.sc) == (7, 0):
            rights["Q"] = False
        elif (move.sr, move.sc) == (7, 7):
            rights["K"] = False
    elif piece == "r":
        if (move.sr, move.sc) == (0, 0):
            rights["q"] = False
        elif (move.sr, move.sc) == (0, 7):
            rights["k"] = False

    if target == "R":
        if (move.tr, move.tc) == (7, 0):
            rights["Q"] = False
        elif (move.tr, move.tc) == (7, 7):
            rights["K"] = False
    elif target == "r":
        if (move.tr, move.tc) == (0, 0):
            rights["q"] = False
        elif (move.tr, move.tc) == (0, 7):
            rights["k"] = False

    next_ep: str | None = None
    if pawn_move and abs(move.tr - move.sr) == 2:
        middle_row = (move.sr + move.tr) // 2
        next_ep = rc_to_square(middle_row, move.sc)

    return new_board, rights, next_ep, capture, pawn_move


def legal_moves_from_position(
    board: list[list[str]],
    side: str,
    castling: dict[str, bool],
    en_passant: str | None,
) -> list[ChessMove]:
    moves: list[ChessMove] = []
    for row in range(8):
        for col in range(8):
            if piece_side(board[row][col]) != side:
                continue
            for move in pseudo_moves_for_piece(
                board, side, row, col, castling, en_passant
            ):
                next_board, _rights, _ep, _capture, _pawn = transition_position(
                    board, castling, en_passant, move, side
                )
                if not in_check(next_board, side):
                    moves.append(move)
    return moves


def _position_key(
    board: list[list[str]],
    side: str,
    castling: dict[str, bool],
    en_passant: str | None,
) -> str:
    flat = "/".join("".join(row) for row in board)
    rights = "".join(key for key in "KQkq" if castling.get(key)) or "-"
    return f"{flat}|{side}|{rights}|{en_passant or '-'}"


def _insufficient_material(board: list[list[str]]) -> bool:
    pieces: list[tuple[str, int, int]] = []
    for row in range(8):
        for col in range(8):
            piece = board[row][col]
            if piece != "." and piece.lower() != "k":
                pieces.append((piece, row, col))

    if not pieces:
        return True
    if len(pieces) == 1 and pieces[0][0].lower() in ("b", "n"):
        return True
    if all(piece.lower() == "b" for piece, _r, _c in pieces):
        colours = {(row + col) % 2 for _piece, row, col in pieces}
        return len(colours) == 1
    return False


def normalise_move_text(raw: str, side: str) -> tuple[str, str, str | None] | None:
    text = str(raw or "").strip().lower()
    compact = re.sub(r"[\s_:\-x>]+", "", text)
    compact = compact.replace("=", "")

    castle_text = text.replace("0", "o").replace(" ", "").lower()
    if castle_text in ("o-o", "oo"):
        return ("e1", "g1", None) if side == WHITE else ("e8", "g8", None)
    if castle_text in ("o-o-o", "ooo"):
        return ("e1", "c1", None) if side == WHITE else ("e8", "c8", None)

    match = re.fullmatch(r"([a-h][1-8])([a-h][1-8])([qrbn])?", compact)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def find_legal_move(
    raw: str,
    side: str,
    moves: list[ChessMove],
) -> ChessMove | None:
    parsed = normalise_move_text(raw, side)
    if parsed is None:
        return None
    start, end, promotion = parsed
    sr, sc = square_to_rc(start)
    tr, tc = square_to_rc(end)

    matches = [
        move for move in moves
        if (move.sr, move.sc, move.tr, move.tc) == (sr, sc, tr, tc)
    ]
    if not matches:
        return None

    if promotion:
        for move in matches:
            if move.promotion == promotion:
                return move
        return None

    promoted = [move for move in matches if move.promotion]
    if promoted:
        return next((move for move in promoted if move.promotion == "q"), promoted[0])
    return matches[0]


def evaluate_board(board: list[list[str]]) -> int:
    score = 0
    for row in range(8):
        for col in range(8):
            piece = board[row][col]
            if piece == ".":
                continue
            value = PIECE_VALUES[piece.lower()]
            # Tiny centre/advancement bonuses make Computer mode less robotic.
            centre = 4 - (abs(3.5 - row) + abs(3.5 - col))
            bonus = int(centre * 2)
            if piece.lower() == "p":
                bonus += (6 - row) * 2 if piece.isupper() else (row - 1) * 2
            piece_score = value + bonus
            score += piece_score if piece.islower() else -piece_score
    return score


def _search_score(
    board: list[list[str]],
    side: str,
    castling: dict[str, bool],
    en_passant: str | None,
    depth: int,
    alpha: int,
    beta: int,
) -> int:
    moves = legal_moves_from_position(board, side, castling, en_passant)
    if not moves:
        if in_check(board, side):
            return 100000 if side == WHITE else -100000
        return 0
    if depth <= 0:
        return evaluate_board(board)

    if side == BLACK:
        value = -10**9
        for move in moves:
            b2, c2, ep2, _cap, _pawn = transition_position(
                board, castling, en_passant, move, side
            )
            value = max(
                value,
                _search_score(b2, WHITE, c2, ep2, depth - 1, alpha, beta),
            )
            alpha = max(alpha, value)
            if alpha >= beta:
                break
        return value

    value = 10**9
    for move in moves:
        b2, c2, ep2, _cap, _pawn = transition_position(
            board, castling, en_passant, move, side
        )
        value = min(
            value,
            _search_score(b2, BLACK, c2, ep2, depth - 1, alpha, beta),
        )
        beta = min(beta, value)
        if alpha >= beta:
            break
    return value


def choose_computer_move(
    board: list[list[str]],
    castling: dict[str, bool],
    en_passant: str | None,
) -> ChessMove | None:
    moves = legal_moves_from_position(board, BLACK, castling, en_passant)
    if not moves:
        return None

    random.shuffle(moves)
    best_score = -10**9
    best: list[ChessMove] = []
    for move in moves:
        b2, c2, ep2, capture, _pawn = transition_position(
            board, castling, en_passant, move, BLACK
        )
        score = _search_score(b2, WHITE, c2, ep2, 1, -10**9, 10**9)
        if capture:
            score += 3
        if in_check(b2, WHITE):
            score += 4
        if move.promotion:
            score += 20

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
            if piece == ".":
                cells.append("·" if (row + col) % 2 == 0 else "•")
            else:
                cells.append(PIECE_SYMBOLS[piece])
        rank = 8 - row
        lines.append(f"{rank}   " + " ".join(cells) + f"   {rank}")
    lines.append("    a b c d e f g h")
    return "\n".join(lines)


def _player_label(game: dict[str, Any], side: str) -> str:
    player_id = int(game["p1_id"] if side == WHITE else game["p2_id"])
    if bool(game.get("computer")) and side == BLACK:
        return "🤖 Computer"
    return f"<@{player_id}>"


def active_content(game: dict[str, Any]) -> str:
    board = valid_board(game.get("board"))
    side = str(game.get("turn") or WHITE)
    checked = in_check(board, side)
    last = str(game.get("last_move") or "").strip()
    status = f"{_player_label(game, side)} to move"
    if checked:
        status += " — **CHECK!**"
    if bool(game.get("computer")) and side == BLACK:
        status = "🤖 Computer is thinking..."

    last_line = f"\nLast move: `{last}`" if last else ""
    return (
        "♟️ **Chess**\n"
        f"White: {_player_label(game, WHITE)}  •  Black: {_player_label(game, BLACK)}\n"
        f"{status}{last_line}\n"
        f"```\n{render_board(board)}\n```"
        "Press **Move** and enter something like `e2e4`. Castling: `O-O` / `O-O-O`."
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
        "♟️ **Chess — Game Over**\n"
        f"{result}\n"
        f"```\n{render_board(board)}\n```"
    )


class ChessMoveModal(discord.ui.Modal, title="Chess move"):
    move_text = discord.ui.TextInput(
        label="Move",
        placeholder="e2e4   (or O-O to castle)",
        required=True,
        max_length=20,
    )

    def __init__(
        self,
        cog: "ChessCog",
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


class ChessMoveButton(discord.ui.Button):
    def __init__(self, cog: "ChessCog"):
        super().__init__(
            label="Move",
            emoji="♟️",
            style=discord.ButtonStyle.primary,
            custom_id="hotbot:chess:move:v1",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.response.send_message(
                "This Chess game is no longer active.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ChessMoveModal(
                self.cog,
                interaction.guild_id,
                interaction.channel_id,
                interaction.message.id,
            )
        )


class ChessResignButton(discord.ui.Button):
    def __init__(self, cog: "ChessCog"):
        super().__init__(
            label="Resign",
            style=discord.ButtonStyle.danger,
            custom_id="hotbot:chess:resign:v1",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_resign(interaction)


class ChessCancelButton(discord.ui.Button):
    def __init__(self, cog: "ChessCog"):
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="hotbot:chess:cancel:v1",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_cancel(interaction)


class ChessView(discord.ui.View):
    def __init__(self, cog: "ChessCog", *, finished: bool = False):
        super().__init__(timeout=None)
        self.add_item(ChessMoveButton(cog))
        self.add_item(ChessResignButton(cog))
        self.add_item(ChessCancelButton(cog))
        if finished:
            for child in self.children:
                child.disabled = True


class ChessCog(commands.Cog):
    GAME_META = {
        "key": "chess",
        "label": "Chess",
        "kind": "head_to_head",
        "result_word": "win",
        "description": "Full chess with persistent games and Computer mode",
        "emoji": "♟️",
        "requires_opponent": True,
    }

    HELP_META = {
        "title": "Chess",
        "summary": "Persistent Chess for two players or one player vs Computer.",
        "details": (
            "Use /chess and optionally choose an opponent. Leave opponent blank for "
            "Computer mode. Press Move and enter coordinate notation such as e2e4, "
            "e7e8q, O-O or O-O-O. Legal move checking includes check, checkmate, "
            "stalemate, castling, en passant and promotion."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._restored_once = False
        register_game("chess", label="Chess", kind="head_to_head", result_word="win")

    async def cog_load(self) -> None:
        self.bot.add_view(ChessView(self))

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
        game["castling"] = _normalise_castling(game.get("castling"))
        game["en_passant"] = _normalise_en_passant(game.get("en_passant"))
        game["turn"] = WHITE if game.get("turn") != BLACK else BLACK
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
                "chess",
                p1_id,
                p2_id,
                winner_id=winner_id,
                result_id=f"chess:{game_id}",
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
                        await message.edit(content=active_content(game), view=ChessView(self))
                except discord.NotFound:
                    stale.append(channel_key)
                except Exception as exc:
                    warn(f"Chess restore failed for {guild_id}/{channel_id}/{message_id}: {exc!r}")
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
        if not self.settings.is_game_allowed(interaction.guild_id, interaction.channel_id, "chess"):
            await interaction.followup.send("❌ Chess is not enabled in this channel.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            existing = self._get_game(guild_id, channel_id)
            if existing:
                text = "A Chess game is already running here."
                jump = self._jump_link(existing)
                if jump:
                    text += f" [Open it]({jump})"
                await interaction.followup.send(text, ephemeral=True)
                return

            board = initial_board()
            castling = {"K": True, "Q": True, "k": True, "q": True}
            game: dict[str, Any] = {
                "game_id": uuid.uuid4().hex,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "message_id": 0,
                "p1_id": interaction.user.id,
                "p2_id": int(p2_id),
                "computer": bool(computer),
                "turn": WHITE,
                "board": board,
                "castling": castling,
                "en_passant": None,
                "halfmove_clock": 0,
                "position_history": [_position_key(board, WHITE, castling, None)],
                "last_move": "",
                "created_at": _utc_now(),
            }
            message = await interaction.followup.send(
                content=active_content(game),
                view=ChessView(self),
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

    def _state_after_move(self, game: dict[str, Any], move: ChessMove, side: str) -> None:
        board = valid_board(game.get("board"))
        castling = _normalise_castling(game.get("castling"))
        ep = _normalise_en_passant(game.get("en_passant"))
        board2, castling2, ep2, capture, pawn_move = transition_position(
            board, castling, ep, move, side
        )
        next_side = opponent(side)
        game["board"] = board2
        game["castling"] = castling2
        game["en_passant"] = ep2
        game["turn"] = next_side
        game["last_move"] = move.uci()
        game["halfmove_clock"] = 0 if (capture or pawn_move) else int(game.get("halfmove_clock") or 0) + 1
        history = game.get("position_history")
        if not isinstance(history, list):
            history = []
        history.append(_position_key(board2, next_side, castling2, ep2))
        game["position_history"] = history[-300:]

    def _outcome(self, game: dict[str, Any]) -> tuple[str | None, str | None]:
        board = valid_board(game.get("board"))
        side = str(game.get("turn") or WHITE)
        castling = _normalise_castling(game.get("castling"))
        ep = _normalise_en_passant(game.get("en_passant"))
        moves = legal_moves_from_position(board, side, castling, ep)
        if not moves:
            if in_check(board, side):
                return opponent(side), None
            return None, "stalemate"
        if _insufficient_material(board):
            return None, "insufficient material"
        if int(game.get("halfmove_clock") or 0) >= 100:
            return None, "50-move rule"
        history = game.get("position_history")
        if isinstance(history, list) and history:
            current = history[-1]
            if sum(1 for key in history if key == current) >= 3:
                return None, "threefold repetition"
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
        if winner_side == WHITE:
            winner_id = int(game["p1_id"])
        elif winner_side == BLACK:
            winner_id = int(game["p2_id"])
        self._record_result(guild_id, game, winner_id)
        self._remove_game(guild_id, channel_id)
        await message.edit(
            content=final_content(game, winner_side=winner_side, draw_reason=draw_reason),
            view=ChessView(self, finished=True),
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
                await interaction.followup.send("This Chess game is no longer active.", ephemeral=True)
                return

            turn = str(game.get("turn") or WHITE)
            p1_id = int(game["p1_id"])
            p2_id = int(game["p2_id"])
            computer = bool(game.get("computer"))
            allowed = (p1_id,) if computer else (p1_id, p2_id)
            expected_id = p1_id if turn == WHITE else p2_id
            if interaction.user.id not in allowed:
                await interaction.followup.send("❌ You aren’t playing this game.", ephemeral=True)
                return
            if interaction.user.id != expected_id:
                await interaction.followup.send("⏳ Not your turn.", ephemeral=True)
                return

            board = valid_board(game.get("board"))
            castling = _normalise_castling(game.get("castling"))
            ep = _normalise_en_passant(game.get("en_passant"))
            moves = legal_moves_from_position(board, turn, castling, ep)
            move = find_legal_move(raw_move, turn, moves)
            if move is None:
                await interaction.followup.send(
                    "❌ Illegal or unrecognised move. Use coordinate notation like `e2e4`, "
                    "`e7e8q`, `O-O` or `O-O-O`.",
                    ephemeral=True,
                )
                return

            self._state_after_move(game, move, turn)
            message = await self._fetch_message(channel_id, message_id)
            if message is None:
                self._remove_game(guild_id, channel_id)
                await interaction.followup.send("The Chess message could not be found, so the saved game was cleared.", ephemeral=True)
                return

            winner_side, draw_reason = self._outcome(game)
            if winner_side or draw_reason:
                await self._finish(message, game, winner_side=winner_side or None, draw_reason=draw_reason)
                return

            if computer and game.get("turn") == BLACK:
                board = valid_board(game.get("board"))
                castling = _normalise_castling(game.get("castling"))
                ep = _normalise_en_passant(game.get("en_passant"))
                ai_move = choose_computer_move(board, castling, ep)
                if ai_move is None:
                    winner_side, draw_reason = self._outcome(game)
                    await self._finish(message, game, winner_side=winner_side or None, draw_reason=draw_reason)
                    return
                self._state_after_move(game, ai_move, BLACK)
                winner_side, draw_reason = self._outcome(game)
                if winner_side or draw_reason:
                    await self._finish(message, game, winner_side=winner_side or None, draw_reason=draw_reason)
                    return

            self._set_game(guild_id, channel_id, game)
            await message.edit(content=active_content(game), view=ChessView(self))

    async def handle_resign(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.followup.send("This Chess game is no longer active.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != interaction.message.id:
                await interaction.followup.send("This Chess game is no longer active.", ephemeral=True)
                return
            p1_id = int(game["p1_id"])
            p2_id = int(game["p2_id"])
            computer = bool(game.get("computer"))
            allowed = (p1_id,) if computer else (p1_id, p2_id)
            if interaction.user.id not in allowed:
                await interaction.followup.send("❌ You aren’t playing this game.", ephemeral=True)
                return
            winner_side = BLACK if interaction.user.id == p1_id else WHITE
            await self._finish(interaction.message, game, winner_side=winner_side)

    async def handle_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.followup.send("This Chess game is no longer active.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != interaction.message.id:
                await interaction.followup.send("This Chess game is no longer active.", ephemeral=True)
                return
            if interaction.user.id != int(game["p1_id"]):
                await interaction.followup.send("❌ Only the game starter can cancel.", ephemeral=True)
                return
            self._remove_game(guild_id, channel_id)
            await interaction.message.edit(
                content=final_content(game, cancelled=True),
                view=ChessView(self, finished=True),
            )

    @app_commands.command(name="chess", description="Play Chess")
    @app_commands.describe(opponent="Who to play against — leave blank to play the Computer")
    async def chess(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None = None,
    ) -> None:
        log_cmd("chess", interaction)
        if not self.settings.is_game_allowed(interaction.guild_id, interaction.channel_id, "chess"):
            await interaction.response.send_message(
                "❌ `/chess` can only be used in the configured game channel(s).",
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
    cog = ChessCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
