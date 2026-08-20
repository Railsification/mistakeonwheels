# cogs/rock_paper_scissors.py
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

GAME_KEY = "rps"
GAMES_FILENAME = "rps_games.json"

CHOICES: dict[str, tuple[str, str]] = {
    "rock": ("🪨", "Rock"),
    "paper": ("📄", "Paper"),
    "scissors": ("✂️", "Scissors"),
}

BEATS = {
    "rock": "scissors",
    "paper": "rock",
    "scissors": "paper",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def choice_text(choice: str) -> str:
    emoji, label = CHOICES[choice]
    return f"{emoji} **{label}**"


class RPSChoiceButton(discord.ui.Button):
    def __init__(self, choice: str):
        emoji, label = CHOICES[choice]
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.primary,
            custom_id=f"rps:choice:{choice}",
            row=0,
        )
        self.choice = choice

    async def callback(self, interaction: discord.Interaction) -> None:
        view: RockPaperScissorsView = self.view  # type: ignore[assignment]
        await view.choose(interaction, self.choice)


class RPSCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Cancel",
            emoji="✖️",
            style=discord.ButtonStyle.danger,
            custom_id="rps:cancel",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: RockPaperScissorsView = self.view  # type: ignore[assignment]
        await view.cancel(interaction)


class RockPaperScissorsView(discord.ui.View):
    def __init__(
        self,
        *,
        player_one_id: int,
        player_two_id: int,
        computer: bool,
        cog: RockPaperScissorsCog | None = None,
        guild_id: int | None = None,
        channel_id: int | None = None,
        message_id: int | None = None,
        choices: dict[int, str] | None = None,
        finished: bool = False,
        cancelled_by: int | None = None,
        created_at: str | None = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id or 0)
        self.channel_id = int(channel_id or 0)
        self.message_id = int(message_id or 0)
        self.player_one_id = int(player_one_id)
        self.player_two_id = int(player_two_id)
        self.computer = bool(computer)
        self.choices: dict[int, str] = {
            int(user_id): str(choice)
            for user_id, choice in (choices or {}).items()
            if str(choice) in CHOICES
        }
        self.finished = bool(finished)
        self.cancelled_by = int(cancelled_by or 0)
        self.created_at = str(created_at or _utc_now())
        self._lock = asyncio.Lock()

        for choice in ("rock", "paper", "scissors"):
            self.add_item(RPSChoiceButton(choice))
        self.add_item(RPSCancelButton())
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
            "choices": {str(user_id): choice for user_id, choice in self.choices.items()},
            "finished": self.finished,
            "cancelled_by": self.cancelled_by,
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

    def player_two_name(self) -> str:
        return "🤖 **Computer**" if self.computer else f"<@{self.player_two_id}>"

    def status_content(self) -> str:
        p1_state = "✅ Locked in" if self.player_one_id in self.choices else "⏳ Choosing"
        if self.computer:
            p2_state = "🤖 Waiting for your pick"
        else:
            p2_state = "✅ Locked in" if self.player_two_id in self.choices else "⏳ Choosing"

        return (
            "🪨 📄 ✂️ **Rock Paper Scissors**\n"
            f"<@{self.player_one_id}> **vs** {self.player_two_name()}\n\n"
            f"<@{self.player_one_id}> — {p1_state}\n"
            f"{self.player_two_name()} — {p2_state}\n\n"
            "*Choices stay hidden until both players lock in.*"
        )

    def result_content(self, winner_id: int | None) -> str:
        p1_choice = self.choices[self.player_one_id]
        p2_choice = self.choices[self.player_two_id]

        if winner_id is None:
            result = "🤝 **Draw!**"
        elif self.computer and winner_id == self.player_two_id:
            result = "🤖 **Computer wins!**"
        else:
            result = f"🏆 <@{winner_id}> **wins!**"

        return (
            "🪨 📄 ✂️ **Rock Paper Scissors**\n\n"
            f"<@{self.player_one_id}> chose {choice_text(p1_choice)}\n"
            f"{self.player_two_name()} chose {choice_text(p2_choice)}\n\n"
            f"{result}"
        )

    def cancelled_content(self) -> str:
        return (
            "🪨 📄 ✂️ **Rock Paper Scissors**\n\n"
            f"❌ Game cancelled by <@{self.cancelled_by}>."
        )

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    def _winner(self) -> int | None:
        p1_choice = self.choices[self.player_one_id]
        p2_choice = self.choices[self.player_two_id]

        if p1_choice == p2_choice:
            return None
        if BEATS[p1_choice] == p2_choice:
            return self.player_one_id
        return self.player_two_id

    def _is_allowed_player(self, user_id: int) -> bool:
        if user_id == self.player_one_id:
            return True
        return not self.computer and user_id == self.player_two_id

    async def choose(self, interaction: discord.Interaction, choice: str) -> None:
        user_id = int(interaction.user.id)

        if not self._is_allowed_player(user_id):
            await interaction.response.send_message(
                "❌ This isn’t your Rock Paper Scissors game.",
                ephemeral=True,
            )
            return

        async with self._lock:
            self._sync_ids_from_interaction(interaction)

            if self.finished:
                await interaction.response.send_message("This game is already finished.", ephemeral=True)
                return

            if user_id in self.choices:
                await interaction.response.send_message(
                    f"You already locked in {choice_text(self.choices[user_id])}.",
                    ephemeral=True,
                )
                return

            self.choices[user_id] = choice
            if self.computer:
                self.choices[self.player_two_id] = secrets.choice(tuple(CHOICES))

            complete = self.player_one_id in self.choices and self.player_two_id in self.choices

            # Save hidden selections before acknowledging the click so a restart
            # cannot make a player's already-locked choice disappear.
            self._persist()

            await interaction.response.send_message(
                f"✅ Locked in {choice_text(choice)}.",
                ephemeral=True,
            )

            if interaction.message is None:
                return

            if not complete:
                await interaction.message.edit(content=self.status_content(), view=self)
                return

            self.finished = True
            self._disable_all()
            winner_id = self._winner()
            self._persist()

            if not self.computer and interaction.guild_id is not None:
                try:
                    record_head_to_head_result(
                        interaction.guild_id,
                        GAME_KEY,
                        self.player_one_id,
                        self.player_two_id,
                        winner_id=winner_id,
                        result_id=f"rps:{interaction.message.id}",
                    )
                except Exception as exc:
                    warn(f"Failed to record RPS result: {exc!r}")

            await interaction.message.edit(content=self.result_content(winner_id), view=self)
            self._remove_saved()

    async def cancel(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)

        if not self._is_allowed_player(user_id):
            await interaction.response.send_message(
                "❌ Only the players can cancel this game.",
                ephemeral=True,
            )
            return

        async with self._lock:
            self._sync_ids_from_interaction(interaction)

            if self.finished:
                await interaction.response.send_message("This game is already finished.", ephemeral=True)
                return

            self.finished = True
            self.cancelled_by = user_id
            self._disable_all()
            self._persist()
            await interaction.response.defer()

            if interaction.message is not None:
                await interaction.message.edit(content=self.cancelled_content(), view=self)
                self._remove_saved()


