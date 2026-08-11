# cogs/rock_paper_scissors.py
from __future__ import annotations

import asyncio
import secrets

import discord
from discord import app_commands
from discord.ext import commands

from core.game_stats import record_head_to_head_result, register_game
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.utils import ensure_deferred


GAME_KEY = "rps"

# Easy to replace later if custom server emojis/assets are made.
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
    ):
        # Quick game, but no artificial Discord timeout while the bot stays online.
        super().__init__(timeout=None)
        self.player_one_id = int(player_one_id)
        self.player_two_id = int(player_two_id)
        self.computer = bool(computer)

        self.choices: dict[int, str] = {}
        self.finished = False
        self._lock = asyncio.Lock()

        for choice in ("rock", "paper", "scissors"):
            self.add_item(RPSChoiceButton(choice))
        self.add_item(RPSCancelButton())

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
            if self.finished:
                await interaction.response.send_message(
                    "This game is already finished.",
                    ephemeral=True,
                )
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

            complete = (
                self.player_one_id in self.choices
                and self.player_two_id in self.choices
            )

            await interaction.response.send_message(
                f"✅ Locked in {choice_text(choice)}.",
                ephemeral=True,
            )

            if interaction.message is None:
                return

            if not complete:
                await interaction.message.edit(
                    content=self.status_content(),
                    view=self,
                )
                return

            self.finished = True
            self._disable_all()
            winner_id = self._winner()

            # Computer games are practice, matching the existing bot pattern.
            if (
                not self.computer
                and interaction.guild_id is not None
                and interaction.message.id
            ):
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

            await interaction.message.edit(
                content=self.result_content(winner_id),
                view=self,
            )

    async def cancel(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)

        if not self._is_allowed_player(user_id):
            await interaction.response.send_message(
                "❌ Only the players can cancel this game.",
                ephemeral=True,
            )
            return

        async with self._lock:
            if self.finished:
                await interaction.response.send_message(
                    "This game is already finished.",
                    ephemeral=True,
                )
                return

            self.finished = True
            self._disable_all()
            await interaction.response.defer()

            if interaction.message is not None:
                await interaction.message.edit(
                    content=(
                        "🪨 📄 ✂️ **Rock Paper Scissors**\n\n"
                        f"❌ Game cancelled by <@{user_id}>."
                    ),
                    view=self,
                )


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
            "Computer games are practice and do not alter the leaderboard."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

        register_game(
            GAME_KEY,
            label="Rock Paper Scissors",
            kind="head_to_head",
            result_word="win",
        )

    def allowed(self, interaction: discord.Interaction) -> bool:
        return self.settings.is_game_allowed(
            interaction.guild_id,
            interaction.channel_id,
            GAME_KEY,
        )

    async def _start(
        self,
        interaction: discord.Interaction,
        *,
        player_two_id: int,
        computer: bool,
    ) -> None:
        view = RockPaperScissorsView(
            player_one_id=interaction.user.id,
            player_two_id=player_two_id,
            computer=computer,
        )

        await interaction.followup.send(
            content=view.status_content(),
            view=view,
            ephemeral=False,
        )

    async def start_game(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
    ) -> None:
        if opponent.bot:
            await interaction.followup.send(
                "❌ Can’t use a Discord bot as the opponent. Choose Computer mode instead.",
                ephemeral=True,
            )
            return

        if opponent.id == interaction.user.id:
            await interaction.followup.send(
                "❌ You can’t play yourself.",
                ephemeral=True,
            )
            return

        await self._start(
            interaction,
            player_two_id=opponent.id,
            computer=False,
        )

    async def start_computer_game(self, interaction: discord.Interaction) -> None:
        bot_user = self.bot.user or interaction.client.user
        if bot_user is None:
            await interaction.followup.send(
                "❌ Computer mode is unavailable until the bot is fully connected.",
                ephemeral=True,
            )
            return

        await self._start(
            interaction,
            player_two_id=int(bot_user.id),
            computer=True,
        )

    @app_commands.command(
        name="rps",
        description="Play Rock Paper Scissors against a player or the Computer",
    )
    @app_commands.describe(
        opponent="Who to play against — leave blank to play the Computer"
    )
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
