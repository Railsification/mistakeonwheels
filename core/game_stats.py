from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Iterable

from core.storage import load_guild_json, save_guild_json


STATS_FILENAME = "game_stats.json"
SCHEMA_VERSION = 2
MAX_PROCESSED_EVENTS = 5000

# These defaults preserve the current names and behaviour. New games are added
# automatically when a recording function is called with a new game key.
_DEFAULT_GAME_CATALOG: dict[str, dict[str, str]] = {
    "tictactoe": {
        "label": "Tic Tac Toe",
        "kind": "head_to_head",
        "result_word": "win",
    },
    "connect4": {
        "label": "Connect Four",
        "kind": "head_to_head",
        "result_word": "win",
    },
    "hangman": {
        "label": "Hangman",
        "kind": "solo",
        "result_word": "solve",
    },
}

# Kept for compatibility with existing imports. GAME_LABELS is updated whenever
# a new game records a result. GAME_KEYS remains the original stable tuple;
# dynamic callers should use iter_game_keys() or get_game_catalog().
GAME_LABELS: dict[str, str] = {
    key: details["label"]
    for key, details in _DEFAULT_GAME_CATALOG.items()
}
GAME_KEYS: tuple[str, ...] = tuple(GAME_LABELS)

_GAME_REGISTRY: dict[str, dict[str, str]] = deepcopy(_DEFAULT_GAME_CATALOG)
_GAME_REGISTRY_LOCK = Lock()
_LOCKS: dict[int, Lock] = {}
_LOCKS_GUARD = Lock()

_VALID_GAME_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VALID_KINDS = {"head_to_head", "solo"}


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


def _normalise_game_key(game: Any) -> str:
    key = str(game or "").strip().lower()

    if not _VALID_GAME_KEY.fullmatch(key):
        raise ValueError(
            "game must use 1-64 lowercase letters, numbers, underscores, or hyphens"
        )

    return key


def _automatic_label(game: str) -> str:
    key = _normalise_game_key(game)

    known = _DEFAULT_GAME_CATALOG.get(key)
    if known:
        return known["label"]

    text = key.replace("_", " ").replace("-", " ")
    text = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)
    text = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", text)
    return " ".join(part.capitalize() for part in text.split()) or key


def _normalise_catalog_entry(
    game: str,
    raw: Any,
    *,
    fallback_kind: str = "head_to_head",
) -> dict[str, str]:
    key = _normalise_game_key(game)
    default = _DEFAULT_GAME_CATALOG.get(key, {})

    raw_dict = raw if isinstance(raw, dict) else {}

    label = str(
        raw_dict.get("label")
        or default.get("label")
        or _automatic_label(key)
    ).strip()

    kind = str(
        raw_dict.get("kind")
        or default.get("kind")
        or fallback_kind
    ).strip().lower()

    if kind not in _VALID_KINDS:
        kind = fallback_kind if fallback_kind in _VALID_KINDS else "head_to_head"

    result_word = str(
        raw_dict.get("result_word")
        or default.get("result_word")
        or ("win" if kind == "head_to_head" else "result")
    ).strip().lower()

    if not result_word:
        result_word = "win" if kind == "head_to_head" else "result"

    return {
        "label": label[:100],
        "kind": kind,
        "result_word": result_word[:30],
    }


def register_game(
    game: str,
    *,
    label: str | None = None,
    kind: str = "head_to_head",
    result_word: str | None = None,
) -> str:
    """Register or update a game without editing this module.

    Recording functions call this automatically, so a new game only needs to
    use the appropriate recording function in its own cog.
    """

    key = _normalise_game_key(game)
    kind = str(kind or "head_to_head").strip().lower()

    if kind not in _VALID_KINDS:
        raise ValueError(f"Unsupported game kind: {kind}")

    with _GAME_REGISTRY_LOCK:
        existing = _GAME_REGISTRY.get(key, {})

        details = _normalise_catalog_entry(
            key,
            {
                "label": label or existing.get("label"),
                "kind": kind or existing.get("kind"),
                "result_word": result_word or existing.get("result_word"),
            },
            fallback_kind=kind,
        )

        _GAME_REGISTRY[key] = details
        GAME_LABELS[key] = details["label"]

    return key


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


