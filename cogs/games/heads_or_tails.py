# cogs/heads_or_tails.py
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.game_stats import register_game
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.storage import known_guild_dirs, load_guild_json, save_guild_json
from core.utils import ensure_deferred


__version__ = "1.1.0"

GAME_KEY = "headsortails"
GAMES_FILENAME = "headsortails_games.json"

SIDES: dict[str, tuple[str, str]] = {
    "heads": ("👑", "Heads"),
    "tails": ("🪙", "Tails"),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    def __init__(
        self,
        player_id: int,
        *,
        cog: HeadsOrTailsCog | None = None,
        guild_id: int | None = None,
        channel_id: int | None = None,
        message_id: int | None = None,
        finished: bool = False,
        picked: str | None = None,
        result: str | None = None,
        cancelled_by: int | None = None,
        created_at: str | None = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id or 0)
        self.channel_id = int(channel_id or 0)
        self.message_id = int(message_id or 0)
        self.player_id = int(player_id)
        self.finished = bool(finished)
        self.picked = picked if picked in SIDES else None
        self.result = result if result in SIDES else None
        self.cancelled_by = int(cancelled_by or 0)
        self.created_at = str(created_at or _utc_now())
        self._lock = asyncio.Lock()

        self.add_item(CoinSideButton("heads"))
        self.add_item(CoinSideButton("tails"))
        self.add_item(CoinCancelButton())
        if self.finished:
            self._disable_all()

    def state(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "player_id": self.player_id,
            "finished": self.finished,
            "picked": self.picked,
            "result": self.result,
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

    def start_content(self) -> str:
        return (
            "🪙 **Heads or Tails**\n\n"
            f"<@{self.player_id}>, call it before the coin flips.\n\n"
            "**Pick Heads or Tails below.**"
        )

    def final_content(self) -> str:
        if self.cancelled_by:
            return (
                "🪙 **Heads or Tails**\n\n"
                f"❌ Coin flip cancelled by <@{self.cancelled_by}>."
            )

        if self.picked in SIDES and self.result in SIDES:
            won = self.picked == self.result
            return (
                "🪙 **Heads or Tails**\n\n"
                f"<@{self.player_id}> called {side_text(self.picked)}\n"
                f"The coin landed on {side_text(self.result)}\n\n"
                f"{'🎉 **You got it!**' if won else '❌ **Wrong side!**'}"
            )

        return self.start_content()

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    async def pick(self, interaction: discord.Interaction, picked: str) -> None:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("❌ This coin flip isn’t yours.", ephemeral=True)
            return

        async with self._lock:
            self._sync_ids_from_interaction(interaction)

            if self.finished:
                await interaction.response.send_message("This coin has already been flipped.", ephemeral=True)
                return

            self.finished = True
            self.picked = picked
            self.result = secrets.choice(("heads", "tails"))
            self._disable_all()
            self._persist()

            await interaction.response.defer()

            if interaction.message is not None:
                await interaction.message.edit(content=self.final_content(), view=self)
                self._remove_saved()

    async def cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(
                "❌ Only the player who started this flip can cancel it.",
                ephemeral=True,
            )
            return

        async with self._lock:
            self._sync_ids_from_interaction(interaction)

            if self.finished:
                await interaction.response.send_message("This coin flip is already finished.", ephemeral=True)
                return

            self.finished = True
            self.cancelled_by = self.player_id
            self._disable_all()
            self._persist()
            await interaction.response.defer()

            if interaction.message is not None:
                await interaction.message.edit(content=self.final_content(), view=self)
                self._remove_saved()


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
            "Pick a side using the buttons and the result updates in the same message. "
            "Open coin flips survive Railway restarts."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._restored_once = False

        register_game(GAME_KEY, label="Heads or Tails", kind="solo", result_word="win")

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

    def _view_from_game(self, guild_id: int, raw: Any) -> HeadsOrTailsView | None:
        if not isinstance(raw, dict):
            return None
        try:
            channel_id = int(raw.get("channel_id") or 0)
            message_id = int(raw.get("message_id") or 0)
            player_id = int(raw.get("player_id") or 0)
        except (TypeError, ValueError):
            return None
        if not channel_id or not message_id or not player_id:
            return None

        return HeadsOrTailsView(
            player_id,
            cog=self,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            finished=bool(raw.get("finished")),
            picked=str(raw.get("picked") or "") or None,
            result=str(raw.get("result") or "") or None,
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
                        await message.edit(content=view.final_content(), view=view)
                        stale.append(message_key)
                    else:
                        await message.edit(content=view.start_content(), view=view)
                except discord.NotFound:
                    stale.append(message_key)
                except Exception as exc:
                    warn(
                        f"Heads or Tails restore failed for "
                        f"{guild_id}/{view.channel_id}/{view.message_id}: {exc!r}"
                    )

            if stale:
                for message_key in stale:
                    blob["games"].pop(message_key, None)
                self._save_blob(guild_id, blob)

    def allowed(self, interaction: discord.Interaction) -> bool:
        return self.settings.is_game_allowed(interaction.guild_id, interaction.channel_id, GAME_KEY)

    async def start_game(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.channel_id is None:
            await interaction.followup.send(
                "❌ Heads or Tails must be started in a server channel.",
                ephemeral=True,
            )
            return

        view = HeadsOrTailsView(
            interaction.user.id,
            cog=self,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
        )
        message = await interaction.followup.send(
            content=view.start_content(),
            view=view,
            ephemeral=False,
            wait=True,
        )
        view.message_id = int(message.id)
        view._persist()

    @app_commands.command(name="headsortails", description="Call Heads or Tails and flip a coin")
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
