from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Iterable

from core.storage import load_guild_json, save_guild_json


STATS_FILENAME = "game_stats.json"
SCHEMA_VERSION = 1
MAX_PROCESSED_EVENTS = 5000

GAME_LABELS: dict[str, str] = {
    "tictactoe": "Tic Tac Toe",
    "connect4": "Connect Four",
    "hangman": "Hangman",
}

GAME_KEYS: tuple[str, ...] = tuple(GAME_LABELS)

_LOCKS: dict[int, Lock] = {}
_LOCKS_GUARD = Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guild_lock(guild_id: int) -> Lock:
    guild_id = int(guild_id)

    with _LOCKS_GUARD:
        lock = _LOCKS.get(guild_id)

        if lock is None:
            lock = Lock()
            _LOCKS[guild_id] = lock

        return lock


def _blank_game_stats() -> dict[str, Any]:
    return {
        "played": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "current_streak": 0,
        "best_streak": 0,
        "last_played_at": "",
    }


def _blank_player() -> dict[str, Any]:
    return {
        "games": {
            game: _blank_game_stats()
            for game in GAME_KEYS
        }
    }


def _blank_blob() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "players": {},
        "processed_events": [],
    }


def _safe_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))

    except (TypeError, ValueError):
        return 0


def _normalise_game_stats(
    raw: Any,
) -> dict[str, Any]:
    clean = _blank_game_stats()

    if not isinstance(raw, dict):
        return clean

    for key in (
        "played",
        "wins",
        "losses",
        "draws",
        "current_streak",
        "best_streak",
    ):
        clean[key] = _safe_non_negative_int(
            raw.get(key)
        )

    clean["last_played_at"] = str(
        raw.get("last_played_at") or ""
    )

    clean["best_streak"] = max(
        clean["best_streak"],
        clean["current_streak"],
    )

    return clean


def _normalise_blob(
    raw: Any,
) -> dict[str, Any]:
    blob = _blank_blob()

    if not isinstance(raw, dict):
        return blob

    players = raw.get("players")

    if isinstance(players, dict):
        for raw_user_id, raw_player in players.items():
            try:
                user_id = str(int(raw_user_id))

            except (TypeError, ValueError):
                continue

            player = _blank_player()

            raw_games = (
                raw_player.get("games")
                if isinstance(raw_player, dict)
                else None
            )

            if isinstance(raw_games, dict):
                for game in GAME_KEYS:
                    player["games"][game] = (
                        _normalise_game_stats(
                            raw_games.get(game)
                        )
                    )

            blob["players"][user_id] = player

    events = raw.get("processed_events")

    if isinstance(events, list):
        clean_events = [
            str(event)
            for event in events
            if str(event).strip()
        ]

        blob["processed_events"] = (
            clean_events[-MAX_PROCESSED_EVENTS:]
        )

    return blob


def _load_blob(
    guild_id: int,
) -> dict[str, Any]:
    raw = load_guild_json(
        int(guild_id),
        STATS_FILENAME,
        _blank_blob(),
    )

    return _normalise_blob(raw)


def _save_blob(
    guild_id: int,
    blob: dict[str, Any],
) -> None:
    blob["version"] = SCHEMA_VERSION

    save_guild_json(
        int(guild_id),
        STATS_FILENAME,
        blob,
    )


def _player(
    blob: dict[str, Any],
    user_id: int,
) -> dict[str, Any]:
    key = str(int(user_id))

    player = blob["players"].get(key)

    if not isinstance(player, dict):
        player = _blank_player()
        blob["players"][key] = player

    return player


def _event_is_new(
    blob: dict[str, Any],
    event_id: str,
) -> bool:
    event_id = str(event_id).strip()

    if not event_id:
        raise ValueError(
            "event_id cannot be empty"
        )

    events: list[str] = blob["processed_events"]

    if event_id in events:
        return False

    events.append(event_id)

    if len(events) > MAX_PROCESSED_EVENTS:
        del events[:-MAX_PROCESSED_EVENTS]

    return True


def _touch(
    stats: dict[str, Any],
    now: str,
) -> None:
    stats["played"] = (
        _safe_non_negative_int(
            stats.get("played")
        )
        + 1
    )

    stats["last_played_at"] = now


def record_head_to_head(
    guild_id: int,
    game: str,
    player_one_id: int,
    player_two_id: int,
    *,
    winner_id: int | None,
    event_id: str,
) -> bool:
    """Record a completed head-to-head game."""

    guild_id = int(guild_id)
    game = str(game).strip().lower()

    player_one_id = int(player_one_id)
    player_two_id = int(player_two_id)

    if game not in {
        "tictactoe",
        "connect4",
    }:
        raise ValueError(
            f"Unsupported head-to-head game: {game}"
        )

    if player_one_id == player_two_id:
        raise ValueError(
            "A head-to-head game needs "
            "two different players"
        )

    if (
        winner_id is not None
        and int(winner_id)
        not in {
            player_one_id,
            player_two_id,
        }
    ):
        raise ValueError(
            "winner_id must be one "
            "of the two players"
        )

    with _guild_lock(guild_id):
        blob = _load_blob(guild_id)

        if not _event_is_new(
            blob,
            event_id,
        ):
            return False

        now = _utc_now()

        player_one_stats = (
            _player(
                blob,
                player_one_id,
            )["games"][game]
        )

        player_two_stats = (
            _player(
                blob,
                player_two_id,
            )["games"][game]
        )

        _touch(
            player_one_stats,
            now,
        )

        _touch(
            player_two_stats,
            now,
        )

        if winner_id is None:
            player_one_stats["draws"] += 1
            player_two_stats["draws"] += 1

            player_one_stats[
                "current_streak"
            ] = 0

            player_two_stats[
                "current_streak"
            ] = 0

        else:
            winner_id = int(winner_id)

            if winner_id == player_one_id:
                winner_stats = player_one_stats
                loser_stats = player_two_stats

            else:
                winner_stats = player_two_stats
                loser_stats = player_one_stats

            winner_stats["wins"] += 1
            winner_stats["current_streak"] += 1

            winner_stats["best_streak"] = max(
                winner_stats["best_streak"],
                winner_stats["current_streak"],
            )

            loser_stats["losses"] += 1
            loser_stats["current_streak"] = 0

        _save_blob(
            guild_id,
            blob,
        )

        return True


