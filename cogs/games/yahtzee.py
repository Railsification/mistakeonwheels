# cogs/yahtzee.py
from __future__ import annotations

import asyncio
import secrets
import uuid
from collections import Counter
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


__version__ = "1.0.1"

GAME_KEY = "yahtzee"
GAMES_FILENAME = "yahtzee_games.json"
MAX_ROLLS = 3
DICE_COUNT = 5

DICE_FACES = {
    1: "⚀",
    2: "⚁",
    3: "⚂",
    4: "⚃",
    5: "⚄",
    6: "⚅",
}

CATEGORY_ORDER: tuple[str, ...] = (
    "ones",
    "twos",
    "threes",
    "fours",
    "fives",
    "sixes",
    "three_kind",
    "four_kind",
    "full_house",
    "small_straight",
    "large_straight",
    "yahtzee",
    "chance",
)

CATEGORY_LABELS: dict[str, str] = {
    "ones": "Ones",
    "twos": "Twos",
    "threes": "Threes",
    "fours": "Fours",
    "fives": "Fives",
    "sixes": "Sixes",
    "three_kind": "Three of a Kind",
    "four_kind": "Four of a Kind",
    "full_house": "Full House",
    "small_straight": "Small Straight",
    "large_straight": "Large Straight",
    "yahtzee": "Yahtzee",
    "chance": "Chance",
}

UPPER_FACE: dict[str, int] = {
    "ones": 1,
    "twos": 2,
    "threes": 3,
    "fours": 4,
    "fives": 5,
    "sixes": 6,
}

