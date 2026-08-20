# cogs/dice.py
from __future__ import annotations

import asyncio
import secrets
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


__version__ = "1.1.0"

GAME_KEY = "dice"
GAMES_FILENAME = "dice_games.json"

DICE_FACES = {
    1: "⚀",
    2: "⚁",
    3: "⚂",
    4: "⚃",
    5: "⚄",
    6: "⚅",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def roll_die() -> int:
    return secrets.randbelow(6) + 1


def die_text(value: int) -> str:
    return f"{DICE_FACES[value]} **{value}**"


class DiceRollButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Roll Dice",
            emoji="🎲",
            style=discord.ButtonStyle.primary,
            custom_id="dice:roll",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: DiceDuelView = self.view  # type: ignore[assignment]
        await view.roll(interaction)


class DiceCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Cancel",
            emoji="✖️",
            style=discord.ButtonStyle.danger,
            custom_id="dice:cancel",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: DiceDuelView = self.view  # type: ignore[assignment]
        await view.cancel(interaction)


class DiceDuelView(discord.ui.View):
    def __init__(
        self,
        *,
        player_one_id: int,
        player_two_id: int,
        computer: bool,
        cog: DiceCog | None = None,
        guild_id: int | None = None,
        channel_id: int | None = None,
        message_id: int | None = None,
        rolls: dict[int, int] | None = None,
        finished: bool = False,
        created_at: str | None = None,
    ):
        # Persistent custom IDs plus saved game state keep the duel alive through restarts.
        super().__init__(timeout=None)

        self.cog = cog
        self.guild_id = int(guild_id or 0)
        self.channel_id = int(channel_id or 0)
        self.message_id = int(message_id or 0)
        self.player_one_id = int(player_one_id)
        self.player_two_id = int(player_two_id)
        self.computer = bool(computer)
        self.rolls: dict[int, int] = {
            int(user_id): int(value)
            for user_id, value in (rolls or {}).items()
            if int(value) in DICE_FACES
        }
        self.finished = bool(finished)
        self.created_at = str(created_at or _utc_now())
        self._lock = asyncio.Lock()

        self.add_item(DiceRollButton())
        self.add_item(DiceCancelButton())
        if self.finished:
            self._disable_all()

    def state(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "player_one_id": self.player_one_id,
            "player_two_id": self.player_two_id,
            "computer": self.computer,
            "rolls": {str(user_id): value for user_id, value in self.rolls.items()},
            "finished": self.finished,
            "created_at": self.created_at,
            "updated_at": _utc_now(),
        }

    def _sync_ids_from_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id:
            self.guild_id = int(interaction.guild_id)
        if interaction.channel_id:
            self.channel_id = int(interaction.channel_id)
        if interaction.message is not None:
            self.message_id = int(interaction.message.id)

    def _persist(self) -> None:
        if self.cog is None or not self.guild_id or not self.message_id:
            return
        self.cog._set_game(self.guild_id, self.message_id, self.state())

    def _remove_saved(self) -> None:
        if self.cog is None or not self.guild_id or not self.message_id:
            return
        self.cog._remove_game(self.guild_id, self.message_id)

    def player_two_text(self) -> str:
        if self.computer:
            return "🤖 **Computer**"
        return f"<@{self.player_two_id}>"

    def _is_player(self, user_id: int) -> bool:
        if user_id == self.player_one_id:
            return True
        return not self.computer and user_id == self.player_two_id

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    def status_content(self) -> str:
        p1_state = "✅ Rolled" if self.player_one_id in self.rolls else "⏳ Waiting to roll"
        if self.computer:
            p2_state = "🤖 Rolls when you do"
        else:
            p2_state = "✅ Rolled" if self.player_two_id in self.rolls else "⏳ Waiting to roll"

        return (
            "🎲 **Dice Duel**\n"
            f"<@{self.player_one_id}> **vs** {self.player_two_text()}\n\n"
            f"<@{self.player_one_id}> — {p1_state}\n"
            f"{self.player_two_text()} — {p2_state}\n\n"
            "**Highest roll wins.**\n"
            "If you tie, both dice automatically reroll."
        )

    def _resolve_rolls(self) -> tuple[int, int, list[tuple[int, int]]]:
        p1_roll = self.rolls[self.player_one_id]
        p2_roll = self.rolls[self.player_two_id]
        rerolls: list[tuple[int, int]] = []

        while p1_roll == p2_roll:
            p1_roll = roll_die()
            p2_roll = roll_die()
            rerolls.append((p1_roll, p2_roll))

        return p1_roll, p2_roll, rerolls

    def result_content(
        self,
        *,
        original_p1: int,
        original_p2: int,
        final_p1: int,
        final_p2: int,
        rerolls: list[tuple[int, int]],
        winner_id: int,
    ) -> str:
        lines = [
            "🎲 **Dice Duel**",
            "",
            f"<@{self.player_one_id}> rolled {die_text(original_p1)}",
            f"{self.player_two_text()} rolled {die_text(original_p2)}",
        ]

        if original_p1 == original_p2:
            lines.extend(["", "🤝 **Tie! Automatic reroll...**"])
            for index, (p1_roll, p2_roll) in enumerate(rerolls, start=1):
                lines.append(f"Reroll {index}: {die_text(p1_roll)} **vs** {die_text(p2_roll)}")
                if p1_roll == p2_roll:
                    lines.append("↪️ Tie again — rerolling...")

        lines.append("")
        if self.computer and winner_id == self.player_two_id:
            lines.append("🤖 **Computer wins!**")
        else:
            lines.append(f"🏆 <@{winner_id}> **wins!**")
        return "\n".join(lines)

    async def roll(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)

        if not self._is_player(user_id):
            await interaction.response.send_message("❌ This isn’t your Dice Duel.", ephemeral=True)
            return

        async with self._lock:
            self._sync_ids_from_interaction(interaction)

            if self.finished:
                await interaction.response.send_message("This Dice Duel is already finished.", ephemeral=True)
                return

            if user_id in self.rolls:
                await interaction.response.send_message("🎲 You’ve already rolled.", ephemeral=True)
                return

            self.rolls[user_id] = roll_die()
            if self.computer:
                self.rolls[self.player_two_id] = roll_die()

            complete = self.player_one_id in self.rolls and self.player_two_id in self.rolls

            # Save the hidden roll before acknowledging the interaction. A restart
            # after this point cannot lose a player's already-locked roll.
            self._persist()

            await interaction.response.send_message("🎲 **Roll locked in.**", ephemeral=True)

            if interaction.message is None:
                return

            if not complete:
                await interaction.message.edit(content=self.status_content(), view=self)
                return

            original_p1 = self.rolls[self.player_one_id]
            original_p2 = self.rolls[self.player_two_id]
            final_p1, final_p2, rerolls = self._resolve_rolls()
            winner_id = self.player_one_id if final_p1 > final_p2 else self.player_two_id

            self.finished = True
            self._disable_all()
            self._persist()

            if not self.computer and interaction.guild_id is not None:
                try:
                    record_head_to_head_result(
                        interaction.guild_id,
                        GAME_KEY,
                        self.player_one_id,
                        self.player_two_id,
                        winner_id=winner_id,
                        result_id=f"dice:{interaction.message.id}",
                    )
                except Exception as exc:
                    warn(f"Failed to record Dice Duel result: {exc!r}")

            await interaction.message.edit(
                content=self.result_content(
                    original_p1=original_p1,
                    original_p2=original_p2,
                    final_p1=final_p1,
                    final_p2=final_p2,
                    rerolls=rerolls,
                    winner_id=winner_id,
                ),
                view=self,
            )
            self._remove_saved()

    async def cancel(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)

        if not self._is_player(user_id):
            await interaction.response.send_message(
                "❌ Only the players can cancel this Dice Duel.",
                ephemeral=True,
            )
            return

        async with self._lock:
            self._sync_ids_from_interaction(interaction)

            if self.finished:
                await interaction.response.send_message("This Dice Duel is already finished.", ephemeral=True)
                return

            self.finished = True
            self._disable_all()
            self._persist()
            await interaction.response.defer()

            if interaction.message is not None:
                await interaction.message.edit(
                    content="🎲 **Dice Duel**\n\n" f"❌ Game cancelled by <@{user_id}>.",
                    view=self,
                )
                self._remove_saved()


class DiceCog(commands.Cog):
    GAME_META = {
        "key": GAME_KEY,
        "label": "Dice Duel",
        "kind": "head_to_head",
        "result_word": "win",
        "description": "Roll against a player or the computer — highest number wins",
        "emoji": "🎲",
        "requires_opponent": True,
    }

    HELP_META = {
        "title": "Dice Duel",
        "summary": "Roll a die against another player or the Computer.",
        "details": (
            "Use /dice and optionally choose an opponent. Leave the opponent blank "
            "for Computer mode, or start Dice Duel from /games. Each player rolls "
            "once and the highest number wins. Ties automatically reroll until "
            "there is a winner. Active games and locked rolls survive Railway "
            "restarts. Computer games are practice and do not alter the leaderboard."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._restored_once = False

        register_game(GAME_KEY, label="Dice Duel", kind="head_to_head", result_word="win")

    def _load_blob(self, guild_id: int) -> dict[str, Any]:
        raw = load_guild_json(guild_id, GAMES_FILENAME, {"games": {}})
        if not isinstance(raw, dict):
            raw = {"games": {}}
        if not isinstance(raw.get("games"), dict):
            raw["games"] = {}
        return raw

    def _save_blob(self, guild_id: int, blob: dict[str, Any]) -> None:
        save_guild_json(guild_id, GAMES_FILENAME, blob)

    def _set_game(self, guild_id: int, message_id: int, game: dict[str, Any]) -> None:
        blob = self._load_blob(guild_id)
        blob["games"][str(int(message_id))] = game
        self._save_blob(guild_id, blob)

    def _remove_game(self, guild_id: int, message_id: int) -> None:
        blob = self._load_blob(guild_id)
        blob["games"].pop(str(int(message_id)), None)
        self._save_blob(guild_id, blob)

    def _view_from_game(self, guild_id: int, raw: Any) -> DiceDuelView | None:
        if not isinstance(raw, dict):
            return None
        try:
            message_id = int(raw.get("message_id") or 0)
            channel_id = int(raw.get("channel_id") or 0)
            p1_id = int(raw.get("player_one_id") or 0)
            p2_id = int(raw.get("player_two_id") or 0)
        except (TypeError, ValueError):
            return None
        if not message_id or not channel_id or not p1_id or not p2_id:
            return None

        rolls_raw = raw.get("rolls") if isinstance(raw.get("rolls"), dict) else {}
        rolls: dict[int, int] = {}
        for user_id, value in rolls_raw.items():
            try:
                uid = int(user_id)
                die = int(value)
            except (TypeError, ValueError):
                continue
            if die in DICE_FACES:
                rolls[uid] = die

        return DiceDuelView(
            cog=self,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            player_one_id=p1_id,
            player_two_id=p2_id,
            computer=bool(raw.get("computer")),
            rolls=rolls,
            finished=bool(raw.get("finished")),
            created_at=str(raw.get("created_at") or ""),
        )

    async def cog_load(self) -> None:
        for guild_id in known_guild_dirs():
            blob = self._load_blob(guild_id)
            for raw in blob["games"].values():
                view = self._view_from_game(guild_id, raw)
                if view is None or view.finished:
                    continue
                self.bot.add_view(view, message_id=view.message_id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored_once:
            return
        await self.restore_saved_games()
        self._restored_once = True

    async def restore_saved_games(self) -> None:
        for guild_id in known_guild_dirs():
            blob = self._load_blob(guild_id)
            stale: list[str] = []

            for message_key, raw in list(blob["games"].items()):
                view = self._view_from_game(guild_id, raw)
                if view is None or view.finished:
                    stale.append(message_key)
                    continue

                try:
                    channel = self.bot.get_channel(view.channel_id)
                    if channel is None:
                        channel = await self.bot.fetch_channel(view.channel_id)
                    message = await channel.fetch_message(view.message_id)  # type: ignore[attr-defined]
                    await message.edit(content=view.status_content(), view=view)
                except discord.NotFound:
                    stale.append(message_key)
                except Exception as exc:
                    warn(
                        f"Dice Duel restore failed for "
                        f"{guild_id}/{view.channel_id}/{view.message_id}: {exc!r}"
                    )

            if stale:
                for message_key in stale:
                    blob["games"].pop(message_key, None)
                self._save_blob(guild_id, blob)

    def allowed(self, interaction: discord.Interaction) -> bool:
        return self.settings.is_game_allowed(interaction.guild_id, interaction.channel_id, GAME_KEY)

    async def _start_game(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None,
        *,
        computer: bool,
    ) -> None:
        if interaction.guild is None or interaction.channel_id is None:
            await interaction.followup.send(
                "❌ Dice Duel must be started in a server channel.",
                ephemeral=True,
            )
            return

        if not self.allowed(interaction):
            await interaction.followup.send(
                "❌ `/dice` can only be used in the configured game channel(s).",
                ephemeral=True,
            )
            return

        if computer:
            bot_user = self.bot.user or interaction.client.user
            if bot_user is None:
                await interaction.followup.send(
                    "❌ Computer mode is unavailable until the bot is fully connected.",
                    ephemeral=True,
                )
                return
            player_two_id = int(bot_user.id)
        else:
            if opponent is None:
                await interaction.followup.send(
                    "❌ Choose an opponent or use Computer mode.",
                    ephemeral=True,
                )
                return
            if opponent.id == interaction.user.id:
                await interaction.followup.send("❌ You can’t play yourself.", ephemeral=True)
                return
            if opponent.bot:
                await interaction.followup.send(
                    "❌ Can’t use a Discord bot as the opponent. Choose Computer mode instead.",
                    ephemeral=True,
                )
                return
            player_two_id = int(opponent.id)

        view = DiceDuelView(
            cog=self,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            player_one_id=interaction.user.id,
            player_two_id=player_two_id,
            computer=computer,
        )

        message = await interaction.followup.send(
            content=view.status_content(),
            view=view,
            ephemeral=False,
            wait=True,
        )
        view.message_id = int(message.id)
        view._persist()

    async def start_game(self, interaction: discord.Interaction, opponent: discord.Member) -> None:
        await self._start_game(interaction, opponent, computer=False)

    async def start_computer_game(self, interaction: discord.Interaction) -> None:
        await self._start_game(interaction, None, computer=True)

    @app_commands.command(name="dice", description="Play Dice Duel against a player or the Computer")
    @app_commands.describe(opponent="Opponent — leave blank to play the Computer")
    async def dice(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None = None,
    ) -> None:
        log_cmd("dice", interaction)
        await ensure_deferred(interaction, ephemeral=False)

        if opponent is None:
            await self.start_computer_game(interaction)
        else:
            await self.start_game(interaction, opponent)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = DiceCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
