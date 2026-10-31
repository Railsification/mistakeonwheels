# cogs/games/bingo.py
"""WoS Bingo: a channel-wide word bingo game.

* One game per channel. Anyone can join; each player gets a private 5x5 card
  of WoS words with a FREE centre square.
* Players mark their own card (no auto-marking). A square can only be marked
  once its word has been called.
* Words are called automatically on a timer and/or by the host by hand.
* The first valid BINGO! claim wins and is recorded once on the shared games
  leaderboard.
* All state lives in data/guilds/<guild>/bingo_games.json and every button is
  persistent, so a game carries on exactly where it was after a restart.
"""
from __future__ import annotations

__version__ = "1.0.0"

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from core.bingo_logic import (
    CARD_SQUARES,
    DEFAULT_WORDS,
    FINISHED_STATUSES,
    FREE_POS,
    GRID,
    LIVE_STATUSES,
    MAX_INTERVAL,
    MIN_INTERVAL,
    MODE_FULL,
    MODE_LINE,
    MODES,
    STATUS_ENDED,
    STATUS_EXHAUSTED,
    STATUS_LOBBY,
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_WON,
    DEFAULT_INTERVAL,
    build_card,
    clamp_interval,
    clean_words,
    completed_line,
    normalise_game,
    normalise_mode,
    pick_next_call,
    squares_to_go,
    valid_marks,
)
from core.command_scope import bind_public_cog
from core.game_stats import record_solo_result
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.storage import (
    guild_json_path,
    known_guild_dirs,
    load_guild_json,
    save_guild_json,
)
from core.utils import ensure_deferred

GAME_KEY = "bingo"
GAME_LABEL = "Bingo"
GAMES_FILENAME = "bingo_games.json"
TICK_SECONDS = 3
RECENT_CALLS_SHOWN = 15
COG_NAME = "BingoCog"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jump(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _service_from(interaction: discord.Interaction) -> "BingoService | None":
    cog = interaction.client.get_cog(COG_NAME)  # type: ignore[union-attr]
    return getattr(cog, "service", None)


HOW_TO_PLAY_GOAL = (
    "Be the first to complete a line (or the whole card in Full card games) of "
    "called WoS words, then press 🎉 BINGO!"
)
HOW_TO_PLAY_STEPS = (
    "1. Press **🎟️ My Card** on the game message to join. Your 5×5 card of WoS "
    "words is private, so only you can see it. The centre square is FREE.\n"
    "2. HotBot calls one word at a time. Every call is posted as a new message, "
    "and the game message shows the most recent calls.\n"
    "3. **You mark your own card.** Tap a word once it has been called and it "
    "turns green. HotBot never marks it for you, so watch the calls. Tap a "
    "marked word again to unmark it.\n"
    "4. When you have a full row, column or diagonal (or every square in Full "
    "card mode), press **🎉 BINGO!** on the game message."
)
HOW_TO_PLAY_RULES = (
    "• You can only mark a word after it has been called. Marking it late is allowed.\n"
    "• A BINGO! claim is checked. If you don't have a win yet, HotBot tells you how "
    "many squares you still need, and there's no penalty.\n"
    "• The first valid claim wins and ends the game. The win counts on the shared "
    "games leaderboard.\n"
    "• You can join at any time, even after calling has started.\n"
    "• If every word is called and nobody claims, the game ends with no winner.\n"
    "• The host (whoever started it) or a server admin controls calling: start, "
    "pause, call the next word by hand, change speed and end the game.\n"
    "• Games and cards carry on after a bot restart. If your private card has "
    "disappeared, press My Card again and you'll get the same card back."
)
HOW_TO_PLAY_COMMANDS = (
    "`/bingo` or `!bingo start` starts a game, and `!bingo start full` starts a "
    "Full card game.\n"
    "Host/admin: `!bingo go` (start or resume auto-calling), `!bingo pause`, "
    "`!bingo call` (call the next word now), `!bingo speed 20` (seconds between "
    f"calls, {MIN_INTERVAL}–{MAX_INTERVAL}), `!bingo end`.\n"
    "Anyone: `!bingo` (status and a link to the game) and `!bingo help`."
)


def build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎱 WoS Bingo: How to Play",
        description=HOW_TO_PLAY_GOAL,
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="How to Play", value=HOW_TO_PLAY_STEPS[:1024], inline=False)
    embed.add_field(name="Rules", value=HOW_TO_PLAY_RULES[:1024], inline=False)
    embed.add_field(name="Commands", value=HOW_TO_PLAY_COMMANDS[:1024], inline=False)
    return embed


