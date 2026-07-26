# cogs/leaderboard.py
from __future__ import annotations

import re
from typing import Any, Callable, Coroutine

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.game_stats import (
    GAME_KEYS,
    MIN_GAMES_FOR_WIN_RATE,
    get_player_stats,
    leaderboard_entries,
    record_hangman_win,
    win_rate,
)
from core.logger import log_cmd, warn
from core.utils import ensure_deferred

GAME_LABELS = {
    "overall": "Overall",
    "tictactoe": "Tic Tac Toe",
    "connect4": "Connect Four",
    "hangman": "Hangman",
}


def _member_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member is not None:
        return member.display_name
    return f"<@{user_id}>"


def _rate_text(record: dict[str, int]) -> str:
    rate = win_rate(record)
    if rate is None:
        return f"— (<{MIN_GAMES_FOR_WIN_RATE} games)"
    return f"{rate:.1f}%"


def build_leaderboard_embed(
    guild: discord.Guild,
    selected: str,
) -> discord.Embed:
    selected = selected if selected in GAME_KEYS else "overall"
    title = GAME_LABELS[selected]
    entries = leaderboard_entries(guild.id, selected)

    embed = discord.Embed(
        title=f"🏆 {guild.name} — {title} Leaderboard",
        colour=discord.Colour.gold(),
    )

    if not entries:
        embed.description = "No completed games have been recorded yet."
        return embed

    medals = ("🥇", "🥈", "🥉")
    lines: list[str] = []

    for index, (user_id, record) in enumerate(entries[:10], start=1):
        rank = medals[index - 1] if index <= 3 else f"**{index}.**"
        name = _member_name(guild, user_id)
        lines.append(
            f"{rank} **{name}** — "
            f"{record['wins']}W / {record['losses']}L / {record['draws']}D "
            f"• {_rate_text(record)} "
            f"• Best streak {record['best_streak']}"
        )

    embed.description = "\n".join(lines)
    embed.set_footer(
        text=(
            f"Win rate is shown after {MIN_GAMES_FOR_WIN_RATE} games. "
            "Cancellations do not count."
        )
    )
    return embed