SHORT_LABELS: dict[str, str] = {
    "ones": "1s",
    "twos": "2s",
    "threes": "3s",
    "fours": "4s",
    "fives": "5s",
    "sixes": "6s",
    "three_kind": "3-Kind",
    "four_kind": "4-Kind",
    "full_house": "Full House",
    "small_straight": "Sm Straight",
    "large_straight": "Lg Straight",
    "yahtzee": "Yahtzee",
    "chance": "Chance",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def roll_die() -> int:
    return secrets.randbelow(6) + 1


def _blank_dice() -> list[int]:
    return [0] * DICE_COUNT


def _blank_holds() -> list[bool]:
    return [False] * DICE_COUNT


def _normalise_dice(raw: Any) -> list[int]:
    if not isinstance(raw, list) or len(raw) != DICE_COUNT:
        return _blank_dice()
    out: list[int] = []
    for value in raw:
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            ivalue = 0
        out.append(ivalue if 1 <= ivalue <= 6 else 0)
    return out


def _normalise_holds(raw: Any) -> list[bool]:
    if not isinstance(raw, list) or len(raw) != DICE_COUNT:
        return _blank_holds()
    return [bool(value) for value in raw]


def _normalise_card(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for category in CATEGORY_ORDER:
        if category not in raw:
            continue
        try:
            value = max(0, int(raw[category]))
        except (TypeError, ValueError):
            continue
        out[category] = value
    return out


def score_category(category: str, dice: list[int]) -> int:
    dice = _normalise_dice(dice)
    if 0 in dice:
        return 0

    counts = Counter(dice)
    total = sum(dice)

    if category in UPPER_FACE:
        face = UPPER_FACE[category]
        return face * counts.get(face, 0)

    if category == "three_kind":
        return total if max(counts.values(), default=0) >= 3 else 0

    if category == "four_kind":
        return total if max(counts.values(), default=0) >= 4 else 0

    if category == "full_house":
        return 25 if sorted(counts.values()) == [2, 3] else 0

    unique = set(dice)

    if category == "small_straight":
        straights = (
            {1, 2, 3, 4},
            {2, 3, 4, 5},
            {3, 4, 5, 6},
        )
        return 30 if any(straight.issubset(unique) for straight in straights) else 0

    if category == "large_straight":
        return 40 if unique in ({1, 2, 3, 4, 5}, {2, 3, 4, 5, 6}) else 0

    if category == "yahtzee":
        return 50 if len(counts) == 1 else 0

    if category == "chance":
        return total

    return 0


def upper_subtotal(card: dict[str, int]) -> int:
    return sum(int(card.get(category, 0)) for category in UPPER_FACE)


def upper_bonus(card: dict[str, int]) -> int:
    return 35 if upper_subtotal(card) >= 63 else 0


def total_score(card: dict[str, int]) -> int:
    return sum(int(card.get(category, 0)) for category in CATEGORY_ORDER) + upper_bonus(card)


def remaining_categories(card: dict[str, int]) -> list[str]:
    return [category for category in CATEGORY_ORDER if category not in card]


def _player_key(player_number: int) -> str:
    return "p1" if int(player_number) == 1 else "p2"


def _card_for(game: dict[str, Any], player_number: int) -> dict[str, int]:
    scorecards = game.setdefault("scorecards", {"p1": {}, "p2": {}})
    if not isinstance(scorecards, dict):
        scorecards = {"p1": {}, "p2": {}}
        game["scorecards"] = scorecards

    key = _player_key(player_number)
    card = _normalise_card(scorecards.get(key))
    scorecards[key] = card
    return card


def _player_id(game: dict[str, Any], player_number: int) -> int:
    return int(game.get(f"p{int(player_number)}_id") or 0)


def _player_text(game: dict[str, Any], player_number: int) -> str:
    if int(player_number) == 2 and bool(game.get("computer")):
        return "🤖 Computer"
    player_id = _player_id(game, player_number)
    return f"<@{player_id}>" if player_id else "Unknown player"


def _round_number(game: dict[str, Any]) -> int:
    p1_done = len(_card_for(game, 1))
    p2_done = len(_card_for(game, 2))
    return min(13, min(p1_done, p2_done) + 1)


def _game_complete(game: dict[str, Any]) -> bool:
    return len(_card_for(game, 1)) >= 13 and len(_card_for(game, 2)) >= 13


def _dice_text(dice: list[int]) -> str:
    dice = _normalise_dice(dice)
    if not any(dice):
        return "— — — — —"
    return "  ".join(DICE_FACES.get(value, "—") for value in dice)


def _score_value(card: dict[str, int], category: str) -> str:
    if category not in card:
        return "—"
    return str(card[category])


def _scorecard_text(game: dict[str, Any], player_number: int) -> str:
    card = _card_for(game, player_number)
    upper = " | ".join(
        f"{SHORT_LABELS[category]} {_score_value(card, category)}"
        for category in CATEGORY_ORDER[:6]
    )
    lower = " | ".join(
        f"{SHORT_LABELS[category]} {_score_value(card, category)}"
        for category in CATEGORY_ORDER[6:]
    )
    bonus = upper_bonus(card)
    return (
        f"**{_player_text(game, player_number)} — {total_score(card)} pts**\n"
        f"{upper}\n"
        f"Bonus +{bonus} | {lower}"
    )


def active_content(game: dict[str, Any]) -> str:
    current_player = int(game.get("current_player") or 1)
    rolls_used = max(0, min(MAX_ROLLS, int(game.get("rolls_used") or 0)))
    held = _normalise_holds(game.get("held"))
    dice = _normalise_dice(game.get("dice"))
    selected = str(game.get("selected_category") or "").strip()

    if bool(game.get("computer")) and current_player == 2:
        turn_line = "🤖 **Computer is taking its turn...**"
    else:
        turn_line = f"🎯 {_player_text(game, current_player)}, your turn."

    held_positions = [str(index + 1) for index, value in enumerate(held) if value]
    held_line = ", ".join(held_positions) if held_positions else "none"
    selected_line = CATEGORY_LABELS.get(selected, "none") if selected else "none"

    last_action = str(game.get("last_action") or "").strip()
    last_line = f"\n{last_action}\n" if last_action else "\n"

    return (
        "🎲 **YAHTZEE**\n"
        f"Round **{_round_number(game)}/13** · {turn_line}\n"
        f"Dice: **{_dice_text(dice)}**\n"
        f"Rolls: **{rolls_used}/{MAX_ROLLS}** · Held: **{held_line}** · "
        f"Selected: **{selected_line}**\n"
        f"{last_line}\n"
        f"{_scorecard_text(game, 1)}\n\n"
        f"{_scorecard_text(game, 2)}"
    )


def final_content(
    game: dict[str, Any],
    *,
    winner_number: int | None,
    resigned_by: int | None = None,
) -> str:
    p1_total = total_score(_card_for(game, 1))
    p2_total = total_score(_card_for(game, 2))

    lines = [
        "🎲 **YAHTZEE — GAME OVER**",
        "",
    ]

    if resigned_by is not None:
        lines.append(f"🏳️ <@{int(resigned_by)}> resigned.")
    elif winner_number is None:
        lines.append(f"🤝 **Draw — {p1_total} to {p2_total}.**")
    else:
        lines.append(
            f"🏆 **{_player_text(game, winner_number)} wins — "
            f"{p1_total} to {p2_total}!**"
        )

    lines.extend(
        [
            "",
            _scorecard_text(game, 1),
            "",
            _scorecard_text(game, 2),
        ]
    )

    if bool(game.get("computer")):
        lines.extend(["", "🤖 Computer games are practice and do not affect the leaderboard."])

    return "\n".join(lines)


def _best_straight_holds(dice: list[int]) -> list[bool] | None:
    targets = (
        {1, 2, 3, 4, 5},
        {2, 3, 4, 5, 6},
        {1, 2, 3, 4},
        {2, 3, 4, 5},
        {3, 4, 5, 6},
    )

    best_target: set[int] | None = None
    best_overlap = 0
    unique = set(dice)
    for target in targets:
        overlap = len(unique & target)
        if overlap > best_overlap:
            best_overlap = overlap
            best_target = target

    if best_target is None or best_overlap < 3:
        return None

    held = [False] * DICE_COUNT
    already_held: set[int] = set()
    for index, value in enumerate(dice):
        if value in best_target and value not in already_held:
            held[index] = True
            already_held.add(value)
    return held


def choose_ai_holds(dice: list[int], card: dict[str, int]) -> list[bool]:
    remaining = set(remaining_categories(card))
    counts = Counter(dice)

    if ("large_straight" in remaining or "small_straight" in remaining):
        straight_holds = _best_straight_holds(dice)
        if straight_holds is not None:
            return straight_holds

    most_common_value, most_common_count = counts.most_common(1)[0]

    if (
        "yahtzee" in remaining
        or "four_kind" in remaining
        or "three_kind" in remaining
        or most_common_count >= 2
    ):
        return [value == most_common_value for value in dice]

    available_upper = [
        category
        for category in UPPER_FACE
        if category in remaining
    ]
    if available_upper:
        best_face = max(
            (UPPER_FACE[category] for category in available_upper),
            key=lambda face: (counts.get(face, 0), face),
        )
        if counts.get(best_face, 0):
            return [value == best_face for value in dice]

    return [value == most_common_value for value in dice]


def _ai_category_utility(category: str, score: int) -> float:
    if category in UPPER_FACE:
        face = UPPER_FACE[category]
        if score == 0:
            return -0.75 * face
        return score + (0.75 * face)

    if category == "three_kind":
        return float(score if score else -6)
    if category == "four_kind":
        return float(score + 2 if score else -9)
    if category == "full_house":
        return float(score if score else -10)
    if category == "small_straight":
        return float(score if score else -9)
    if category == "large_straight":
        return float(score if score else -12)
    if category == "yahtzee":
        return float(score if score else -15)
    if category == "chance":
        return float(score - 10)
    return float(score)


def choose_ai_category(dice: list[int], card: dict[str, int]) -> str:
    choices = remaining_categories(card)
    if not choices:
        return "chance"

    ranked = []
    for category in choices:
        score = score_category(category, dice)
        ranked.append((_ai_category_utility(category, score), score, category))

    ranked.sort(reverse=True)
    return ranked[0][2]


class YahtzeeDieButton(discord.ui.Button):
    def __init__(self, cog: "YahtzeeCog", index: int):
        super().__init__(
            label=f"{index + 1}:—",
            style=discord.ButtonStyle.secondary,
            custom_id=f"hotbot:yahtzee:hold:{index}:v1",
            row=0,
        )
        self.cog = cog
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_hold(interaction, self.index)


class YahtzeeRollButton(discord.ui.Button):
    def __init__(self, cog: "YahtzeeCog"):
        super().__init__(
            label="Roll",
            emoji="🎲",
            style=discord.ButtonStyle.primary,
            custom_id="hotbot:yahtzee:roll:v1",
            row=1,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_roll(interaction)


class YahtzeeScoreButton(discord.ui.Button):
    def __init__(self, cog: "YahtzeeCog"):
        super().__init__(
            label="Score Selected",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id="hotbot:yahtzee:score:v1",
            row=1,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_score(interaction)


class YahtzeeResignButton(discord.ui.Button):
    def __init__(self, cog: "YahtzeeCog"):
        super().__init__(
            label="Resign",
            emoji="🏳️",
            style=discord.ButtonStyle.danger,
            custom_id="hotbot:yahtzee:resign:v1",
            row=1,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_resign_request(interaction)


class YahtzeeCategorySelect(discord.ui.Select):
    def __init__(self, cog: "YahtzeeCog", game: dict[str, Any] | None = None):
        self.cog = cog
        options = self._options_for(game)
        super().__init__(
            placeholder="Choose where to score this roll...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="hotbot:yahtzee:category:v1",
            row=2,
        )

    @staticmethod
    def _options_for(game: dict[str, Any] | None) -> list[discord.SelectOption]:
        if not isinstance(game, dict):
            return [
                discord.SelectOption(
                    label="Choose a score category",
                    value="__none__",
                )
            ]

        player_number = int(game.get("current_player") or 1)
        card = _card_for(game, player_number)
        dice = _normalise_dice(game.get("dice"))
        rolls_used = int(game.get("rolls_used") or 0)
        selected = str(game.get("selected_category") or "")
        options: list[discord.SelectOption] = []

        for category in remaining_categories(card):
            score = score_category(category, dice) if rolls_used > 0 else 0
            label = CATEGORY_LABELS[category]
            if rolls_used > 0:
                label = f"{label} — {score} pts"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=category,
                    default=category == selected,
                )
            )

        if not options:
            options.append(
                discord.SelectOption(
                    label="Scorecard complete",
                    value="__none__",
                )
            )

        return options[:25]

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "__none__":
            await interaction.response.send_message(
                "This scorecard is already complete.",
                ephemeral=True,
            )
            return
        await self.cog.handle_category(interaction, value)


class YahtzeeView(discord.ui.View):
    def __init__(
        self,
        cog: "YahtzeeCog",
        game: dict[str, Any] | None = None,
        *,
        finished: bool = False,
    ):
        super().__init__(timeout=None)
        self.cog = cog

        for index in range(DICE_COUNT):
            self.add_item(YahtzeeDieButton(cog, index))

        self.add_item(YahtzeeRollButton(cog))
        self.add_item(YahtzeeScoreButton(cog))
        self.add_item(YahtzeeResignButton(cog))
        self.add_item(YahtzeeCategorySelect(cog, game))

        if game is not None:
            self.apply_game(game, finished=finished)

    def apply_game(self, game: dict[str, Any], *, finished: bool) -> None:
        dice = _normalise_dice(game.get("dice"))
        held = _normalise_holds(game.get("held"))
        rolls_used = max(0, min(MAX_ROLLS, int(game.get("rolls_used") or 0)))
        current_player = int(game.get("current_player") or 1)
        computer_turn = bool(game.get("computer")) and current_player == 2
        selected = str(game.get("selected_category") or "")

        for item in self.children:
            if isinstance(item, YahtzeeDieButton):
                value = dice[item.index]
                item.label = f"{item.index + 1}:{DICE_FACES.get(value, '—')}"
                item.style = (
                    discord.ButtonStyle.success
                    if held[item.index]
                    else discord.ButtonStyle.secondary
                )
                item.disabled = (
                    finished
                    or computer_turn
                    or rolls_used == 0
                    or rolls_used >= MAX_ROLLS
                )
            elif isinstance(item, YahtzeeRollButton):
                item.disabled = finished or computer_turn or rolls_used >= MAX_ROLLS
            elif isinstance(item, YahtzeeScoreButton):
                item.disabled = (
                    finished
                    or computer_turn
                    or rolls_used == 0
                    or not selected
                )
            elif isinstance(item, YahtzeeCategorySelect):
                item.disabled = finished or computer_turn or rolls_used == 0
            elif isinstance(item, YahtzeeResignButton):
                item.disabled = finished

        if finished:
            for item in self.children:
                item.disabled = True


class ConfirmResignView(discord.ui.View):
    def __init__(
        self,
        cog: "YahtzeeCog",
        *,
        user_id: int,
        guild_id: int,
        channel_id: int,
        public_message_id: int,
        game_id: str,
    ):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.public_message_id = int(public_message_id)
        self.game_id = str(game_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "This confirmation isn’t yours.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Confirm Resign", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(content="🏳️ Resigning...", view=None)
        await self.cog.confirm_resign(
            interaction,
            user_id=self.user_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            public_message_id=self.public_message_id,
            game_id=self.game_id,
        )

    @discord.ui.button(label="Keep Playing", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="✅ Resign cancelled.",
            view=None,
        )


class YahtzeeCog(commands.Cog):
    GAME_META = {
        "key": GAME_KEY,
        "label": "Yahtzee",
        "kind": "head_to_head",
        "result_word": "win",
        "description": "13-round Yahtzee against a player or the Computer",
        "emoji": "🎲",
        "requires_opponent": True,
    }

    HELP_META = {
        "title": "Yahtzee",
        "summary": "Full 13-category Yahtzee for two players or vs Computer.",
        "details": (
            "Use /yahtzee and optionally choose an opponent. Leave the opponent blank "
            "for Computer mode, or choose Yahtzee from /games. Roll up to three times, "
            "tap dice to hold them, choose a score category, then press Score Selected. "
            "The standard 35-point upper-section bonus is included. PvP games count "
            "toward the leaderboard; Computer games are practice. Active games survive "
            "normal Railway restarts."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        # Prevent repeated taps on actions such as Roll/Score from being queued
        # while Discord is still updating the game message.
        self._pending_actions: set[tuple[int, int, str]] = set()
        self._restored_once = False

        register_game(
            GAME_KEY,
            label="Yahtzee",
            kind="head_to_head",
            result_word="win",
        )

    async def cog_load(self) -> None:
        self.bot.add_view(YahtzeeView(self))

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

    def _claim_action(
        self,
        interaction: discord.Interaction,
        action: str,
    ) -> tuple[int, int, str] | None:
        if interaction.message is None:
            return None
        key = (int(interaction.message.id), int(interaction.user.id), str(action))
        if key in self._pending_actions:
            return None
        self._pending_actions.add(key)
        return key

    def _release_action(self, key: tuple[int, int, str] | None) -> None:
        if key is not None:
            self._pending_actions.discard(key)

    @staticmethod
    def _requested_hold_state(
        interaction: discord.Interaction,
        index: int,
        current_state: bool,
    ) -> bool:
        """Return what the button the user actually tapped was asking for.

        Discord includes the source message/components in the interaction. If a
        user taps the same grey die twice before the first edit becomes visible,
        both interactions therefore mean "hold this die" instead of the second
        queued interaction accidentally toggling it back off.
        """
        message = interaction.message
        target_custom_id = f"hotbot:yahtzee:hold:{int(index)}:v1"
        if message is not None:
            for row in getattr(message, "components", []) or []:
                for component in getattr(row, "children", []) or []:
                    if getattr(component, "custom_id", None) != target_custom_id:
                        continue
                    style = getattr(component, "style", None)
                    return style != discord.ButtonStyle.success

        # Safe fallback for unusual/older interaction payloads.
        return not bool(current_state)

    def _load_blob(self, guild_id: int) -> dict[str, Any]:
        raw = load_guild_json(guild_id, GAMES_FILENAME, {"games": {}})
        if not isinstance(raw, dict):
            raw = {"games": {}}
        if not isinstance(raw.get("games"), dict):
            raw["games"] = {}
        return raw

    def _save_blob(self, guild_id: int, blob: dict[str, Any]) -> None:
        save_guild_json(guild_id, GAMES_FILENAME, blob)

    def _normalise_game(self, game: dict[str, Any]) -> dict[str, Any]:
        game["computer"] = bool(game.get("computer"))
        game["current_player"] = 2 if int(game.get("current_player") or 1) == 2 else 1
        game["dice"] = _normalise_dice(game.get("dice"))
        game["held"] = _normalise_holds(game.get("held"))
        game["rolls_used"] = max(0, min(MAX_ROLLS, int(game.get("rolls_used") or 0)))
        selected = str(game.get("selected_category") or "").strip()
        game["selected_category"] = selected if selected in CATEGORY_ORDER else ""

        scorecards = game.get("scorecards")
        if not isinstance(scorecards, dict):
            scorecards = {}
        game["scorecards"] = {
            "p1": _normalise_card(scorecards.get("p1")),
            "p2": _normalise_card(scorecards.get("p2")),
        }
        return game

    def _get_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        raw = self._load_blob(guild_id)["games"].get(str(channel_id))
        if not isinstance(raw, dict):
            return None
        return self._normalise_game(raw)

    def _set_game(self, guild_id: int, channel_id: int, game: dict[str, Any]) -> None:
        blob = self._load_blob(guild_id)
        blob["games"][str(channel_id)] = self._normalise_game(game)
        self._save_blob(guild_id, blob)

    def _remove_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
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

    async def _send_error(self, interaction: discord.Interaction, text: str) -> None:
        try:
            await interaction.followup.send(text, ephemeral=True)
        except Exception:
            pass

    def _human_allowed(self, game: dict[str, Any], user_id: int) -> bool:
        current_player = int(game.get("current_player") or 1)
        if bool(game.get("computer")) and current_player == 2:
            return False
        return int(user_id) == _player_id(game, current_player)

    def _reset_turn(self, game: dict[str, Any]) -> None:
        game["dice"] = _blank_dice()
        game["held"] = _blank_holds()
        game["rolls_used"] = 0
        game["selected_category"] = ""

    def _record_result(
        self,
        guild_id: int,
        game: dict[str, Any],
        winner_number: int | None,
    ) -> None:
        if bool(game.get("computer")):
            return

        p1_id = _player_id(game, 1)
        p2_id = _player_id(game, 2)
        game_id = str(game.get("game_id") or "").strip()
        if not p1_id or not p2_id or not game_id:
            return

        winner_id = _player_id(game, winner_number) if winner_number else None

        try:
            record_head_to_head_result(
                guild_id,
                GAME_KEY,
                p1_id,
                p2_id,
                winner_id=winner_id,
                result_id=f"yahtzee:{game_id}",
            )
        except Exception as exc:
            warn(f"Failed to record Yahtzee result: {exc!r}")

    def _winner_number(self, game: dict[str, Any]) -> int | None:
        p1_total = total_score(_card_for(game, 1))
        p2_total = total_score(_card_for(game, 2))
        if p1_total > p2_total:
            return 1
        if p2_total > p1_total:
            return 2
        return None

    def _computer_turn(self, game: dict[str, Any]) -> None:
        card = _card_for(game, 2)
        if not remaining_categories(card):
            game["current_player"] = 1
            self._reset_turn(game)
            return

        dice = _blank_dice()
        held = _blank_holds()

        for roll_index in range(MAX_ROLLS):
            for index in range(DICE_COUNT):
                if roll_index == 0 or not held[index]:
                    dice[index] = roll_die()

            if roll_index < MAX_ROLLS - 1:
                held = choose_ai_holds(dice, card)

        category = choose_ai_category(dice, card)
        score = score_category(category, dice)
        card[category] = score
        game["scorecards"]["p2"] = card
        game["last_action"] = (
            f"🤖 Computer scored **{score}** in **{CATEGORY_LABELS[category]}**."
        )
        game["current_player"] = 1
        self._reset_turn(game)

    async def _message_for_game(self, game: dict[str, Any]) -> discord.Message | None:
        channel_id = int(game.get("channel_id") or 0)
        message_id = int(game.get("message_id") or 0)
        if not channel_id or not message_id:
            return None

        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            return await channel.fetch_message(message_id)  # type: ignore[attr-defined]
        except Exception:
            return None

    async def _finish_saved_game(
        self,
        guild_id: int,
        channel_id: int,
        game: dict[str, Any],
        *,
        resigned_by: int | None = None,
        forced_winner_number: int | None = None,
    ) -> None:
        winner_number = (
            forced_winner_number
            if resigned_by is not None
            else self._winner_number(game)
        )
        self._record_result(guild_id, game, winner_number)
        self._remove_game(guild_id, channel_id)

        message = await self._message_for_game(game)
        if message is not None:
            try:
                await message.edit(
                    content=final_content(
                        game,
                        winner_number=winner_number,
                        resigned_by=resigned_by,
                    ),
                    view=YahtzeeView(self, game, finished=True),
                )
            except Exception as exc:
                warn(f"Failed to edit finished Yahtzee message: {exc!r}")

    async def restore_saved_games(self) -> None:
        for guild_id in known_guild_dirs():
            blob = self._load_blob(guild_id)
            stale_channels: list[str] = []

            for channel_key, raw_game in list(blob["games"].items()):
                if not isinstance(raw_game, dict):
                    stale_channels.append(channel_key)
                    continue

                game = self._normalise_game(raw_game)
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
                except discord.NotFound:
                    stale_channels.append(channel_key)
                    continue
                except Exception as exc:
                    warn(
                        f"Yahtzee restore fetch failed for "
                        f"{guild_id}/{channel_id}/{message_id}: {exc!r}"
                    )
                    continue

                if _game_complete(game):
                    self._record_result(guild_id, game, self._winner_number(game))
                    blob["games"].pop(channel_key, None)
                    self._save_blob(guild_id, blob)
                    try:
                        await message.edit(
                            content=final_content(
                                game,
                                winner_number=self._winner_number(game),
                            ),
                            view=YahtzeeView(self, game, finished=True),
                        )
                    except Exception as exc:
                        warn(f"Yahtzee final restore edit failed: {exc!r}")
                    continue

                if bool(game.get("computer")) and int(game.get("current_player") or 1) == 2:
                    self._computer_turn(game)
                    blob["games"][channel_key] = game
                    self._save_blob(guild_id, blob)

                    if _game_complete(game):
                        blob["games"].pop(channel_key, None)
                        self._save_blob(guild_id, blob)
                        try:
                            await message.edit(
                                content=final_content(
                                    game,
                                    winner_number=self._winner_number(game),
                                ),
                                view=YahtzeeView(self, game, finished=True),
                            )
                        except Exception as exc:
                            warn(f"Yahtzee computer restore finish failed: {exc!r}")
                        continue

                try:
                    await message.edit(
                        content=active_content(game),
                        view=YahtzeeView(self, game),
                    )
                except Exception as exc:
                    warn(
                        f"Yahtzee restore edit failed for "
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
                "❌ Yahtzee must be started in a server channel.",
                ephemeral=True,
            )
            return

        if not self.settings.is_game_allowed(
            interaction.guild_id,
            interaction.channel_id,
            GAME_KEY,
        ):
            await interaction.followup.send(
                "❌ Yahtzee is not enabled in this game channel.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            existing = self._get_game(guild_id, channel_id)
            if existing:
                text = "A Yahtzee game is already running here."
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
                "p1_id": int(interaction.user.id),
                "p2_id": int(p2_id),
                "computer": bool(computer),
                "current_player": 1,
                "dice": _blank_dice(),
                "held": _blank_holds(),
                "rolls_used": 0,
                "selected_category": "",
                "scorecards": {"p1": {}, "p2": {}},
                "last_action": "",
                "created_at": _utc_now(),
            }

            message = await interaction.followup.send(
                content=active_content(game),
                view=YahtzeeView(self, game),
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
        if opponent.id == interaction.user.id:
            await interaction.followup.send(
                "❌ You can’t play yourself.",
                ephemeral=True,
            )
            return
        if opponent.bot:
            await interaction.followup.send(
                "❌ Can’t use a Discord bot as the opponent. Choose Computer mode instead.",
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

    async def handle_roll(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        action_key = self._claim_action(interaction, "roll")
        if action_key is None:
            return

        try:
            if interaction.guild_id is None or interaction.channel_id is None:
                await self._send_error(interaction, "This Yahtzee game is no longer active.")
                return

            guild_id = interaction.guild_id
            channel_id = interaction.channel_id

            async with self._lock_for(guild_id, channel_id):
                game = self._current_game_for_interaction(interaction)
                if not game:
                    await self._send_error(interaction, "This Yahtzee game is no longer active.")
                    return
                if not self._human_allowed(game, interaction.user.id):
                    await self._send_error(interaction, "⏳ It isn’t your turn.")
                    return

                rolls_used = int(game.get("rolls_used") or 0)
                if rolls_used >= MAX_ROLLS:
                    await self._send_error(interaction, "You’ve already used all three rolls.")
                    return

                dice = _normalise_dice(game.get("dice"))
                held = _normalise_holds(game.get("held"))

                if rolls_used > 0 and all(held):
                    await self._send_error(
                        interaction,
                        "All five dice are held. Unhold one or score this roll.",
                    )
                    return

                for index in range(DICE_COUNT):
                    if rolls_used == 0 or not held[index]:
                        dice[index] = roll_die()

                game["dice"] = dice
                game["rolls_used"] = rolls_used + 1
                game["selected_category"] = ""
                self._set_game(guild_id, channel_id, game)

                await interaction.message.edit(  # type: ignore[union-attr]
                    content=active_content(game),
                    view=YahtzeeView(self, game),
                )
        finally:
            self._release_action(action_key)

    async def handle_hold(self, interaction: discord.Interaction, index: int) -> None:
        await interaction.response.defer()
        if interaction.guild_id is None or interaction.channel_id is None:
            await self._send_error(interaction, "This Yahtzee game is no longer active.")
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._current_game_for_interaction(interaction)
            if not game:
                await self._send_error(interaction, "This Yahtzee game is no longer active.")
                return
            if not self._human_allowed(game, interaction.user.id):
                await self._send_error(interaction, "⏳ It isn’t your turn.")
                return

            rolls_used = int(game.get("rolls_used") or 0)
            if rolls_used <= 0:
                await self._send_error(interaction, "Roll the dice first.")
                return
            if rolls_used >= MAX_ROLLS:
                await self._send_error(interaction, "You’re on your final roll — choose a score.")
                return
            if not 0 <= index < DICE_COUNT:
                return

            held = _normalise_holds(game.get("held"))
            # Do not blindly toggle. If Discord delivers a second tap from the same
            # stale grey button, it still means "hold", so it cannot undo the
            # first tap just before Roll runs.
            held[index] = self._requested_hold_state(
                interaction,
                index,
                held[index],
            )
            game["held"] = held
            self._set_game(guild_id, channel_id, game)

            await interaction.message.edit(  # type: ignore[union-attr]
                content=active_content(game),
                view=YahtzeeView(self, game),
            )

    async def handle_category(
        self,
        interaction: discord.Interaction,
        category: str,
    ) -> None:
        await interaction.response.defer()
        if interaction.guild_id is None or interaction.channel_id is None:
            await self._send_error(interaction, "This Yahtzee game is no longer active.")
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._current_game_for_interaction(interaction)
            if not game:
                await self._send_error(interaction, "This Yahtzee game is no longer active.")
                return
            if not self._human_allowed(game, interaction.user.id):
                await self._send_error(interaction, "⏳ It isn’t your turn.")
                return
            if int(game.get("rolls_used") or 0) <= 0:
                await self._send_error(interaction, "Roll the dice first.")
                return

            current_player = int(game.get("current_player") or 1)
            card = _card_for(game, current_player)
            if category not in CATEGORY_ORDER or category in card:
                await self._send_error(interaction, "That score category isn’t available.")
                return

            game["selected_category"] = category
            self._set_game(guild_id, channel_id, game)

            await interaction.message.edit(  # type: ignore[union-attr]
                content=active_content(game),
                view=YahtzeeView(self, game),
            )

    async def handle_score(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        action_key = self._claim_action(interaction, "score")
        if action_key is None:
            return

        try:
            if interaction.guild_id is None or interaction.channel_id is None:
                await self._send_error(interaction, "This Yahtzee game is no longer active.")
                return

            guild_id = interaction.guild_id
            channel_id = interaction.channel_id

            async with self._lock_for(guild_id, channel_id):
                game = self._current_game_for_interaction(interaction)
                if not game:
                    await self._send_error(interaction, "This Yahtzee game is no longer active.")
                    return
                if not self._human_allowed(game, interaction.user.id):
                    await self._send_error(interaction, "⏳ It isn’t your turn.")
                    return

                rolls_used = int(game.get("rolls_used") or 0)
                category = str(game.get("selected_category") or "")
                if rolls_used <= 0:
                    await self._send_error(interaction, "Roll the dice first.")
                    return
                if category not in CATEGORY_ORDER:
                    await self._send_error(interaction, "Choose a score category first.")
                    return

                current_player = int(game.get("current_player") or 1)
                card = _card_for(game, current_player)
                if category in card:
                    await self._send_error(interaction, "That category has already been scored.")
                    return

                dice = _normalise_dice(game.get("dice"))
                score = score_category(category, dice)
                card[category] = score
                game["scorecards"][_player_key(current_player)] = card
                game["last_action"] = (
                    f"{_player_text(game, current_player)} scored **{score}** in "
                    f"**{CATEGORY_LABELS[category]}**."
                )
                game["current_player"] = 2 if current_player == 1 else 1
                self._reset_turn(game)

                # Save the human turn before running the Computer. If Railway restarts
                # here, on_ready will see that it is the Computer's turn and resume it.
                self._set_game(guild_id, channel_id, game)

                if _game_complete(game):
                    await self._finish_saved_game(guild_id, channel_id, game)
                    return

                if bool(game.get("computer")) and int(game.get("current_player") or 1) == 2:
                    self._computer_turn(game)
                    self._set_game(guild_id, channel_id, game)

                    if _game_complete(game):
                        await self._finish_saved_game(guild_id, channel_id, game)
                        return

                await interaction.message.edit(  # type: ignore[union-attr]
                    content=active_content(game),
                    view=YahtzeeView(self, game),
                )

        finally:
            self._release_action(action_key)

    async def handle_resign_request(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild_id is None
            or interaction.channel_id is None
            or interaction.message is None
        ):
            await interaction.response.send_message(
                "This Yahtzee game is no longer active.",
                ephemeral=True,
            )
            return

        game = self._current_game_for_interaction(interaction)
        if not game:
            await interaction.response.send_message(
                "This Yahtzee game is no longer active.",
                ephemeral=True,
            )
            return

        human_ids = {_player_id(game, 1)}
        if not bool(game.get("computer")):
            human_ids.add(_player_id(game, 2))

        if interaction.user.id not in human_ids:
            await interaction.response.send_message(
                "❌ Only a player can resign this game.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "🏳️ **Resign this Yahtzee game?** This ends the match.",
            view=ConfirmResignView(
                self,
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                public_message_id=interaction.message.id,
                game_id=str(game.get("game_id") or ""),
            ),
            ephemeral=True,
        )

    async def confirm_resign(
        self,
        interaction: discord.Interaction,
        *,
        user_id: int,
        guild_id: int,
        channel_id: int,
        public_message_id: int,
        game_id: str,
    ) -> None:
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game:
                await interaction.followup.send(
                    "That Yahtzee game is already over.",
                    ephemeral=True,
                )
                return
            if str(game.get("game_id") or "") != str(game_id):
                await interaction.followup.send(
                    "That confirmation belongs to an older game.",
                    ephemeral=True,
                )
                return
            if int(game.get("message_id") or 0) != int(public_message_id):
                await interaction.followup.send(
                    "That Yahtzee game is no longer active.",
                    ephemeral=True,
                )
                return

            if user_id == _player_id(game, 1):
                winner_number = 2
            elif not bool(game.get("computer")) and user_id == _player_id(game, 2):
                winner_number = 1
            else:
                await interaction.followup.send(
                    "You aren’t a player in this game.",
                    ephemeral=True,
                )
                return

            await self._finish_saved_game(
                guild_id,
                channel_id,
                game,
                resigned_by=user_id,
                forced_winner_number=winner_number,
            )

    @app_commands.command(
        name="yahtzee",
        description="Play Yahtzee against a player or the Computer",
    )
    @app_commands.describe(
        opponent="Opponent — leave blank to play the Computer"
    )
    async def yahtzee(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None = None,
    ) -> None:
        log_cmd("yahtzee", interaction)
        await ensure_deferred(interaction, ephemeral=False)

        if opponent is None:
            await self.start_computer_game(interaction)
        else:
            await self.start_game(interaction, opponent)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = YahtzeeCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