class BingoSquare(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"hotbot:bingo:sq:(?P<game>[0-9]{1,20}):(?P<pos>[0-9]{1,2})",
):
    """One square on a player's private card.

    The custom_id carries the game's message ID and the square position, so the
    button keeps working after a restart with no per-message registration.
    """

    def __init__(
        self,
        game_id: int,
        pos: int,
        label: str = "·",
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=str(label)[:80] or "·",
                style=style,
                custom_id=f"hotbot:bingo:sq:{int(game_id)}:{int(pos)}",
                disabled=disabled,
                row=int(pos) // GRID,
            )
        )
        self.game_id = int(game_id)
        self.pos = int(pos)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(int(match["game"]), int(match["pos"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        service = _service_from(interaction)
        if service is None:
            await interaction.response.send_message(
                "Bingo is not loaded right now. Try again in a moment.", ephemeral=True
            )
            return
        await service.mark_square(interaction, self.game_id, self.pos)


class BingoView(discord.ui.View):
    """Persistent controls on the public game message."""

    def __init__(self, service: "BingoService", status: str = STATUS_LOBBY) -> None:
        super().__init__(timeout=None)
        self.service = service
        if status == STATUS_RUNNING:
            self.start_pause.label = "Pause"
            self.start_pause.emoji = "⏸️"
        elif status == STATUS_PAUSED:
            self.start_pause.label = "Resume Auto"
            self.start_pause.emoji = "▶️"
        else:
            self.start_pause.label = "Start Calling"
            self.start_pause.emoji = "▶️"

    @discord.ui.button(
        label="My Card", emoji="🎟️", style=discord.ButtonStyle.primary,
        custom_id="hotbot:bingo:card:v1", row=0,
    )
    async def my_card(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.service.show_card(interaction)

    @discord.ui.button(
        label="BINGO!", emoji="🎉", style=discord.ButtonStyle.success,
        custom_id="hotbot:bingo:claim:v1", row=0,
    )
    async def claim(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.service.claim(interaction)

    @discord.ui.button(
        label="How to Play", emoji="📖", style=discord.ButtonStyle.secondary,
        custom_id="hotbot:bingo:help:v1", row=0,
    )
    async def how_to_play(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)

    @discord.ui.button(
        label="Start Calling", emoji="▶️", style=discord.ButtonStyle.secondary,
        custom_id="hotbot:bingo:startpause:v1", row=1,
    )
    async def start_pause(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.service.button_action(interaction, "toggle")

    @discord.ui.button(
        label="Call Next", emoji="📣", style=discord.ButtonStyle.secondary,
        custom_id="hotbot:bingo:call:v1", row=1,
    )
    async def call_next(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.service.button_action(interaction, "call")

    @discord.ui.button(
        label="End", emoji="🛑", style=discord.ButtonStyle.danger,
        custom_id="hotbot:bingo:end:v1", row=1,
    )
    async def end(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.service.ask_end(interaction)


class EndConfirmView(discord.ui.View):
    """Short-lived private confirmation; intentionally expires after 60s."""

    def __init__(self, service: "BingoService", guild_id: int, channel_id: int, game_id: int) -> None:
        super().__init__(timeout=60)
        self.service = service
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.game_id = game_id

    @discord.ui.button(label="Yes, end the game", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Ending Bingo…", view=None)
        text = await self.service.action_end(
            self.guild_id, self.channel_id, interaction.user, expected_game_id=self.game_id
        )
        try:
            await interaction.edit_original_response(content=text)
        except Exception:
            pass

    @discord.ui.button(label="Keep playing", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="The game is still on.", view=None)


class BingoService:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        # (guild_id, channel_id) pairs that may hold a game; the ticker only
        # looks at these instead of reading every guild's file every tick.
        self._active: set[tuple[int, int]] = set()

    # ---------- locking / permissions ----------

    def _lock_for(self, guild_id: int, channel_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(channel_id))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def allowed(self, guild_id: int | None, channel_id: int | None) -> bool:
        if not guild_id or not channel_id:
            return False
        try:
            return bool(self.settings.is_game_allowed(guild_id, channel_id, GAME_KEY))
        except Exception:
            return bool(self.settings.is_feature_allowed(guild_id, channel_id, "games"))

    def _is_admin(self, member: Any) -> bool:
        if not isinstance(member, discord.Member):
            return False
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild or perms.manage_messages:
            return True
        role_names = set((getattr(self.bot, "hot_config", None) or {}).get("admin_role_names") or [])
        return any(role.name in role_names for role in member.roles)

    def is_controller(self, member: Any, game: dict[str, Any]) -> bool:
        if member is None:
            return False
        if int(getattr(member, "id", 0)) == int(game.get("host_id") or 0):
            return True
        return self._is_admin(member)

    # ---------- storage ----------

    def _load_blob(self, guild_id: int) -> dict[str, Any]:
        raw = load_guild_json(guild_id, GAMES_FILENAME, {"games": {}})
        if not isinstance(raw, dict):
            raw = {"games": {}}
        if not isinstance(raw.get("games"), dict):
            raw["games"] = {}
        return raw

    def _get_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        raw = self._load_blob(guild_id)["games"].get(str(int(channel_id)))
        return normalise_game(raw)

    def _set_game(self, guild_id: int, channel_id: int, game: dict[str, Any]) -> None:
        blob = self._load_blob(guild_id)
        blob["games"][str(int(channel_id))] = game
        save_guild_json(guild_id, GAMES_FILENAME, blob)
        self._active.add((int(guild_id), int(channel_id)))

    def _remove_game(self, guild_id: int, channel_id: int) -> None:
        blob = self._load_blob(guild_id)
        blob["games"].pop(str(int(channel_id)), None)
        save_guild_json(guild_id, GAMES_FILENAME, blob)
        self._active.discard((int(guild_id), int(channel_id)))

    def load_active_index(self) -> None:
        """Find saved games after a restart without creating files for every guild."""
        for guild_id in known_guild_dirs():
            try:
                if not guild_json_path(guild_id, GAMES_FILENAME).exists():
                    continue
                for channel_id in self._load_blob(guild_id)["games"].keys():
                    if str(channel_id).isdigit():
                        self._active.add((int(guild_id), int(channel_id)))
            except Exception as exc:
                warn(f"bingo: could not index saved games for guild {guild_id}: {exc!r}")

    # ---------- Discord helpers ----------

    async def _channel(self, channel_id: int) -> Any | None:
        channel = self.bot.get_channel(int(channel_id))
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(int(channel_id))
        except Exception as exc:
            warn(f"bingo: fetch channel {channel_id} failed: {exc!r}")
            return None

    def _word(self, game: dict[str, Any], idx: int) -> str:
        words = game["words"]
        return words[idx] if 0 <= idx < len(words) else "?"

    def _latest_call_text(self, game: dict[str, Any]) -> str:
        if not game["called"]:
            return "Nothing called yet."
        return f"**#{len(game['called'])}: {self._word(game, game['called'][-1])}**"

    def build_main_embed(self, game: dict[str, Any], *, ended_by: int = 0) -> discord.Embed:
        status = game["status"]
        mode_label = MODES.get(game["mode"], MODES[MODE_LINE])
        colour = discord.Colour.gold()
        if status == STATUS_LOBBY:
            desc = (
                "Press **🎟️ My Card** to join and get your private card.\n"
                f"<@{game['host_id']}> presses **▶️ Start Calling** when everyone is in."
            )
        elif status == STATUS_RUNNING:
            colour = discord.Colour.green()
            desc = (
                f"Calling a new word every **{game['interval']}s**.\n"
                "Mark your own card. **HotBot won't do it for you!**"
            )
        elif status == STATUS_PAUSED:
            colour = discord.Colour.orange()
            desc = "⏸️ Auto-calling is paused. The host can call words by hand or resume."
        elif status == STATUS_WON:
            colour = discord.Colour.purple()
            winner = int(game.get("winner_id") or 0)
            card = game["players"].get(str(winner), {}).get("card") or []
            words = [
                "FREE" if pos == FREE_POS else self._word(game, card[pos])
                for pos in game.get("winning_positions") or []
                if 0 <= pos < len(card)
            ]
            desc = f"🎉 <@{winner}> got **BINGO!**"
            if words and game["mode"] == MODE_LINE:
                desc += "\nWinning line: " + " · ".join(words)
        elif status == STATUS_EXHAUSTED:
            colour = discord.Colour.dark_grey()
            desc = "Every word has been called and nobody claimed BINGO. No winner this time."
        else:
            colour = discord.Colour.dark_grey()
            by = ended_by or int(game.get("ended_by") or 0)
            desc = f"🛑 Game ended by <@{by}>." if by else "🛑 Game ended."

        title = "🎱 WoS Bingo" if status in LIVE_STATUSES else "🎱 WoS Bingo: Finished"
        embed = discord.Embed(title=title, description=desc, colour=colour)
        embed.add_field(name="📣 Latest call", value=self._latest_call_text(game), inline=False)
        embed.add_field(name="Win", value=mode_label, inline=True)
        embed.add_field(name="Players", value=str(len(game["players"])), inline=True)
        embed.add_field(name="Calls", value=f"{len(game['called'])}/{len(game['words'])}", inline=True)
        if len(game["called"]) > 1:
            earlier = [self._word(game, i) for i in reversed(game["called"][:-1])]
            shown = earlier[:RECENT_CALLS_SHOWN]
            text = " · ".join(shown)
            if len(earlier) > len(shown):
                text += f" · …(+{len(earlier) - len(shown)} more)"
            embed.add_field(name="Earlier calls (newest first)", value=text[:1024], inline=False)
        if status in LIVE_STATUSES:
            embed.set_footer(text=f"Host controls: buttons below or !bingo help · Every {game['interval']}s when running")
        return embed

    def build_card_embed(self, game: dict[str, Any], user_id: int) -> discord.Embed:
        player = game["players"][str(int(user_id))]
        marked = valid_marks(player["card"], player["marks"], game["called"])
        to_go = squares_to_go(marked, game["mode"])
        goal = "the whole card" if game["mode"] == MODE_FULL else "a row, column or diagonal"
        lines = [
            f"Win: **{goal}** · Marked **{len(marked) - 1}/{CARD_SQUARES - 1}**",
            f"Latest call: {self._latest_call_text(game)}",
            "Tap a word **after it's been called** to mark it ✅. Tap it again to unmark it.",
        ]
        if to_go == 0:
            lines.append("**🎉 You have BINGO! Press 🎉 BINGO! on the game message now!**")
        else:
            lines.append(f"{to_go} more square{'s' if to_go != 1 else ''} to win.")
        embed = discord.Embed(
            title="🎟️ Your Bingo Card",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text="Only you can see this card. Lost it? Press My Card again.")
        return embed

    def build_card_view(self, game: dict[str, Any], user_id: int) -> discord.ui.View:
        player = game["players"][str(int(user_id))]
        card = player["card"]
        marks = set(player["marks"])
        view = discord.ui.View(timeout=None)
        for pos in range(CARD_SQUARES):
            if pos == FREE_POS:
                view.add_item(BingoSquare(
                    game["message_id"], pos, label="⭐ FREE",
                    style=discord.ButtonStyle.success, disabled=True,
                ))
                continue
            word = self._word(game, card[pos])
            marked = pos in marks
            view.add_item(BingoSquare(
                game["message_id"], pos,
                label=f"✅ {word}" if marked else word,
                style=discord.ButtonStyle.success if marked else discord.ButtonStyle.secondary,
            ))
        return view

    async def _refresh_main(self, game: dict[str, Any]) -> bool:
        """Edit the public game message. Returns False if it no longer exists."""
        channel = await self._channel(game["channel_id"])
        if channel is None:
            return True
        live = game["status"] in LIVE_STATUSES
        try:
            await channel.get_partial_message(game["message_id"]).edit(
                embed=self.build_main_embed(game),
                view=BingoView(self, game["status"]) if live else None,
            )
        except discord.NotFound:
            return False
        except Exception as exc:
            warn(f"bingo: edit game message failed: {exc!r}")
        return True

    async def _delete_ticker(self, game: dict[str, Any]) -> None:
        old = int(game.get("call_message_id") or 0)
        if not old:
            return
        channel = await self._channel(game["channel_id"])
        if channel is None:
            return
        try:
            await channel.get_partial_message(old).delete()
        except Exception:
            pass

    # ---------- start ----------

    def start_issue(self, guild_id: int | None, channel_id: int | None) -> str | None:
        if not guild_id or not channel_id:
            return "Bingo must be started in a server channel."
        if not self.allowed(guild_id, channel_id):
            return "❌ Bingo can only be played in the configured games channel(s)."
        existing = self._get_game(guild_id, channel_id)
        if existing:
            return (
                "A Bingo game is already running in this channel. "
                f"[Open it]({_jump(guild_id, channel_id, existing['message_id'])})"
            )
        return None

    async def _create_game(
        self,
        guild_id: int,
        channel_id: int,
        host_id: int,
        mode: str,
        send: Callable[[discord.Embed, discord.ui.View], Awaitable[Any]],
    ) -> str | None:
        """Post and save a new game. Returns an error message, or None on success."""
        async with self._lock_for(guild_id, channel_id):
            issue = self.start_issue(guild_id, channel_id)
            if issue:
                return issue
            game: dict[str, Any] = {
                "guild_id": int(guild_id),
                "channel_id": int(channel_id),
                "message_id": 0,
                "host_id": int(host_id),
                "mode": normalise_mode(mode),
                "words": clean_words(DEFAULT_WORDS),
                "called": [],
                "status": STATUS_LOBBY,
                "interval": DEFAULT_INTERVAL,
                "next_call_at": 0.0,
                "call_message_id": 0,
                "players": {},
                "created_at": _utc_now(),
            }
            game = normalise_game(game) or game
            message = await send(self.build_main_embed(game), BingoView(self, STATUS_LOBBY))
            game["message_id"] = int(message.id)
            self._set_game(guild_id, channel_id, game)
            return None

    async def start_game(self, interaction: discord.Interaction, mode: str = MODE_LINE) -> None:
        """Launcher used by /bingo and the /games menu (interaction already acknowledged)."""
        guild_id, channel_id = interaction.guild_id, interaction.channel_id
        issue = self.start_issue(guild_id, channel_id)
        if issue:
            await interaction.followup.send(issue, ephemeral=True)
            return

        async def send(embed: discord.Embed, view: discord.ui.View) -> Any:
            return await interaction.followup.send(embed=embed, view=view, wait=True)

        issue = await self._create_game(int(guild_id), int(channel_id), interaction.user.id, mode, send)
        if issue:
            await interaction.followup.send(issue, ephemeral=True)

    async def start_from_context(self, ctx: commands.Context, mode: str) -> str | None:
        guild_id = ctx.guild.id if ctx.guild else 0
        channel_id = ctx.channel.id if ctx.channel else 0

        async def send(embed: discord.Embed, view: discord.ui.View) -> Any:
            return await ctx.send(embed=embed, view=view)

        return await self._create_game(guild_id, channel_id, ctx.author.id, mode, send)

    # ---------- player actions ----------

    def _active_game_for(self, interaction: discord.Interaction) -> dict[str, Any] | None:
        if not interaction.guild_id or not interaction.channel_id or interaction.message is None:
            return None
        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if not game or game["message_id"] != interaction.message.id:
            return None
        if game["status"] not in LIVE_STATUSES:
            return None
        return game

    async def show_card(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id or not interaction.channel_id:
            return
        joined = False
        async with self._lock_for(interaction.guild_id, interaction.channel_id):
            game = self._active_game_for(interaction)
            if game is None:
                await interaction.response.send_message(
                    "That Bingo game has finished. Start a new one with `/bingo` or `!bingo start`.",
                    ephemeral=True,
                )
                return
            key = str(interaction.user.id)
            if key not in game["players"]:
                game["players"][key] = {
                    "card": build_card(len(game["words"])),
                    "marks": [],
                    "joined_at": _utc_now(),
                }
                self._set_game(interaction.guild_id, interaction.channel_id, game)
                joined = True
            await interaction.response.send_message(
                embed=self.build_card_embed(game, interaction.user.id),
                view=self.build_card_view(game, interaction.user.id),
                ephemeral=True,
            )
            if joined:
                await self._refresh_main(game)

    async def mark_square(self, interaction: discord.Interaction, game_id: int, pos: int) -> None:
        if not interaction.guild_id or not interaction.channel_id:
            return
        async with self._lock_for(interaction.guild_id, interaction.channel_id):
            game = self._get_game(interaction.guild_id, interaction.channel_id)
            if not game or game["status"] not in LIVE_STATUSES:
                await interaction.response.send_message("That Bingo game has finished.", ephemeral=True)
                return
            if game["message_id"] != int(game_id):
                await interaction.response.send_message(
                    "That card is from an older game. Press **🎟️ My Card** on the current game.",
                    ephemeral=True,
                )
                return
            player = game["players"].get(str(interaction.user.id))
            if player is None:
                await interaction.response.send_message(
                    "That isn't your card. Press **🎟️ My Card** to get your own.", ephemeral=True
                )
                return
            if not 0 <= pos < CARD_SQUARES or pos == FREE_POS:
                await interaction.response.send_message("The FREE square is always marked.", ephemeral=True)
                return
            marks = set(player["marks"])
            if pos in marks:
                marks.discard(pos)
            else:
                word_idx = player["card"][pos]
                if word_idx not in game["called"]:
                    await interaction.response.send_message(
                        f"**{self._word(game, word_idx)}** hasn't been called yet.", ephemeral=True
                    )
                    return
                marks.add(pos)
            player["marks"] = sorted(marks)
            self._set_game(interaction.guild_id, interaction.channel_id, game)
            await interaction.response.edit_message(
                embed=self.build_card_embed(game, interaction.user.id),
                view=self.build_card_view(game, interaction.user.id),
            )

    async def claim(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id or not interaction.channel_id:
            return
        async with self._lock_for(interaction.guild_id, interaction.channel_id):
            game = self._active_game_for(interaction)
            if game is None:
                await interaction.response.send_message("That Bingo game has already finished.", ephemeral=True)
                return
            player = game["players"].get(str(interaction.user.id))
            if player is None:
                await interaction.response.send_message(
                    "You don't have a card yet. Press **🎟️ My Card** to join.", ephemeral=True
                )
                return
            marked = valid_marks(player["card"], player["marks"], game["called"])
            line = completed_line(marked, game["mode"])
            if line is None:
                to_go = squares_to_go(marked, game["mode"])
                await interaction.response.send_message(
                    f"Not yet! You need **{to_go}** more marked square{'s' if to_go != 1 else ''}. "
                    "Check your card for called words you haven't marked.",
                    ephemeral=True,
                )
                return
            game["status"] = STATUS_WON
            game["winner_id"] = int(interaction.user.id)
            game["winning_positions"] = list(line)
            game["next_call_at"] = 0.0
            game["finished_at"] = _utc_now()
            self._set_game(interaction.guild_id, interaction.channel_id, game)
            await interaction.response.send_message("🎉 BINGO confirmed! You win!", ephemeral=True)
            await self._finalize(game)

    # ---------- host actions ----------

    async def button_action(self, interaction: discord.Interaction, action: str) -> None:
        if not interaction.guild_id or not interaction.channel_id:
            return
        game = self._active_game_for(interaction)
        if game is None:
            await interaction.response.send_message("That Bingo game has finished.", ephemeral=True)
            return
        if not self.is_controller(interaction.user, game):
            await interaction.response.send_message(
                f"Only the host (<@{game['host_id']}>) or a server admin can do that.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if action == "call":
            text = await self.action_call(interaction.guild_id, interaction.channel_id, interaction.user)
        else:
            text = await self.action_auto(interaction.guild_id, interaction.channel_id, interaction.user, "toggle")
        await interaction.followup.send(text, ephemeral=True)

    async def ask_end(self, interaction: discord.Interaction) -> None:
        game = self._active_game_for(interaction)
        if game is None:
            await interaction.response.send_message("That Bingo game has already finished.", ephemeral=True)
            return
        if not self.is_controller(interaction.user, game):
            await interaction.response.send_message(
                f"Only the host (<@{game['host_id']}>) or a server admin can end the game.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "End this Bingo game for everyone? Nobody will win it.",
            view=EndConfirmView(self, interaction.guild_id, interaction.channel_id, game["message_id"]),
            ephemeral=True,
        )

    async def action_call(self, guild_id: int, channel_id: int, member: Any) -> str:
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or game["status"] not in LIVE_STATUSES:
                return "There is no Bingo game running here."
            if not self.is_controller(member, game):
                return f"Only the host (<@{game['host_id']}>) or a server admin can call words."
            if game["status"] == STATUS_LOBBY:
                # Calling by hand before auto-calling starts leaves auto off.
                game["status"] = STATUS_PAUSED
            word = await self._call_next(game)
            if word is None:
                return "Every word has been called. The game is over."
            return f"Called **{word}**."

    async def action_auto(self, guild_id: int, channel_id: int, member: Any, want: str) -> str:
        """want: 'toggle', 'resume' or 'pause'."""
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or game["status"] not in LIVE_STATUSES:
                return "There is no Bingo game running here."
            if not self.is_controller(member, game):
                return f"Only the host (<@{game['host_id']}>) or a server admin can control calling."
            running = game["status"] == STATUS_RUNNING
            if want == "toggle":
                want = "pause" if running else "resume"
            if want == "pause":
                if not running:
                    return "Auto-calling is already paused."
                game["status"] = STATUS_PAUSED
                game["next_call_at"] = 0.0
                self._set_game(guild_id, channel_id, game)
                await self._refresh_main(game)
                return "⏸️ Auto-calling paused."
            if running:
                return "Auto-calling is already running."
            game["status"] = STATUS_RUNNING
            word = await self._call_next(game)  # first call straight away
            if word is None:
                return "Every word has been called. The game is over."
            return f"▶️ Auto-calling every {game['interval']}s. First call: **{word}**."

    async def action_speed(self, guild_id: int, channel_id: int, member: Any, raw: str) -> str:
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or game["status"] not in LIVE_STATUSES:
                return "There is no Bingo game running here."
            if not self.is_controller(member, game):
                return f"Only the host (<@{game['host_id']}>) or a server admin can change the speed."
            text = str(raw or "").strip().lower().rstrip("s")
            if not text.isdigit():
                return f"Use `!bingo speed <seconds>`, between {MIN_INTERVAL} and {MAX_INTERVAL}. Now: {game['interval']}s."
            game["interval"] = clamp_interval(text)
            if game["status"] == STATUS_RUNNING:
                game["next_call_at"] = min(
                    float(game.get("next_call_at") or 0) or time.time() + game["interval"],
                    time.time() + game["interval"],
                )
            self._set_game(guild_id, channel_id, game)
            await self._refresh_main(game)
            return f"Calls now come every **{game['interval']}s**."

    async def action_end(
        self, guild_id: int, channel_id: int, member: Any, *, expected_game_id: int = 0
    ) -> str:
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or game["status"] not in LIVE_STATUSES:
                return "There is no Bingo game running here."
            if expected_game_id and game["message_id"] != int(expected_game_id):
                return "That game has already finished."
            if not self.is_controller(member, game):
                return f"Only the host (<@{game['host_id']}>) or a server admin can end the game."
            game["status"] = STATUS_ENDED
            game["ended_by"] = int(getattr(member, "id", 0))
            game["next_call_at"] = 0.0
            game["finished_at"] = _utc_now()
            self._set_game(guild_id, channel_id, game)
            await self._finalize(game)
            return "🛑 Bingo ended. Anyone can start a new game."

    # ---------- calling / finishing (call with the lock held) ----------

    async def _call_next(self, game: dict[str, Any]) -> str | None:
        guild_id, channel_id = game["guild_id"], game["channel_id"]
        idx = pick_next_call(len(game["words"]), game["called"])
        if idx is None:
            game["status"] = STATUS_EXHAUSTED
            game["next_call_at"] = 0.0
            game["finished_at"] = _utc_now()
            self._set_game(guild_id, channel_id, game)
            await self._finalize(game)
            return None
        game["called"].append(idx)
        if game["status"] == STATUS_RUNNING:
            game["next_call_at"] = time.time() + game["interval"]
        # Saved before announcing, so a restart can never "un-call" a word.
        self._set_game(guild_id, channel_id, game)
        word = self._word(game, idx)

        if not await self._refresh_main(game):
            # The game message was deleted, so nobody can reach the controls any more.
            game["status"] = STATUS_ENDED
            game["next_call_at"] = 0.0
            game["announced"] = True
            self._set_game(guild_id, channel_id, game)
            await self._finalize(game)
            return word

        channel = await self._channel(channel_id)
        if channel is not None:
            await self._delete_ticker(game)
            try:
                msg = await channel.send(
                    f"## 📣 {word}\n-# Bingo call #{len(game['called'])} of {len(game['words'])}. "
                    f"Mark it if it's on your card. [Game]({_jump(guild_id, channel_id, game['message_id'])})",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                game["call_message_id"] = int(msg.id)
                self._set_game(guild_id, channel_id, game)
            except Exception as exc:
                warn(f"bingo: posting call failed: {exc!r}")
        return word

    async def _finalize(self, game: dict[str, Any]) -> None:
        """Record, announce and remove a finished game. Safe to repeat after a restart."""
        guild_id, channel_id = game["guild_id"], game["channel_id"]
        if game["status"] == STATUS_WON and not game.get("recorded"):
            try:
                record_solo_result(
                    guild_id,
                    GAME_KEY,
                    int(game["winner_id"]),
                    event_id=f"bingo:{guild_id}:{game['message_id']}",
                    label=GAME_LABEL,
                    result_word="win",
                )
            except Exception as exc:
                warn(f"bingo stats update failed: {exc!r}")
            game["recorded"] = True
            self._set_game(guild_id, channel_id, game)

        if not game.get("announced"):
            await self._refresh_main(game)
            await self._delete_ticker(game)
            channel = await self._channel(channel_id)
            if channel is not None and game["status"] in (STATUS_WON, STATUS_EXHAUSTED):
                try:
                    if game["status"] == STATUS_WON:
                        await channel.send(
                            f"🎉 **BINGO!** <@{game['winner_id']}> wins WoS Bingo after "
                            f"{len(game['called'])} calls! "
                            f"[Game]({_jump(guild_id, channel_id, game['message_id'])})",
                            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                        )
                    else:
                        await channel.send(
                            "🎱 Every Bingo word has been called and nobody claimed. No winner this time.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                except Exception as exc:
                    warn(f"bingo: result post failed: {exc!r}")
            game["announced"] = True
            self._set_game(guild_id, channel_id, game)

        self._remove_game(guild_id, channel_id)

    # ---------- background calling ----------

    async def tick(self) -> None:
        now = time.time()
        for guild_id, channel_id in list(self._active):
            lock = self._lock_for(guild_id, channel_id)
            if lock.locked():
                continue  # a player/host action is in progress; try next tick
            try:
                async with lock:
                    game = self._get_game(guild_id, channel_id)
                    if game is None:
                        self._active.discard((guild_id, channel_id))
                        continue
                    if game["status"] in FINISHED_STATUSES:
                        await self._finalize(game)  # recover a finish interrupted by a restart
                    elif game["status"] == STATUS_RUNNING and float(game["next_call_at"] or 0) <= now:
                        await self._call_next(game)
            except Exception as exc:
                warn(f"bingo: tick failed for {guild_id}/{channel_id}: {exc!r}")

    async def run_forever(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except Exception:
            pass
        while True:
            await self.tick()
            await asyncio.sleep(TICK_SECONDS)


class BingoCog(commands.Cog):
    GAME_META = {
        "key": GAME_KEY,
        "label": GAME_LABEL,
        "kind": "solo",
        "result_word": "win",
        "description": "WoS word bingo for the whole channel with private cards",
        "emoji": "🎱",
        "requires_opponent": False,
    }

    HELP_META = {
        "title": "WoS Bingo",
        "summary": "Channel-wide bingo with WoS words, private cards and host-controlled calling.",
        "goal": HOW_TO_PLAY_GOAL,
        "how_to_play": HOW_TO_PLAY_STEPS,
        "rules": HOW_TO_PLAY_RULES,
        "details": HOW_TO_PLAY_COMMANDS,
    }

    def __init__(self, bot: commands.Bot, service: BingoService) -> None:
        self.bot = bot
        self.service = service
        self._task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self.service.load_active_index()
        self._task = asyncio.create_task(self.service.run_forever())

    async def cog_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()

    @app_commands.command(name="bingo", description="Start a WoS Bingo game in this channel")
    @app_commands.describe(mode="What wins: one line (default) or the full card")
    @app_commands.choices(mode=[
        app_commands.Choice(name="One line", value=MODE_LINE),
        app_commands.Choice(name="Full card", value=MODE_FULL),
    ])
    async def bingo(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        log_cmd("bingo", interaction)
        issue = self.service.start_issue(interaction.guild_id, interaction.channel_id)
        if issue:
            await interaction.response.send_message(issue, ephemeral=True)
            return
        if not await ensure_deferred(interaction, ephemeral=False):
            return
        await self.service.start_game(interaction, mode=mode.value if mode else MODE_LINE)

    @commands.command(name="bingo")
    async def bingo_prefix(self, ctx: commands.Context, action: str = "", *, arg: str = "") -> None:
        """!bingo [start [full] | go | pause | call | speed <s> | end | help]"""
        if ctx.guild is None:
            await ctx.reply("Bingo only works in a server channel.")
            return
        service = self.service
        guild_id, channel_id = ctx.guild.id, ctx.channel.id
        action = (action or "").strip().lower()

        if action in ("help", "rules", "how"):
            await ctx.reply(embed=build_help_embed(), mention_author=False)
            return

        if action in ("start", "new", "play"):
            issue = await service.start_from_context(ctx, arg)
            if issue:
                await ctx.reply(issue, mention_author=False)
            return

        game = service._get_game(guild_id, channel_id)
        if action in ("", "status", "card"):
            if not game or game["status"] not in LIVE_STATUSES:
                await ctx.reply(
                    "No Bingo game in this channel. Start one with `!bingo start` "
                    "(or `!bingo start full`). `!bingo help` explains how to play.",
                    mention_author=False,
                )
                return
            state = {
                STATUS_LOBBY: "waiting to start",
                STATUS_RUNNING: f"calling every {game['interval']}s",
                STATUS_PAUSED: "paused",
            }.get(game["status"], game["status"])
            await ctx.reply(
                f"🎱 Bingo is **{state}**. {len(game['players'])} player(s), "
                f"{len(game['called'])}/{len(game['words'])} calls. "
                f"Cards are private, so press **🎟️ My Card** here: "
                f"{_jump(guild_id, channel_id, game['message_id'])}",
                mention_author=False,
            )
            return

        if action in ("call", "next"):
            text = await service.action_call(guild_id, channel_id, ctx.author)
        elif action in ("go", "resume", "auto", "begin", "unpause"):
            text = await service.action_auto(guild_id, channel_id, ctx.author, "resume")
        elif action in ("pause", "stop", "hold"):
            text = await service.action_auto(guild_id, channel_id, ctx.author, "pause")
        elif action in ("speed", "interval", "timer"):
            text = await service.action_speed(guild_id, channel_id, ctx.author, arg)
        elif action in ("end", "quit", "cancel"):
            text = await service.action_end(guild_id, channel_id, ctx.author)
        else:
            text = "Unknown Bingo command. Try `!bingo help`."
        await ctx.reply(text, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    # Make "bingo" a known game straight away so channel settings and the
    # leaderboard list it even before anyone opens /games.
    try:
        from core.game_stats import register_game

        register_game(GAME_KEY, label=GAME_LABEL, kind="solo", result_word="win")
    except Exception as exc:
        warn(f"bingo: register_game failed: {exc!r}")

    service = BingoService(bot)
    # Persistent public controls and private card squares survive restarts.
    bot.add_view(BingoView(service))
    bot.add_dynamic_items(BingoSquare)

    cog = BingoCog(bot, service)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
