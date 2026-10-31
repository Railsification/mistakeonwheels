# core/bingo_logic.py
"""Pure WoS Bingo rules shared by cogs/games/bingo.py.

Kept free of discord.py so the rules can be unit-tested on GitHub CI, which
installs no packages. Card layout is a 5x5 grid stored as 25 word indices in
reading order (row by row); the centre square is FREE and stored as -1.
"""
from __future__ import annotations

__version__ = "1.0.0"

import random
from typing import Any, Iterable

GRID = 5
CARD_SQUARES = GRID * GRID
FREE_POS = CARD_SQUARES // 2  # centre square, position 12
FREE_WORD = -1

MODE_LINE = "line"
MODE_FULL = "full"
MODES: dict[str, str] = {
    MODE_LINE: "One line",
    MODE_FULL: "Full card",
}

DEFAULT_INTERVAL = 25
MIN_INTERVAL = 10
MAX_INTERVAL = 600

STATUS_LOBBY = "lobby"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
LIVE_STATUSES = (STATUS_LOBBY, STATUS_RUNNING, STATUS_PAUSED)
# Finished games are kept until their result is recorded and announced, so a
# restart part-way through finishing cannot lose or double-post the result.
STATUS_WON = "won"
STATUS_ENDED = "ended"
STATUS_EXHAUSTED = "exhausted"
FINISHED_STATUSES = (STATUS_WON, STATUS_ENDED, STATUS_EXHAUSTED)

# Taken from HotBot's established WoS Hangman vocabulary, trimmed to names that
# fit on a Discord button. 52 words gives a line game of roughly 15-25 calls.
DEFAULT_WORDS: list[str] = [
    # Heroes
    "Jeronimo", "Natalia", "Molly", "Sergey", "Bahiti", "Gina", "Zinman",
    "Jessie", "Patrick", "Charlie", "Flint", "Philly", "Mia", "Greg",
    "Logan", "Ahmose", "Reina", "Lynn", "Norah", "Wu Ming",
    # Buildings
    "Furnace", "Embassy", "Infirmary", "Command Center", "Storehouse",
    "Barracks", "Chief House", "Lighthouse", "Arena",
    # Events
    "State of Power", "Sunfire Castle", "Foundry Battle", "Canyon Clash",
    "Bear Hunt", "Crazy Joe", "Frostfire Mine", "Hall of Chiefs",
    "Fishing Tournament",
    # General
    "Fire Crystal", "Chief Gear", "Chief Charm", "Hero Gear", "Essence Stone",
    "Mythic Shard", "Rally", "Garrison", "Expedition", "Exploration",
    "Teleport", "Shield", "Troops", "Survivor",
]

MIN_WORDS = CARD_SQUARES - 1  # 24 words fill a card around the FREE square


