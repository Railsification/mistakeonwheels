# core/game_stats.py
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from core.storage import load_guild_json, save_guild_json

STATS_FILENAME = "game_stats.json"
MIN_GAMES_FOR_WIN_RATE = 3
MAX_PROCESSED_RESULTS = 5000

GAME_ALIASES = {
    "overall": "overall",
    "tictactoe": "tictactoe",
    "tic_tac_toe": "tictactoe",
    "tic tac toe": "tictactoe",
    "connect4": "connect4",
    "connect_4": "connect4",
    "connectfour": "connect4",
    "connect_four": "connect4",
    "connect four": "connect4",
    "hangman": "hangman",
}
GAME_KEYS = ("overall", "tictactoe", "connect4", "hangman")


def _empty_record() -> dict[str, int]:
    return {
        "played": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "current_streak": 0,
        "best_streak": 0,
    }


def _empty_player() -> dict[str, dict[str, int]]:
    return {key: _empty_record() for key in GAME_KEYS}


def _normalise_game(game: str) -> str:
    key = str(game or "").strip().lower()
    normalised = GAME_ALIASES.get(key)
    if normalised not in ("tictactoe", "connect4", "hangman"):
        raise ValueError(f"Unsupported game: {game!r}")
    return normalised


def _normalise_record(raw: Any) -> dict[str, int]:
    record = _empty_record()
    if not isinstance(raw, dict):
        return record

    for key in record:
        try:
            record[key] = max(0, int(raw.get(key, 0) or 0))
        except (TypeError, ValueError):
            record[key] = 0

    record["played"] = max(
        record["played"],
        record["wins"] + record["losses"] + record["draws"],
    )
    record["best_streak"] = max(
        record["best_streak"],
        record["current_streak"],
    )
    return record


def _normalise_player(raw: Any) -> dict[str, dict[str, int]]:
    player = _empty_player()
    if not isinstance(raw, dict):
        return player

    for key in GAME_KEYS:
        player[key] = _normalise_record(raw.get(key))
    return player


def _load_blob(guild_id: int) -> dict[str, Any]:
    raw = load_guild_json(
        int(guild_id),
        STATS_FILENAME,
        {"version": 1, "players": {}, "processed_results": []},
    )
    if not isinstance(raw, dict):
        raw = {}

    raw["version"] = 1

    players = raw.get("players")
    if not isinstance(players, dict):
        players = {}

    cleaned_players: dict[str, dict[str, dict[str, int]]] = {}
    for user_id, player_raw in players.items():
        user_text = str(user_id)
        if user_text.isdigit():
            cleaned_players[user_text] = _normalise_player(player_raw)
    raw["players"] = cleaned_players

    processed = raw.get("processed_results")
    if isinstance(processed, dict):
        processed = list(processed.keys())
    if not isinstance(processed, list):
        processed = []

    cleaned_processed: list[str] = []
    seen: set[str] = set()
    for item in processed:
        text = str(item).strip()
        if text and text not in seen:
            cleaned_processed.append(text)
            seen.add(text)
    raw["processed_results"] = cleaned_processed[-MAX_PROCESSED_RESULTS:]
    return raw


def _save_blob(guild_id: int, blob: dict[str, Any]) -> None:
    save_guild_json(int(guild_id), STATS_FILENAME, blob)


def _player(blob: dict[str, Any], user_id: int) -> dict[str, dict[str, int]]:
    key = str(int(user_id))
    players = blob["players"]
    if key not in players:
        players[key] = _empty_player()
    else:
        players[key] = _normalise_player(players[key])
    return players[key]


def _apply_result(record: dict[str, int], result: str) -> None:
    record["played"] += 1

    if result == "win":
        record["wins"] += 1
        record["current_streak"] += 1
        record["best_streak"] = max(
            record["best_streak"],
            record["current_streak"],
        )
    elif result == "loss":
        record["losses"] += 1
        record["current_streak"] = 0
    elif result == "draw":
        record["draws"] += 1
        record["current_streak"] = 0
    else:
        raise ValueError(f"Unsupported result: {result!r}")


def _unique_ids(values: Iterable[int] | None) -> list[int]:
    output: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            continue
        if user_id > 0 and user_id not in seen:
            output.append(user_id)
            seen.add(user_id)
    return output