def record_head_to_head_result(
    guild_id: int,
    game: str,
    player_one_id: int,
    player_two_id: int,
    *,
    winner_id: int | None,
    result_id: str,
) -> bool:
    """Original public function name used by game cogs.

    This function name must remain available permanently.
    """

    return record_head_to_head(
        guild_id,
        game,
        player_one_id,
        player_two_id,
        winner_id=winner_id,
        event_id=result_id,
    )


def record_hangman_solve(
    guild_id: int,
    user_id: int,
    *,
    event_id: str,
) -> bool:
    """Record the player who solved a Hangman word."""

    guild_id = int(guild_id)
    user_id = int(user_id)

    with _guild_lock(guild_id):
        blob = _load_blob(guild_id)

        if not _event_is_new(
            blob,
            event_id,
        ):
            return False

        stats = (
            _player(
                blob,
                user_id,
            )["games"]["hangman"]
        )

        now = _utc_now()

        _touch(
            stats,
            now,
        )

        stats["wins"] += 1
        stats["current_streak"] += 1

        stats["best_streak"] = max(
            stats["best_streak"],
            stats["current_streak"],
        )

        _save_blob(
            guild_id,
            blob,
        )

        return True


def get_player_stats(
    guild_id: int,
    user_id: int,
) -> dict[str, Any]:
    with _guild_lock(int(guild_id)):
        blob = _load_blob(int(guild_id))

        player = blob["players"].get(
            str(int(user_id))
        )

        if isinstance(player, dict):
            return deepcopy(player)

        return _blank_player()


def _overall_from_player(
    player: dict[str, Any],
) -> dict[str, Any]:
    games = (
        player.get("games")
        if isinstance(player, dict)
        else {}
    )

    total = _blank_game_stats()
    total["last_played_at"] = ""

    for game in GAME_KEYS:
        stats = _normalise_game_stats(
            games.get(game)
            if isinstance(games, dict)
            else None
        )

        for key in (
            "played",
            "wins",
            "losses",
            "draws",
        ):
            total[key] += stats[key]

        if (
            stats["last_played_at"]
            > total["last_played_at"]
        ):
            total["last_played_at"] = (
                stats["last_played_at"]
            )

    total["current_streak"] = 0
    total["best_streak"] = 0

    return total


def get_overall_stats(
    guild_id: int,
    user_id: int,
) -> dict[str, Any]:
    return _overall_from_player(
        get_player_stats(
            guild_id,
            user_id,
        )
    )


def win_rate(
    stats: dict[str, Any],
) -> float:
    played = _safe_non_negative_int(
        stats.get("played")
    )

    wins = _safe_non_negative_int(
        stats.get("wins")
    )

    return (
        wins / played
        if played
        else 0.0
    )


def get_leaderboard(
    guild_id: int,
    game: str = "overall",
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    game = str(
        game or "overall"
    ).strip().lower()

    if (
        game != "overall"
        and game not in GAME_KEYS
    ):
        raise ValueError(
            f"Unknown game: {game}"
        )

    with _guild_lock(int(guild_id)):
        blob = _load_blob(int(guild_id))

    rows: list[dict[str, Any]] = []

    for raw_user_id, player in (
        blob["players"].items()
    ):
        user_id = int(raw_user_id)

        if game == "overall":
            stats = _overall_from_player(
                player
            )

        else:
            stats = _normalise_game_stats(
                player["games"].get(game)
            )

        if (
            stats["played"] <= 0
            and stats["wins"] <= 0
        ):
            continue

        rows.append(
            {
                "user_id": user_id,
                "stats": stats,
            }
        )

    if game == "hangman":
        rows.sort(
            key=lambda row: (
                -row["stats"]["wins"],
                -row["stats"]["best_streak"],
                row["user_id"],
            )
        )

    else:
        rows.sort(
            key=lambda row: (
                -row["stats"]["wins"],
                -win_rate(row["stats"]),
                -row["stats"]["played"],
                row["user_id"],
            )
        )

    if limit is not None:
        return rows[
            :max(0, int(limit))
        ]

    return rows


def get_rank(
    guild_id: int,
    user_id: int,
    game: str = "overall",
) -> int | None:
    user_id = int(user_id)

    leaderboard = get_leaderboard(
        guild_id,
        game,
    )

    for index, row in enumerate(
        leaderboard,
        start=1,
    ):
        if row["user_id"] == user_id:
            return index

    return None


def iter_game_keys() -> Iterable[str]:
    return GAME_KEYS