def clean_words(words: Iterable[Any]) -> list[str]:
    """Trim, de-duplicate (case-insensitively) and length-limit a word list."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in words or []:
        text = " ".join(str(raw or "").split())[:80]
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def build_card(word_count: int, rng: random.Random | None = None) -> list[int]:
    """Return 25 word indices for a new card; the centre is FREE (-1)."""
    if word_count < MIN_WORDS:
        raise ValueError(f"Bingo needs at least {MIN_WORDS} words, got {word_count}")
    rng = rng or random.SystemRandom()
    picks = rng.sample(range(word_count), MIN_WORDS)
    card = list(picks[:FREE_POS]) + [FREE_WORD] + list(picks[FREE_POS:])
    return card


def winning_lines() -> list[tuple[int, ...]]:
    """5 rows, 5 columns and the 2 diagonals, as card positions."""
    lines: list[tuple[int, ...]] = []
    for r in range(GRID):
        lines.append(tuple(r * GRID + c for c in range(GRID)))
    for c in range(GRID):
        lines.append(tuple(r * GRID + c for r in range(GRID)))
    lines.append(tuple(i * GRID + i for i in range(GRID)))
    lines.append(tuple(i * GRID + (GRID - 1 - i) for i in range(GRID)))
    return lines


_LINES = winning_lines()


def effective_marks(marks: Iterable[Any]) -> set[int]:
    """Valid marked positions, always including the FREE centre."""
    out = {FREE_POS}
    for raw in marks or []:
        try:
            pos = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= pos < CARD_SQUARES:
            out.add(pos)
    return out


def valid_marks(card: list[int], marks: Iterable[Any], called: Iterable[Any]) -> set[int]:
    """Marked positions whose word really has been called (plus FREE).

    Marks are already restricted to called words when made, but checking again
    at claim time keeps a claim honest even if stored data was edited.
    """
    called_set = {int(x) for x in called or [] if isinstance(x, int) or str(x).lstrip("-").isdigit()}
    out = {FREE_POS}
    for pos in effective_marks(marks):
        if pos == FREE_POS:
            continue
        if 0 <= pos < len(card) and card[pos] in called_set:
            out.add(pos)
    return out


def completed_line(marked: set[int], mode: str = MODE_LINE) -> tuple[int, ...] | None:
    """Return the winning positions, or None if the card has not won yet."""
    if mode == MODE_FULL:
        full = tuple(range(CARD_SQUARES))
        return full if all(pos in marked for pos in full) else None
    for line in _LINES:
        if all(pos in marked for pos in line):
            return line
    return None


def squares_to_go(marked: set[int], mode: str = MODE_LINE) -> int:
    """How many more marks the closest win needs (0 means it has won)."""
    if mode == MODE_FULL:
        return CARD_SQUARES - len(marked & set(range(CARD_SQUARES)))
    return min(sum(1 for pos in line if pos not in marked) for line in _LINES)


def pick_next_call(word_count: int, called: Iterable[int], rng: random.Random | None = None) -> int | None:
    """Pick a random word index that has not been called yet, or None."""
    done = set(called or [])
    remaining = [i for i in range(word_count) if i not in done]
    if not remaining:
        return None
    rng = rng or random.SystemRandom()
    return rng.choice(remaining)


def clamp_interval(value: Any) -> int:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        seconds = DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, seconds))


def normalise_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("full", "fullcard", "full_card", "full card", "house", "fullhouse", "blackout", "all"):
        return MODE_FULL
    return MODE_LINE


def normalise_player(raw: Any, word_count: int) -> dict[str, Any] | None:
    """Validate one stored player; returns None if the card is unusable."""
    if not isinstance(raw, dict):
        return None
    card = raw.get("card")
    if not isinstance(card, list) or len(card) != CARD_SQUARES:
        return None
    try:
        card = [int(x) for x in card]
    except (TypeError, ValueError):
        return None
    if card[FREE_POS] != FREE_WORD:
        return None
    others = [x for i, x in enumerate(card) if i != FREE_POS]
    if any(x < 0 or x >= word_count for x in others) or len(set(others)) != len(others):
        return None
    marks = sorted(effective_marks(raw.get("marks")) - {FREE_POS})
    return {"card": card, "marks": marks, "joined_at": str(raw.get("joined_at") or "")}


def normalise_game(raw: Any) -> dict[str, Any] | None:
    """Return a cleaned copy of a stored game, or None if it is unusable.

    Unknown keys are preserved so future versions can add fields safely.
    """
    if not isinstance(raw, dict):
        return None
    words = clean_words(raw.get("words") or [])
    if len(words) < MIN_WORDS:
        return None
    game = dict(raw)
    game["words"] = words
    count = len(words)
    called: list[int] = []
    for x in raw.get("called") or []:
        try:
            idx = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < count and idx not in called:
            called.append(idx)
    game["called"] = called
    game["mode"] = normalise_mode(raw.get("mode"))
    game["interval"] = clamp_interval(raw.get("interval", DEFAULT_INTERVAL))
    status = str(raw.get("status") or STATUS_LOBBY)
    if status not in LIVE_STATUSES + FINISHED_STATUSES:
        status = STATUS_LOBBY
    game["status"] = status
    try:
        game["next_call_at"] = float(raw.get("next_call_at") or 0)
    except (TypeError, ValueError):
        game["next_call_at"] = 0.0
    for key in ("guild_id", "channel_id", "message_id", "host_id", "call_message_id", "winner_id"):
        try:
            game[key] = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            game[key] = 0
    players: dict[str, Any] = {}
    raw_players = raw.get("players") if isinstance(raw.get("players"), dict) else {}
    for uid, pdata in raw_players.items():
        if not str(uid).isdigit():
            continue
        player = normalise_player(pdata, count)
        if player is not None:
            players[str(int(uid))] = player
    game["players"] = players
    return game