def empty_game_stats() -> dict[str, Any]:
    """Return a fresh empty stats record for display code."""

    return _blank_game_stats()


def _blank_player() -> dict[str, Any]:
    return {"games": {}}


def _blank_blob() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "game_catalog": {},
        "players": {},
        "processed_events": [],
    }


def _safe_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _normalise_game_stats(raw: Any) -> dict[str, Any]:
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
        clean[key] = _safe_non_negative_int(raw.get(key))

    clean["last_played_at"] = str(raw.get("last_played_at") or "")
    clean["best_streak"] = max(clean["best_streak"], clean["current_streak"])
    return clean


def _registry_snapshot() -> dict[str, dict[str, str]]:
    with _GAME_REGISTRY_LOCK:
        return deepcopy(_GAME_REGISTRY)


def _normalise_blob(raw: Any) -> dict[str, Any]:
    blob = _blank_blob()

    if not isinstance(raw, dict):
        raw = {}

    raw_catalog = raw.get("game_catalog")
    if isinstance(raw_catalog, dict):
        for raw_game, raw_details in raw_catalog.items():
            try:
                game = _normalise_game_key(raw_game)
            except ValueError:
                continue

            blob["game_catalog"][game] = _normalise_catalog_entry(
                game,
                raw_details,
            )

    players = raw.get("players")
    if isinstance(players, dict):
        for raw_user_id, raw_player in players.items():
            try:
                user_id = str(int(raw_user_id))
            except (TypeError, ValueError):
                continue

            player = _blank_player()
            raw_games = raw_player.get("games") if isinstance(raw_player, dict) else None

            if isinstance(raw_games, dict):
                for raw_game, raw_stats in raw_games.items():
                    try:
                        game = _normalise_game_key(raw_game)
                    except ValueError:
                        continue

                    player["games"][game] = _normalise_game_stats(raw_stats)
                    blob["game_catalog"].setdefault(
                        game,
                        _normalise_catalog_entry(game, None),
                    )

            blob["players"][user_id] = player

    events = raw.get("processed_events")
    if isinstance(events, list):
        clean_events = [str(event) for event in events if str(event).strip()]
        blob["processed_events"] = clean_events[-MAX_PROCESSED_EVENTS:]

    # Merge the in-memory registry after loading persisted data. Persisted
    # metadata wins, but newly loaded game cogs become available immediately.
    for game, details in _registry_snapshot().items():
        blob["game_catalog"].setdefault(game, deepcopy(details))

    return blob


def _load_blob(guild_id: int) -> dict[str, Any]:
    raw = load_guild_json(int(guild_id), STATS_FILENAME, _blank_blob())
    return _normalise_blob(raw)


def _save_blob(guild_id: int, blob: dict[str, Any]) -> None:
    blob["version"] = SCHEMA_VERSION
    save_guild_json(int(guild_id), STATS_FILENAME, blob)


def _player(blob: dict[str, Any], user_id: int) -> dict[str, Any]:
    key = str(int(user_id))
    player = blob["players"].get(key)

    if not isinstance(player, dict):
        player = _blank_player()
        blob["players"][key] = player

    if not isinstance(player.get("games"), dict):
        player["games"] = {}

    return player


def _game_stats(
    blob: dict[str, Any],
    user_id: int,
    game: str,
) -> dict[str, Any]:
    player = _player(blob, user_id)
    stats = player["games"].get(game)

    if not isinstance(stats, dict):
        stats = _blank_game_stats()
        player["games"][game] = stats

    return stats


