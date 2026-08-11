# cogs/heads_or_tails.py
from __future__ import annotations

import asyncio
import secrets

import discord
from discord import app_commands
from discord.ext import commands

from core.game_stats import register_game
from core.logger import log_cmd
from core.settings import SettingsManager
from core.utils import ensure_deferred


GAME_KEY = "headsortails"

# Easy to replace later if custom coin graphics/emojis are made.
SIDES: dict[str, tuple[str, str]] = {
    "heads": ("👑", "Heads"),
    "tails": ("🪙", "Tails"),
}


def side_text(side: str) -> str:
    emoji, label = SIDES[side]
    return f"{emoji} **{label}**"


class CoinSideButton(discord.ui.Button):
    def __init__(self, side: str):
        emoji, label = SIDES[side]
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.primary,
            custom_id=f"headsortails:{side}",
            row=0,
        )
        self.side = side

    async def callback(self, interaction: discord.Interaction) -> None:
        view: HeadsOrTailsView = self.view  # type: ignore[assignment]
        await view.pick(interaction, self.side)


class CoinCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Cancel",
            emoji="✖️",
            style=discord.ButtonStyle.danger,
            custom_id="headsortails:cancel",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: HeadsOrTailsView = self.view  # type: ignore[assignment]
        await view.cancel(interaction)


class HeadsOrTailsView(discord.ui.View):
    def __init__(self, player_id: int):
        # No artificial Discord timeout while the bot stays online.
        super().__init__(timeout=None)
        self.player_id = int(player_id)
        self.finished = False
        self._lock = asyncio.Lock()

        self.add_item(CoinSideButton("heads"))
        self.add_item(CoinSideButton("tails"))
        self.add_item(CoinCancelButton())

    def start_content(self) -> str:
        return (
            "🪙 **Heads or Tails**\n\n"
            f"<@{self.player_id}>, call it before the coin flips.\n\n"
            "**Pick Heads or Tails below.**"
        )

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    async def pick(self, interaction: discord.Interaction, picked: str) -> None:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(
                "❌ This coin flip isn’t yours.",
                ephemeral=True,
            )
            return

        async with self._lock:
            if self.finished:
                await interaction.response.send_message(
                    "This coin has already been flipped.",
                    ephemeral=True,
                )
                return

            self.finished = True
            self._disable_all()
            result = secrets.choice(("heads", "tails"))
            won = picked == result

            await interaction.response.defer()

            if interaction.message is not None:
                await interaction.message.edit(
                    content=(
                        "🪙 **Heads or Tails**\n\n"
                        f"<@{self.player_id}> called {side_text(picked)}\n"
                        f"The coin landed on {side_text(result)}\n\n"
                        f"{'🎉 **You got it!**' if won else '❌ **Wrong side!**'}"
                    ),
                    view=self,
                )

    async def cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(
                "❌ Only the player who started this flip can cancel it.",
                ephemeral=True,
            )
            return

        async with self._lock:
            if self.finished:
                await interaction.response.send_message(
                    "This coin flip is already finished.",
                    ephemeral=True,
                )
                return

            self.finished = True
            self._disable_all()
            await interaction.response.defer()

            if interaction.message is not None:
                await interaction.message.edit(
                    content=(
                        "🪙 **Heads or Tails**\n\n"
                        f"❌ Coin flip cancelled by <@{self.player_id}>."
                    ),
                    view=self,
                )


class HeadsOrTailsCog(commands.Cog):
    GAME_META = {
        "key": GAME_KEY,
        "label": "Heads or Tails",
        "kind": "solo",
        "result_word": "win",
        "description": "Call heads or tails and let the bot flip the coin",
        "emoji": "🪙",
        "requires_opponent": False,
    }

    HELP_META = {
        "title": "Heads or Tails",
        "summary": "Call Heads or Tails, then let the bot flip the coin.",
        "details": (
            "Use /headsortails or start Heads or Tails from /games. "
            "Pick a side using the buttons and the result updates in the same message."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

        register_game(
            GAME_KEY,
            label="Heads or Tails",
            kind="solo",
            result_word="win",
        )

    def allowed(self, interaction: discord.Interaction) -> bool:
        return self.settings.is_game_allowed(
            interaction.guild_id,
            interaction.channel_id,
            GAME_KEY,
        )

    async def start_game(self, interaction: discord.Interaction) -> None:
        view = HeadsOrTailsView(interaction.user.id)
        await interaction.followup.send(
            content=view.start_content(),
            view=view,
            ephemeral=False,
        )

    @app_commands.command(
        name="headsortails",
        description="Call Heads or Tails and flip a coin",
    )
    async def headsortails(self, interaction: discord.Interaction) -> None:
        log_cmd("headsortails", interaction)

        if not self.allowed(interaction):
            await interaction.response.send_message(
                "❌ `/headsortails` can only be used in the configured game channel(s).",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=False)
        await self.start_game(interaction)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = HeadsOrTailsCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
