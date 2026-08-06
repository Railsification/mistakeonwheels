# core/game_registry.py
from __future__ import annotations

import inspect
from collections import OrderedDict
from typing import Any, Callable

from discord.ext import commands

from core.game_stats import get_game_catalog, register_game


GameEntry = dict[str, Any]


def _clean_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _normalise_meta(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, dict) else {}


def _app_commands_for(cog: commands.Cog) -> list[Any]:
    try:
        return list(cog.get_app_commands())
    except Exception:
        return []


def _command_for_key(cog: commands.Cog, game_key: str) -> Any | None:
    key = str(game_key or "").strip().lower()
    for command in _app_commands_for(cog):
        if str(getattr(command, "name", "")).strip().lower() == key:
            return command
    return None


def _launcher_for(cog: commands.Cog) -> Callable[..., Any] | None:
    launcher = getattr(cog, "start_game", None)
    if callable(launcher):
        return launcher

    service = getattr(cog, "service", None)
    launcher = getattr(service, "start_game", None)
    if callable(launcher):
        return launcher

    return None


def _requires_opponent(
    launcher: Callable[..., Any],
    meta: dict[str, Any],
    kind: str,
) -> bool:
    explicit = meta.get("requires_opponent")
    if isinstance(explicit, bool):
        return explicit

    try:
        required_positional = [
            parameter
            for parameter in inspect.signature(launcher).parameters.values()
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
            and parameter.default is inspect.Parameter.empty
        ]
        # Bound start methods expose interaction, then optionally opponent.
        if len(required_positional) >= 2:
            return True
        if len(required_positional) == 1:
            return False
    except (TypeError, ValueError):
        pass

    return kind == "head_to_head"


def _register_cog_metadata(bot: commands.Bot) -> None:
    """Register every loaded cog that declares the standard GAME_META block."""

    for cog in bot.cogs.values():
        meta = _normalise_meta(getattr(cog, "GAME_META", None))
        key = _clean_text(meta.get("key")).lower()
        if not key:
            continue

        kind = _clean_text(meta.get("kind"), "head_to_head").lower()
        result_word = _clean_text(
            meta.get("result_word"),
            "win" if kind == "head_to_head" else "result",
        ).lower()

        register_game(
            key,
            label=_clean_text(meta.get("label")) or None,
            kind=kind,
            result_word=result_word,
        )


def discover_games(
    bot: commands.Bot,
    guild_id: int | None = None,
) -> "OrderedDict[str, GameEntry]":
    """Return every loaded, playable game through one shared registry.

    New game cogs can self-register without editing central menus by exposing:

        GAME_META = {
            "key": "mygame",
            "label": "My Game",
            "kind": "solo" or "head_to_head",
            "result_word": "solve" or "win",
            "description": "Short menu description",
            "emoji": "🎮",
            "requires_opponent": False or True,
        }

    The cog, or its ``service`` object, must expose ``start_game``.
    Existing games remain compatible because their slash-command name is
    matched to the already registered stats key automatically.
    """

    _register_cog_metadata(bot)
    catalog = get_game_catalog(guild_id)
    discovered: "OrderedDict[str, GameEntry]" = OrderedDict()

    for game_key, stats_meta in catalog.items():
        key = str(game_key).strip().lower()

        for cog in bot.cogs.values():
            cog_meta = _normalise_meta(getattr(cog, "GAME_META", None))
            declared_key = _clean_text(cog_meta.get("key")).lower()
            command = _command_for_key(cog, key)

            if declared_key:
                if declared_key != key:
                    continue
            elif command is None:
                continue

            launcher = _launcher_for(cog)
            if launcher is None:
                continue

            kind = _clean_text(stats_meta.get("kind"), "head_to_head").lower()
            label = _clean_text(
                cog_meta.get("label"),
                _clean_text(stats_meta.get("label"), key),
            )
            description = _clean_text(cog_meta.get("description"))
            if not description and command is not None:
                description = _clean_text(getattr(command, "description", ""))
            if not description:
                description = (
                    "Solo or community game"
                    if kind == "solo"
                    else "Head-to-head game"
                )

            discovered[key] = {
                "key": key,
                "label": label[:100],
                "kind": kind,
                "result_word": _clean_text(
                    stats_meta.get("result_word"),
                    "win" if kind == "head_to_head" else "result",
                )[:30],
                "description": description[:100],
                "emoji": _clean_text(cog_meta.get("emoji"))[:32],
                "requires_opponent": _requires_opponent(
                    launcher,
                    cog_meta,
                    kind,
                ),
                "cog": cog,
                "launcher": launcher,
            }
            break

    return discovered


def get_game_entry(
    bot: commands.Bot,
    game_key: str,
    guild_id: int | None = None,
) -> GameEntry | None:
    return discover_games(bot, guild_id).get(str(game_key or "").strip().lower())