def _ensure_catalog_entry(
    blob: dict[str, Any],
    game: str,
    *,
    kind: str,
    label: str | None = None,
    result_word: str | None = None,
) -> None:
    details = _normalise_catalog_entry(
        game,
        {
            "label": label,
            "kind": kind,
            "result_word": result_word,
        },
        fallback_kind=kind,
    )

    current = blob["game_catalog"].get(game)
    if isinstance(current, dict):
        merged = dict(current)
        merged["kind"] = kind

        if label:
            merged["label"] = label
        elif not merged.get("label"):
            merged["label"] = details["label"]

        if result_word:
            merged["result_word"] = result_word
        elif not merged.get("result_word"):
            merged["result_word"] = details["result_word"]

        blob["game_catalog"][game] = _normalise_catalog_entry(
            game,
            merged,
            fallback_kind=kind,
        )
    else:
        blob["game_catalog"][game] = details


def _event_is_new(blob: dict[str, Any], event_id: str) -> bool:
    event_id = str(event_id).strip()

    if not event_id:
        raise ValueError("event_id cannot be empty")

    events: list[str] = blob["processed_events"]

    if event_id in events:
        return False

    events.append(event_id)

    if len(events) > MAX_PROCESSED_EVENTS:
        del events[:-MAX_PROCESSED_EVENTS]

    return True


def _touch(stats: dict[str, Any], now: str) -> None:
    stats["played"] = _safe_non_negative_int(stats.get("played")) + 1
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
    """Record any completed two-player game.

    winner_id=None records a draw. Calling this with a new game key registers
    that game automatically and makes it available to the leaderboard.
    """

    guild_id = int(guild_id)
    game = register_game(game, kind="head_to_head", result_word="win")
    player_one_id = int(player_one_id)
    player_two_id = int(player_two_id)

    if player_one_id == player_two_id:
        raise ValueError("A head-to-head game needs two different players")

    if winner_id is not None and int(winner_id) not in {
        player_one_id,
        player_two_id,
    }:
        raise ValueError("winner_id must be one of the two players")

    with _guild_lock(guild_id):
        blob = _load_blob(guild_id)

        if not _event_is_new(blob, event_id):
            return False

        _ensure_catalog_entry(blob, game, kind="head_to_head", result_word="win")

        now = _utc_now()
        player_one_stats = _game_stats(blob, player_one_id, game)
        player_two_stats = _game_stats(blob, player_two_id, game)

        _touch(player_one_stats, now)
        _touch(player_two_stats, now)

        if winner_id is None:
            player_one_stats["draws"] += 1
            player_two_stats["draws"] += 1
            player_one_stats["current_streak"] = 0
            player_two_stats["current_streak"] = 0
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

        _save_blob(guild_id, blob)
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
    """Original public function name used by existing game cogs."""

    return record_head_to_head(
        guild_id,
        game,
        player_one_id,
        player_two_id,
        winner_id=winner_id,
        event_id=result_id,
    )


def record_solo_result(
    guild_id: int,
    game: str,
    user_id: int,
    *,
    event_id: str,
    label: str | None = None,
    result_word: str = "win",
) -> bool:
    """Record a result for a solo or cooperative game winner/solver."""

    guild_id = int(guild_id)
    user_id = int(user_id)
    game = register_game(
        game,
        label=label,
        kind="solo",
        result_word=result_word,
    )

    with _guild_lock(guild_id):
        blob = _load_blob(guild_id)

        if not _event_is_new(blob, event_id):
            return False

        _ensure_catalog_entry(
            blob,
            game,
            kind="solo",
            label=label,
            result_word=result_word,
        )

        stats = _game_stats(blob, user_id, game)
        now = _utc_now()

        _touch(stats, now)
        stats["wins"] += 1
        stats["current_streak"] += 1
        stats["best_streak"] = max(
            stats["best_streak"],
            stats["current_streak"],
        )

        _save_blob(guild_id, blob)
        return True


