# cogs/dice.py
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


GAME_KEY = "dice"

# Standard dice faces for now. These can be swapped for custom emojis/assets later.
DICE_FACES = {
    1: "⚀",
    2: "⚁",
    3: "⚂",
    4: "⚃",
    5: "⚄",
    6: "⚅",
}


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
    ):
        # No artificial timeout while the bot remains online.
        super().__init__(timeout=None)

        self.player_one_id = int(player_one_id)
        self.player_two_id = int(player_two_id)
        self.computer = bool(computer)

        self.rolls: dict[int, int] = {}
        self.finished = False
        self._lock = asyncio.Lock()

        self.add_item(DiceRollButton())
        self.add_item(DiceCancelButton())

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
        """
        Return the final non-tied rolls plus any tied automatic rerolls.

        The first rolls are already stored in self.rolls. If they tie, both
        dice are automatically rerolled until there is a winner.
        """
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
            lines.extend(
                [
                    "",
                    "🤝 **Tie! Automatic reroll...**",
                ]
            )

            for index, (p1_roll, p2_roll) in enumerate(rerolls, start=1):
                lines.append(
                    f"Reroll {index}: "
                    f"{die_text(p1_roll)} **vs** {die_text(p2_roll)}"
                )
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
            await interaction.response.send_message(
                "❌ This isn’t your Dice Duel.",
                ephemeral=True,
            )
            return

        async with self._lock:
            if self.finished:
                await interaction.response.send_message(
                    "This Dice Duel is already finished.",
                    ephemeral=True,
                )
                return

            if user_id in self.rolls:
                await interaction.response.send_message(
                    "🎲 You’ve already rolled.",
                    ephemeral=True,
                )
                return

            self.rolls[user_id] = roll_die()

            if self.computer:
                self.rolls[self.player_two_id] = roll_die()

            complete = (
                self.player_one_id in self.rolls
                and self.player_two_id in self.rolls
            )

            # Keep individual roll values hidden until both players have rolled.
            await interaction.response.send_message(
                "🎲 **Roll locked in.**",
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

            original_p1 = self.rolls[self.player_one_id]
            original_p2 = self.rolls[self.player_two_id]

            final_p1, final_p2, rerolls = self._resolve_rolls()

            if final_p1 > final_p2:
                winner_id = self.player_one_id
            else:
                winner_id = self.player_two_id

            self.finished = True
            self._disable_all()

            # Computer games are practice only. PvP games count on the leaderboard.
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

    async def cancel(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)

        if not self._is_player(user_id):
            await interaction.response.send_message(
                "❌ Only the players can cancel this Dice Duel.",
                ephemeral=True,
            )
            return

        async with self._lock:
            if self.finished:
                await interaction.response.send_message(
                    "This Dice Duel is already finished.",
                    ephemeral=True,
                )
                return

            self.finished = True
            self._disable_all()

            await interaction.response.defer()

            if interaction.message is not None:
                await interaction.message.edit(
                    content=(
                        "🎲 **Dice Duel**\n\n"
                        f"❌ Game cancelled by <@{user_id}>."
                    ),
                    view=self,
                )


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
            "there is a winner. Computer games are practice and do not alter "
            "the leaderboard."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

        register_game(
            GAME_KEY,
            label="Dice Duel",
            kind="head_to_head",
            result_word="win",
        )

    def allowed(self, interaction: discord.Interaction) -> bool:
        return self.settings.is_game_allowed(
            interaction.guild_id,
            interaction.channel_id,
            GAME_KEY,
        )

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

            player_two_id = int(opponent.id)

        view = DiceDuelView(
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
        await self._start_game(
            interaction,
            opponent,
            computer=False,
        )

    async def start_computer_game(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._start_game(
            interaction,
            None,
            computer=True,
        )

    @app_commands.command(
        name="dice",
        description="Play Dice Duel against a player or the Computer",
    )
    @app_commands.describe(
        opponent="Opponent — leave blank to play the Computer"
    )
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