def build_stats_embed(
    guild: discord.Guild,
    member: discord.Member,
) -> discord.Embed:
    all_stats = get_player_stats(guild.id, member.id)
    embed = discord.Embed(
        title=f"🎮 Game Stats — {member.display_name}",
        colour=discord.Colour.blurple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    for key in GAME_KEYS:
        record = all_stats[key]
        embed.add_field(
            name=GAME_LABELS[key],
            value=(
                f"Played: **{record['played']}**\n"
                f"W / L / D: **{record['wins']} / {record['losses']} / {record['draws']}**\n"
                f"Win rate: **{_rate_text(record)}**\n"
                f"Streak: **{record['current_streak']}** "
                f"(best **{record['best_streak']}**)"
            ),
            inline=key != "overall",
        )

    return embed


class LeaderboardButton(discord.ui.Button):
    def __init__(self, game_key: str, row: int):
        super().__init__(
            label=GAME_LABELS[game_key],
            style=discord.ButtonStyle.secondary,
            custom_id=f"hotbot:leaderboard:{game_key}:v1",
            row=row,
        )
        self.game_key = game_key

    async def callback(self, interaction: discord.Interaction) -> None:
        view: LeaderboardView = self.view  # type: ignore[assignment]
        if interaction.user.id != view.author_id:
            await interaction.response.send_message(
                "Use `/leaderboard` to open your own controls.",
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "This only works in a server.",
                ephemeral=True,
            )
            return

        view.selected = self.game_key
        view.update_styles()
        await interaction.response.edit_message(
            embed=build_leaderboard_embed(interaction.guild, view.selected),
            view=view,
        )


class LeaderboardView(discord.ui.View):
    def __init__(self, author_id: int, selected: str = "overall"):
        super().__init__(timeout=300)
        self.author_id = int(author_id)
        self.selected = selected if selected in GAME_KEYS else "overall"

        self.add_item(LeaderboardButton("overall", 0))
        self.add_item(LeaderboardButton("tictactoe", 0))
        self.add_item(LeaderboardButton("connect4", 0))
        self.add_item(LeaderboardButton("hangman", 0))
        self.update_styles()

    def update_styles(self) -> None:
        for item in self.children:
            if isinstance(item, LeaderboardButton):
                item.style = (
                    discord.ButtonStyle.primary
                    if item.game_key == self.selected
                    else discord.ButtonStyle.secondary
                )


class LeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._hangman_hook_installed = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._install_hangman_hooks()

    def _install_hangman_hooks(self) -> None:
        """Add Hangman wins to the leaderboard without replacing hangman.py."""
        if self._hangman_hook_installed:
            return

        hangman_cog = self.bot.get_cog("HangmanCog")
        service = getattr(hangman_cog, "service", None)
        if service is None:
            return

        if getattr(service, "_hotbot_game_stats_hooked", False):
            self._hangman_hook_installed = True
            return

        original_letter = getattr(service, "submit_letter", None)
        original_word = getattr(service, "submit_word", None)
        get_game = getattr(service, "_get_game", None)

        if not callable(original_letter) or not callable(original_word) or not callable(get_game):
            warn("Leaderboard could not attach Hangman result hooks.")
            return

        def normalise(value: str) -> str:
            return re.sub(r"\s+", " ", (value or "").strip().upper())

        def prospective_letter_win(
            interaction: discord.Interaction,
            raw_letter: str,
        ) -> tuple[int, int, str] | None:
            if not interaction.guild_id or not interaction.channel_id:
                return None

            game = get_game(interaction.guild_id, interaction.channel_id)
            if not isinstance(game, dict):
                return None

            letter = raw_letter.strip().upper()
            if len(letter) != 1 or not letter.isalpha():
                return None

            word = normalise(str(game.get("word") or ""))
            guessed = {
                str(value).strip().upper()
                for value in game.get("guessed_letters", [])
                if str(value).strip()
            }
            wrong = {
                str(value).strip().upper()
                for value in game.get("wrong_letters", [])
                if str(value).strip()
            }

            if letter in guessed or letter in wrong or letter not in word:
                return None

            after = guessed | {letter}
            solved = all(not char.isalpha() or char in after for char in word)
            if not solved:
                return None

            message_id = int(game.get("message_id") or 0)
            result_id = (
                f"hangman:{interaction.guild_id}:"
                f"{interaction.channel_id}:{message_id}"
            )
            return interaction.guild_id, interaction.channel_id, result_id

        def prospective_word_win(
            interaction: discord.Interaction,
            raw_word: str,
        ) -> tuple[int, int, str] | None:
            if not interaction.guild_id or not interaction.channel_id:
                return None

            game = get_game(interaction.guild_id, interaction.channel_id)
            if not isinstance(game, dict):
                return None

            if normalise(raw_word) != normalise(str(game.get("word") or "")):
                return None

            message_id = int(game.get("message_id") or 0)
            result_id = (
                f"hangman:{interaction.guild_id}:"
                f"{interaction.channel_id}:{message_id}"
            )
            return interaction.guild_id, interaction.channel_id, result_id

        async def wrapped_letter(
            interaction: discord.Interaction,
            raw_letter: str,
        ) -> None:
            possible = prospective_letter_win(interaction, raw_letter)
            await original_letter(interaction, raw_letter)

            if possible is None:
                return

            guild_id, channel_id, result_id = possible
            remaining = get_game(guild_id, channel_id)
            if remaining is None:
                record_hangman_win(
                    guild_id,
                    interaction.user.id,
                    result_id=result_id,
                )

        async def wrapped_word(
            interaction: discord.Interaction,
            raw_word: str,
        ) -> None:
            possible = prospective_word_win(interaction, raw_word)
            await original_word(interaction, raw_word)

            if possible is None:
                return

            guild_id, channel_id, result_id = possible
            remaining = get_game(guild_id, channel_id)
            if remaining is None:
                record_hangman_win(
                    guild_id,
                    interaction.user.id,
                    result_id=result_id,
                )

        service.submit_letter = wrapped_letter
        service.submit_word = wrapped_word
        service._hotbot_game_stats_hooked = True
        self._hangman_hook_installed = True

    @app_commands.command(
        name="leaderboard",
        description="Show this server's game leaderboard.",
    )
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        log_cmd("leaderboard", interaction)
        if interaction.guild is None:
            await interaction.response.send_message(
                "This only works in a server.",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=False)
        view = LeaderboardView(interaction.user.id)
        await interaction.followup.send(
            embed=build_leaderboard_embed(interaction.guild, "overall"),
            view=view,
            ephemeral=False,
        )

    @app_commands.command(
        name="stats",
        description="Show game stats for yourself or another member.",
    )
    @app_commands.describe(member="Member to inspect; defaults to you")
    async def stats(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        log_cmd("stats", interaction)
        if interaction.guild is None:
            await interaction.response.send_message(
                "This only works in a server.",
                ephemeral=True,
            )
            return

        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message(
                "Could not resolve that member.",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=False)
        await interaction.followup.send(
            embed=build_stats_embed(interaction.guild, target),
            ephemeral=False,
        )


async def setup(bot: commands.Bot) -> None:
    cog = LeaderboardCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