def record_hangman_solve(
    guild_id: int,
    user_id: int,
    *,
    event_id: str,
) -> bool:
    """Original Hangman recording function. Kept permanently."""

    return record_solo_result(
        guild_id,
        "hangman",
        user_id,
        event_id=event_id,
        label="Hangman",
        result_word="solve",
    )


def get_game_catalog(guild_id: int | None = None) -> dict[str, dict[str, str]]:
    """Return every known game and its display metadata."""

    catalog = _registry_snapshot()

    if guild_id is not None:
        with _guild_lock(int(guild_id)):
            blob = _load_blob(int(guild_id))

        for game, details in blob["game_catalog"].items():
            catalog[game] = deepcopy(details)

    return dict(sorted(catalog.items(), key=lambda item: item[1]["label"].lower()))


def get_game_label(game: str, guild_id: int | None = None) -> str:
    key = _normalise_game_key(game)
    details = get_game_catalog(guild_id).get(key)
    return details["label"] if details else _automatic_label(key)


def get_game_kind(game: str, guild_id: int | None = None) -> str:
    key = _normalise_game_key(game)
    details = get_game_catalog(guild_id).get(key)
    return details["kind"] if details else "head_to_head"


def get_player_stats(guild_id: int, user_id: int) -> dict[str, Any]:
    with _guild_lock(int(guild_id)):
        blob = _load_blob(int(guild_id))
        player = blob["players"].get(str(int(user_id)))
        return deepcopy(player) if isinstance(player, dict) else _blank_player()


def _overall_from_player(player: dict[str, Any]) -> dict[str, Any]:
    games = player.get("games") if isinstance(player, dict) else {}
    total = _blank_game_stats()
    total["last_played_at"] = ""

    if not isinstance(games, dict):
        return total

    for raw_stats in games.values():
        stats = _normalise_game_stats(raw_stats)

        for key in ("played", "wins", "losses", "draws"):
            total[key] += stats[key]

        if stats["last_played_at"] > total["last_played_at"]:
            total["last_played_at"] = stats["last_played_at"]

    # A combined streak across unrelated games would be misleading.
    total["current_streak"] = 0
    total["best_streak"] = 0
    return total


def get_overall_stats(guild_id: int, user_id: int) -> dict[str, Any]:
    return _overall_from_player(get_player_stats(guild_id, user_id))


def win_rate(stats: dict[str, Any]) -> float:
    played = _safe_non_negative_int(stats.get("played"))
    wins = _safe_non_negative_int(stats.get("wins"))
    return (wins / played) if played else 0.0


def get_leaderboard(
    guild_id: int,
    game: str = "overall",
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    game = str(game or "overall").strip().lower()

    if game != "overall":
        game = _normalise_game_key(game)

    with _guild_lock(int(guild_id)):
        blob = _load_blob(int(guild_id))

    rows: list[dict[str, Any]] = []

    for raw_user_id, player in blob["players"].items():
        user_id = int(raw_user_id)

        if game == "overall":
            stats = _overall_from_player(player)
        else:
            player_games = player.get("games") if isinstance(player, dict) else {}
            stats = _normalise_game_stats(
                player_games.get(game) if isinstance(player_games, dict) else None
            )

        if stats["played"] <= 0 and stats["wins"] <= 0:
            continue

        rows.append({"user_id": user_id, "stats": stats})

    kind = (
        blob["game_catalog"].get(game, {}).get("kind")
        if game != "overall"
        else "head_to_head"
    )

    if kind == "solo":
        rows.sort(
            key=lambda row: (
                -row["stats"]["wins"],
                -row["stats"]["best_streak"],
                -row["stats"]["played"],
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
        return rows[: max(0, int(limit))]

    return rows


def get_rank(guild_id: int, user_id: int, game: str = "overall") -> int | None:
    user_id = int(user_id)

    for index, row in enumerate(get_leaderboard(guild_id, game), start=1):
        if row["user_id"] == user_id:
            return index

    return None


def iter_game_keys(guild_id: int | None = None) -> Iterable[str]:
    return tuple(get_game_catalog(guild_id))
