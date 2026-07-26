from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.game_stats import (
    GAME_LABELS,
    get_leaderboard,
    get_overall_stats,
    get_player_stats,
    get_rank,
    win_rate,
)
from core.logger import log_cmd
from core.settings import SettingsManager

OVERALL = "overall"
GAME_CHOICES = [
    app_commands.Choice(name="Overall", value=OVERALL),
    app_commands.Choice(name="Tic Tac Toe", value="tictactoe"),
    app_commands.Choice(name="Connect Four", value="connect4"),
    app_commands.Choice(name="Hangman", value="hangman"),
]


def _label(game: str) -> str:
    return "Overall" if game == OVERALL else GAME_LABELS.get(game, game)


def _medal(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**{rank}.**")


def _percent(value: float) -> str:
    return f"{round(value * 100):d}%"


def _member_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(int(user_id))
    if member is not None:
        return member.mention
    return f"<@{int(user_id)}>"


def _record_text(game: str, stats: dict[str, Any]) -> str:
    wins = int(stats.get("wins") or 0)
    losses = int(stats.get("losses") or 0)
    draws = int(stats.get("draws") or 0)
    played = int(stats.get("played") or 0)

    if game == "hangman":
        return f"**{wins}** solve{'s' if wins != 1 else ''}"
    if game == OVERALL:
        return f"**{wins}** wins • {played} recorded"
    return f"**{wins}W** • {losses}L • {draws}D • {_percent(win_rate(stats))}"


class LeaderboardView(discord.ui.View):
    def __init__(self, cog: "LeaderboardCog"):
        # Persistent view: buttons keep working indefinitely and are restored
        # when the bot starts again.
        super().__init__(timeout=None)
        self.cog = cog

    async def _switch(self, interaction: discord.Interaction, game: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This leaderboard only works in a server.",
                ephemeral=True,
            )
            return

        # Acknowledge the button immediately before reading the stats file.
        await interaction.response.defer()
        await interaction.edit_original_response(
            embed=self.cog.build_leaderboard_embed(interaction.guild, game),
            view=self,
        )

    @discord.ui.button(
        label="Overall",
        emoji="🏆",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="hotbot:leaderboard:overall",
    )
    async def overall(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._switch(interaction, OVERALL)

    @discord.ui.button(
        label="Tic Tac Toe",
        emoji="❎",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="hotbot:leaderboard:tictactoe",
    )
    async def tictactoe(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._switch(interaction, "tictactoe")

    @discord.ui.button(
        label="Connect Four",
        emoji="🔴",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="hotbot:leaderboard:connect4",
    )
    async def connect4(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._switch(interaction, "connect4")

    @discord.ui.button(
        label="Hangman",
        emoji="🔥",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="hotbot:leaderboard:hangman",
    )
    async def hangman(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._switch(interaction, "hangman")

    @discord.ui.button(
        label="My Stats",
        emoji="📊",
        style=discord.ButtonStyle.success,
        row=1,
        custom_id="hotbot:leaderboard:my_stats",
    )
    async def my_stats(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.edit_original_response(
            embed=self.cog.build_stats_embed(interaction.guild, interaction.user),
        )


class LeaderboardCog(commands.Cog):
    HELP_META = {
        "title": "Game Leaderboards",
        "summary": "Per-server wins and player records for Hangman, Connect Four and Tic Tac Toe.",
        "details": "Use /leaderboard for the server rankings or /stats to see a player's full game record.",
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

    def build_leaderboard_embed(self, guild: discord.Guild, game: str = OVERALL) -> discord.Embed:
        game = game if game in {OVERALL, *GAME_LABELS.keys()} else OVERALL
        rows = get_leaderboard(guild.id, game, limit=10)

        embed = discord.Embed(
            title=f"🏆 {guild.name} Game Leaderboard",
            description=f"**{_label(game)}** rankings",
            colour=discord.Colour.gold(),
        )

        if not rows:
            embed.add_field(
                name="No results yet",
                value="Finish a game and the leaderboard will start automatically.",
                inline=False,
            )
        else:
            lines: list[str] = []
            for rank, row in enumerate(rows, start=1):
                user = _member_name(guild, row["user_id"])
                lines.append(f"{_medal(rank)} {user} — {_record_text(game, row['stats'])}")
            embed.add_field(name="Top 10", value="\n".join(lines), inline=False)

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(
            text="Only games completed in this server count. Cancelled games are ignored."
        )
        return embed

    def build_stats_embed(
        self,
        guild: discord.Guild,
        user: discord.Member | discord.User,
        selected_game: str | None = None,
    ) -> discord.Embed:
        player = get_player_stats(guild.id, user.id)
        games = player["games"]
        overall = get_overall_stats(guild.id, user.id)
        rank = get_rank(guild.id, user.id, OVERALL)

        embed = discord.Embed(
            title=f"📊 Game Stats — {user.display_name}",
            colour=discord.Colour.blurple(),
        )
        avatar = getattr(user, "display_avatar", None)
        if avatar is not None:
            embed.set_thumbnail(url=avatar.url)

        rank_text = f"#{rank}" if rank is not None else "Unranked"
        embed.description = (
            f"**Server rank:** {rank_text}\n"
            f"**Total wins/solves:** {overall['wins']}\n"
            f"**Completed results:** {overall['played']}"
        )

        shown_games = [selected_game] if selected_game in GAME_LABELS else list(GAME_LABELS)
        for game in shown_games:
            stats = games[game]
            if game == "hangman":
                value = (
                    f"Words solved: **{stats['wins']}**\n"
                    "Hangman is cooperative, so other guessers do not receive losses."
                )
            else:
                game_rank = get_rank(guild.id, user.id, game)
                rank_line = f"Rank: **#{game_rank}**" if game_rank is not None else "Rank: **Unranked**"
                value = (
                    f"Record: **{stats['wins']}W — {stats['losses']}L — {stats['draws']}D**\n"
                    f"Win rate: **{_percent(win_rate(stats))}**\n"
                    f"Win streak: **{stats['current_streak']}** • Best: **{stats['best_streak']}**\n"
                    f"{rank_line}"
                )
            embed.add_field(name=GAME_LABELS[game], value=value, inline=False)

        embed.set_footer(text="Stats begin counting after this update is installed.")
        return embed

    @app_commands.command(name="leaderboard", description="Show this server's game leaderboard")
    @app_commands.describe(game="Overall rankings or one specific game")
    @app_commands.choices(game=GAME_CHOICES)
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        log_cmd("leaderboard", interaction)
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        selected = game.value if game else OVERALL
        await interaction.response.defer(thinking=True)
        await interaction.edit_original_response(
            embed=self.build_leaderboard_embed(interaction.guild, selected),
            view=LeaderboardView(self),
        )

    @app_commands.command(name="stats", description="Show your or another member's game stats")
    @app_commands.describe(
        member="Member to inspect; leave blank for yourself",
        game="Optionally show only one game",
    )
    @app_commands.choices(game=GAME_CHOICES[1:])
    async def stats(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        log_cmd("stats", interaction)
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        target = member or interaction.user
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.edit_original_response(
            embed=self.build_stats_embed(
                interaction.guild,
                target,
                game.value if game else None,
            ),
        )


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    cog = LeaderboardCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)

    # Register the persistent component handlers so existing leaderboard
    # messages continue working after a bot restart.
    bot.add_view(LeaderboardView(cog))
