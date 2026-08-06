# cogs/leaderboard.py
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.game_registry import discover_games
from core.game_stats import (
    empty_game_stats,
    get_leaderboard,
    get_overall_stats,
    get_player_stats,
    get_rank,
    win_rate,
)
from core.logger import log_cmd
from core.settings import SettingsManager


OVERALL = "overall"
MY_STATS = "__my_stats__"

# Kept for compatibility with anything importing the old constant. It is
# refreshed from the shared game registry whenever the leaderboard is opened.
GAME_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name="Overall", value=OVERALL),
]


def _medal(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**{rank}.**")


def _percent(value: float) -> str:
    return f"{round(value * 100):d}%"


def _member_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(int(user_id))
    return member.mention if member is not None else f"<@{int(user_id)}>"


def _plural(word: str, count: int) -> str:
    word = str(word or "result").strip()
    if count == 1:
        return word
    if word.endswith("y") and not word.endswith(("ay", "ey", "iy", "oy", "uy")):
        return f"{word[:-1]}ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return f"{word}es"
    return f"{word}s"


def _record_text(
    game: str,
    stats: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
) -> str:
    wins = int(stats.get("wins") or 0)
    losses = int(stats.get("losses") or 0)
    draws = int(stats.get("draws") or 0)
    played = int(stats.get("played") or 0)

    if game == OVERALL:
        return f"**{wins}** wins/results • {played} recorded"

    details = catalog.get(game, {})
    if details.get("kind") == "solo":
        word = str(details.get("result_word") or "result")
        return f"**{wins}** {_plural(word, wins)}"

    return f"**{wins}W** • {losses}L • {draws}D • {_percent(win_rate(stats))}"


class LeaderboardGameSelect(discord.ui.Select):
    def __init__(self, cog: "LeaderboardCog", guild_id: int | None):
        self.cog = cog
        super().__init__(
            placeholder="Choose overall, a game, or your stats...",
            min_values=1,
            max_values=1,
            options=cog.leaderboard_select_options(guild_id),
            custom_id="hotbot:leaderboard:game_select:v2",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This leaderboard only works in a server.",
                ephemeral=True,
            )
            return

        value = self.values[0]
        if value == MY_STATS:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.edit_original_response(
                embed=self.cog.build_stats_embed(
                    interaction.guild,
                    interaction.user,
                )
            )
            return

        await interaction.response.defer()
        await interaction.edit_original_response(
            embed=self.cog.build_leaderboard_embed(interaction.guild, value),
            view=LeaderboardView(self.cog, interaction.guild.id),
        )


class LeaderboardView(discord.ui.View):
    def __init__(self, cog: "LeaderboardCog", guild_id: int | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(LeaderboardGameSelect(cog, guild_id))

    async def _switch(self, interaction: discord.Interaction, game: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This leaderboard only works in a server.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await interaction.edit_original_response(
            embed=self.cog.build_leaderboard_embed(interaction.guild, game),
            view=LeaderboardView(self.cog, interaction.guild.id),
        )

    # Compatibility method names retained without creating hardcoded buttons.
    async def overall(self, interaction: discord.Interaction, _button: Any = None) -> None:
        await self._switch(interaction, OVERALL)

    async def tictactoe(self, interaction: discord.Interaction, _button: Any = None) -> None:
        await self._switch(interaction, "tictactoe")

    async def connect4(self, interaction: discord.Interaction, _button: Any = None) -> None:
        await self._switch(interaction, "connect4")

    async def hangman(self, interaction: discord.Interaction, _button: Any = None) -> None:
        await self._switch(interaction, "hangman")

    async def my_stats(self, interaction: discord.Interaction, _button: Any = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This only works in a server.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.edit_original_response(
            embed=self.cog.build_stats_embed(
                interaction.guild,
                interaction.user,
            )
        )


class LeaderboardCog(commands.Cog):
    HELP_META = {
        "title": "Game Leaderboards",
        "summary": "Per-server wins and player records for every loaded game.",
        "details": "Use /leaderboard and choose Overall, a registered game, or My Stats from one dropdown.",
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

    def game_catalog(self, guild_id: int | None) -> dict[str, dict[str, Any]]:
        return dict(discover_games(self.bot, guild_id))

    def _refresh_game_choices(self, guild_id: int | None) -> None:
        GAME_CHOICES[:] = [app_commands.Choice(name="Overall", value=OVERALL)]
        for key, details in self.game_catalog(guild_id).items():
            GAME_CHOICES.append(
                app_commands.Choice(
                    name=str(details["label"])[:100],
                    value=key,
                )
            )
            if len(GAME_CHOICES) >= 25:
                break

    def game_select_options(self, guild_id: int | None) -> list[discord.SelectOption]:
        """Original public helper retained; returns only actual games."""
        options: list[discord.SelectOption] = []
        for game, details in self.game_catalog(guild_id).items():
            options.append(
                discord.SelectOption(
                    label=str(details["label"])[:100],
                    value=game,
                    description=str(details.get("description") or "Game leaderboard")[:100],
                    emoji=str(details.get("emoji") or "") or None,
                )
            )
            if len(options) >= 25:
                break
        return options

    def leaderboard_select_options(
        self,
        guild_id: int | None,
    ) -> list[discord.SelectOption]:
        options = [
            discord.SelectOption(
                label="Overall",
                value=OVERALL,
                description="Combined results from every loaded game",
                emoji="🏆",
            )
        ]

        # Reserve the final option for My Stats. Discord allows 25 options.
        for option in self.game_select_options(guild_id)[:23]:
            options.append(option)

        options.append(
            discord.SelectOption(
                label="My Stats",
                value=MY_STATS,
                description="Show your full game record privately",
                emoji="📊",
            )
        )
        return options

    def _autocomplete_choices(
        self,
        guild_id: int | None,
        current: str,
        *,
        include_overall: bool,
    ) -> list[app_commands.Choice[str]]:
        search = str(current or "").strip().lower()
        rows: list[tuple[str, str]] = []
        if include_overall:
            rows.append(("Overall", OVERALL))
        rows.extend(
            (str(details["label"]), game)
            for game, details in self.game_catalog(guild_id).items()
        )

        choices: list[app_commands.Choice[str]] = []
        for label, value in rows:
            if search and search not in f"{label} {value}".lower():
                continue
            choices.append(app_commands.Choice(name=label[:100], value=value))
            if len(choices) >= 25:
                break
        return choices

    def build_leaderboard_embed(
        self,
        guild: discord.Guild,
        game: str = OVERALL,
    ) -> discord.Embed:
        catalog = self.game_catalog(guild.id)
        game = str(game or OVERALL).strip().lower()
        if game != OVERALL and game not in catalog:
            game = OVERALL

        rows = get_leaderboard(guild.id, game, limit=10)
        label = "Overall" if game == OVERALL else str(catalog[game]["label"])
        embed = discord.Embed(
            title=f"🏆 {guild.name} Game Leaderboard",
            description=f"**{label}** rankings",
            colour=discord.Colour.gold(),
        )

        if not rows:
            embed.add_field(
                name="No results yet",
                value="Finish a game and the leaderboard will start automatically.",
                inline=False,
            )
        else:
            lines = [
                f"{_medal(rank)} {_member_name(guild, row['user_id'])} — "
                f"{_record_text(game, row['stats'], catalog)}"
                for rank, row in enumerate(rows, start=1)
            ]
            embed.add_field(name="Top 10", value="\n".join(lines), inline=False)

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(
            text="Only completed games count. The game list is loaded automatically from installed cogs."
        )
        return embed

    def build_stats_embed(
        self,
        guild: discord.Guild,
        user: discord.Member | discord.User,
        selected_game: str | None = None,
    ) -> discord.Embed:
        catalog = self.game_catalog(guild.id)
        player = get_player_stats(guild.id, user.id)
        games = player.get("games") if isinstance(player, dict) else {}
        if not isinstance(games, dict):
            games = {}

        overall = get_overall_stats(guild.id, user.id)
        rank = get_rank(guild.id, user.id, OVERALL)
        embed = discord.Embed(
            title=f"📊 Game Stats — {user.display_name}",
            colour=discord.Colour.blurple(),
        )

        avatar = getattr(user, "display_avatar", None)
        if avatar is not None:
            embed.set_thumbnail(url=avatar.url)

        embed.description = (
            f"**Server rank:** {'#' + str(rank) if rank is not None else 'Unranked'}\n"
            f"**Total wins/results:** {overall['wins']}\n"
            f"**Completed results:** {overall['played']}"
        )

        selected_game = str(selected_game or "").strip().lower() or None
        shown_games = [selected_game] if selected_game in catalog else list(catalog)

        for game in shown_games:
            details = catalog[game]
            stats = games.get(game)
            if not isinstance(stats, dict):
                stats = empty_game_stats()

            if details.get("kind") == "solo":
                word = str(details.get("result_word") or "result")
                wins = int(stats.get("wins") or 0)
                value = (
                    f"{_plural(word.capitalize(), wins)}: **{wins}**\n"
                    f"Current streak: **{stats['current_streak']}** • "
                    f"Best: **{stats['best_streak']}**\n"
                    "Solo/community results do not give other players losses."
                )
            else:
                game_rank = get_rank(guild.id, user.id, game)
                value = (
                    f"Record: **{stats['wins']}W — {stats['losses']}L — {stats['draws']}D**\n"
                    f"Win rate: **{_percent(win_rate(stats))}**\n"
                    f"Win streak: **{stats['current_streak']}** • "
                    f"Best: **{stats['best_streak']}**\n"
                    f"Rank: **{'#' + str(game_rank) if game_rank is not None else 'Unranked'}**"
                )

            embed.add_field(
                name=str(details["label"]),
                value=value,
                inline=False,
            )

        embed.set_footer(
            text="The same automatic game registry powers /games, /leaderboard, and /stats."
        )
        return embed

    @app_commands.command(
        name="leaderboard",
        description="Show this server's game leaderboard",
    )
    @app_commands.describe(game="Overall rankings or one specific game")
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        game: str | None = None,
    ) -> None:
        log_cmd("leaderboard", interaction)
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.",
                ephemeral=True,
            )
            return

        self._refresh_game_choices(interaction.guild.id)
        selected = str(game or OVERALL).strip().lower()
        catalog = self.game_catalog(interaction.guild.id)
        if selected != OVERALL and selected not in catalog:
            await interaction.response.send_message(
                "That game cog is not currently loaded.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        await interaction.edit_original_response(
            embed=self.build_leaderboard_embed(interaction.guild, selected),
            view=LeaderboardView(self, interaction.guild.id),
        )

    @leaderboard.autocomplete("game")
    async def leaderboard_game_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return self._autocomplete_choices(
            interaction.guild_id,
            current,
            include_overall=True,
        )

    @app_commands.command(
        name="stats",
        description="Show your or another member's game stats",
    )
    @app_commands.describe(
        member="Member to inspect; leave blank for yourself",
        game="Optionally show only one game",
    )
    async def stats(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        game: str | None = None,
    ) -> None:
        log_cmd("stats", interaction)
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.",
                ephemeral=True,
            )
            return

        selected = str(game or "").strip().lower() or None
        catalog = self.game_catalog(interaction.guild.id)
        if selected is not None and selected not in catalog:
            await interaction.response.send_message(
                "That game cog is not currently loaded.",
                ephemeral=True,
            )
            return

        target = member or interaction.user
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.edit_original_response(
            embed=self.build_stats_embed(interaction.guild, target, selected)
        )

    @stats.autocomplete("game")
    async def stats_game_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return self._autocomplete_choices(
            interaction.guild_id,
            current,
            include_overall=False,
        )


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    cog = LeaderboardCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)

    # One persistent dropdown custom ID handles every game dynamically.
    bot.add_view(LeaderboardView(cog))