def record_game_result(
    guild_id: int,
    game: str,
    *,
    player_ids: Iterable[int] | None = None,
    participants: Iterable[int] | None = None,
    winner_id: int | None = None,
    loser_ids: Iterable[int] | None = None,
    draw: bool = False,
    result_id: str | None = None,
) -> bool:
    """Record one completed game.

    Returns False when result_id has already been processed.
    Cancellations should not call this function.
    """
    game_key = _normalise_game(game)
    participant_ids = _unique_ids(player_ids or participants)
    loser_id_list = _unique_ids(loser_ids)

    clean_winner: int | None
    try:
        clean_winner = int(winner_id) if winner_id else None
    except (TypeError, ValueError):
        clean_winner = None

    if clean_winner and clean_winner not in participant_ids:
        participant_ids.append(clean_winner)

    for loser_id in loser_id_list:
        if loser_id not in participant_ids:
            participant_ids.append(loser_id)

    if not participant_ids:
        return False

    clean_result_id = str(result_id or "").strip()
    blob = _load_blob(guild_id)
    processed: list[str] = blob["processed_results"]

    if clean_result_id and clean_result_id in processed:
        return False

    loser_set = set(loser_id_list)

    for user_id in participant_ids:
        if draw:
            result = "draw"
        elif clean_winner and user_id == clean_winner:
            result = "win"
        elif user_id in loser_set or len(participant_ids) > 1:
            result = "loss"
        else:
            # A one-player result with a winner is a solo win, such as Hangman.
            result = "win" if clean_winner == user_id else "loss"

        player = _player(blob, user_id)
        _apply_result(player[game_key], result)
        _apply_result(player["overall"], result)

    if clean_result_id:
        processed.append(clean_result_id)
        blob["processed_results"] = processed[-MAX_PROCESSED_RESULTS:]

    _save_blob(guild_id, blob)
    return True


def record_head_to_head_result(
    guild_id: int,
    game: str,
    player1_id: int,
    player2_id: int,
    *,
    winner_id: int | None,
    result_id: str,
) -> bool:
    return record_game_result(
        guild_id,
        game,
        player_ids=[player1_id, player2_id],
        winner_id=winner_id,
        draw=winner_id is None,
        result_id=result_id,
    )


def record_solo_win(
    guild_id: int,
    game: str,
    user_id: int,
    *,
    result_id: str,
) -> bool:
    return record_game_result(
        guild_id,
        game,
        player_ids=[user_id],
        winner_id=user_id,
        result_id=result_id,
    )


def record_hangman_win(
    guild_id: int,
    user_id: int,
    *,
    result_id: str,
) -> bool:
    return record_solo_win(
        guild_id,
        "hangman",
        user_id,
        result_id=result_id,
    )


# Compatibility aliases for earlier leaderboard builds.
record_result = record_game_result
record_multiplayer_result = record_head_to_head_result


def get_player_stats(guild_id: int, user_id: int) -> dict[str, dict[str, int]]:
    blob = _load_blob(guild_id)
    player = blob["players"].get(str(int(user_id)))
    return deepcopy(_normalise_player(player))


def get_all_player_stats(
    guild_id: int,
) -> dict[int, dict[str, dict[str, int]]]:
    blob = _load_blob(guild_id)
    return {
        int(user_id): deepcopy(_normalise_player(player))
        for user_id, player in blob["players"].items()
        if str(user_id).isdigit()
    }


def win_rate(record: dict[str, int]) -> float | None:
    played = int(record.get("played", 0) or 0)
    if played < MIN_GAMES_FOR_WIN_RATE:
        return None
    return (int(record.get("wins", 0) or 0) / played) * 100.0


def leaderboard_entries(
    guild_id: int,
    game: str = "overall",
) -> list[tuple[int, dict[str, int]]]:
    key = GAME_ALIASES.get(str(game or "").strip().lower(), "overall")
    if key not in GAME_KEYS:
        key = "overall"

    entries: list[tuple[int, dict[str, int]]] = []
    for user_id, player in get_all_player_stats(guild_id).items():
        record = player[key]
        if record["played"] > 0:
            entries.append((user_id, record))

    def sort_key(item: tuple[int, dict[str, int]]) -> tuple[float, ...]:
        user_id, record = item
        rate = win_rate(record)
        eligible_rate = rate if rate is not None else -1.0
        return (
            float(record["wins"]),
            eligible_rate,
            float(record["best_streak"]),
            float(record["draws"]),
            float(-record["losses"]),
            float(-user_id),
        )

    entries.sort(key=sort_key, reverse=True)
    return entries