class RockPaperScissorsCog(commands.Cog):
    GAME_META = {
        "key": GAME_KEY,
        "label": "Rock Paper Scissors",
        "kind": "head_to_head",
        "result_word": "win",
        "description": "Rock, paper, scissors against a player or the computer",
        "emoji": "✂️",
        "requires_opponent": True,
    }

    HELP_META = {
        "title": "Rock Paper Scissors",
        "summary": "Play Rock Paper Scissors against another player or Computer.",
        "details": (
            "Use /rps and optionally choose an opponent. Leave the opponent blank "
            "for Computer mode, or start Rock Paper Scissors from /games. "
            "Player choices remain hidden until both players have locked in. "
            "Active games and locked choices survive Railway restarts. "
            "Computer games are practice and do not alter the leaderboard."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._restored_once = False

        register_game(GAME_KEY, label="Rock Paper Scissors", kind="head_to_head", result_word="win")

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

    def _view_from_game(self, guild_id: int, raw: Any) -> RockPaperScissorsView | None:
        if not isinstance(raw, dict):
            return None
        try:
            channel_id = int(raw.get("channel_id") or 0)
            message_id = int(raw.get("message_id") or 0)
            p1_id = int(raw.get("player_one_id") or 0)
            p2_id = int(raw.get("player_two_id") or 0)
        except (TypeError, ValueError):
            return None
        if not channel_id or not message_id or not p1_id or not p2_id:
            return None

        choices_raw = raw.get("choices") if isinstance(raw.get("choices"), dict) else {}
        choices: dict[int, str] = {}
        for user_id, choice in choices_raw.items():
            try:
                uid = int(user_id)
            except (TypeError, ValueError):
                continue
            text = str(choice)
            if text in CHOICES:
                choices[uid] = text

        return RockPaperScissorsView(
            cog=self,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            player_one_id=p1_id,
            player_two_id=p2_id,
            computer=bool(raw.get("computer")),
            choices=choices,
            finished=bool(raw.get("finished")),
            cancelled_by=int(raw.get("cancelled_by") or 0),
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
                if view is None:
                    stale.append(message_key)
                    continue

                try:
                    channel = self.bot.get_channel(view.channel_id)
                    if channel is None:
                        channel = await self.bot.fetch_channel(view.channel_id)
                    message = await channel.fetch_message(view.message_id)  # type: ignore[attr-defined]

                    if view.finished:
                        if view.cancelled_by:
                            await message.edit(content=view.cancelled_content(), view=view)
                        elif (
                            view.player_one_id in view.choices
                            and view.player_two_id in view.choices
                        ):
                            await message.edit(content=view.result_content(view._winner()), view=view)
                        stale.append(message_key)
                    else:
                        await message.edit(content=view.status_content(), view=view)
                except discord.NotFound:
                    stale.append(message_key)
                except Exception as exc:
                    warn(
                        f"RPS restore failed for "
                        f"{guild_id}/{view.channel_id}/{view.message_id}: {exc!r}"
                    )

            if stale:
                for message_key in stale:
                    blob["games"].pop(message_key, None)
                self._save_blob(guild_id, blob)

    def allowed(self, interaction: discord.Interaction) -> bool:
        return self.settings.is_game_allowed(interaction.guild_id, interaction.channel_id, GAME_KEY)

    async def _start(
        self,
        interaction: discord.Interaction,
        *,
        player_two_id: int,
        computer: bool,
    ) -> None:
        if interaction.guild is None or interaction.channel_id is None:
            await interaction.followup.send(
                "❌ Rock Paper Scissors must be started in a server channel.",
                ephemeral=True,
            )
            return

        view = RockPaperScissorsView(
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
        if opponent.bot:
            await interaction.followup.send(
                "❌ Can’t use a Discord bot as the opponent. Choose Computer mode instead.",
                ephemeral=True,
            )
            return

        if opponent.id == interaction.user.id:
            await interaction.followup.send("❌ You can’t play yourself.", ephemeral=True)
            return

        await self._start(interaction, player_two_id=opponent.id, computer=False)

    async def start_computer_game(self, interaction: discord.Interaction) -> None:
        bot_user = self.bot.user or interaction.client.user
        if bot_user is None:
            await interaction.followup.send(
                "❌ Computer mode is unavailable until the bot is fully connected.",
                ephemeral=True,
            )
            return

        await self._start(interaction, player_two_id=int(bot_user.id), computer=True)

    @app_commands.command(
        name="rps",
        description="Play Rock Paper Scissors against a player or the Computer",
    )
    @app_commands.describe(opponent="Who to play against — leave blank to play the Computer")
    async def rps(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None = None,
    ) -> None:
        log_cmd("rps", interaction)

        if not self.allowed(interaction):
            await interaction.response.send_message(
                "❌ `/rps` can only be used in the configured game channel(s).",
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

    from core.command_scope import bind_public_cog

    cog = RockPaperScissorsCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
