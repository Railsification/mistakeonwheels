# cogs/games/games.py
from __future__ import annotations

__version__ = "1.1.0"

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.game_registry import discover_games, get_game_entry
from core.game_stats import get_game_catalog
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.utils import ensure_deferred


# Kept for compatibility with any existing imports. These are refreshed from
# the shared registry whenever /games is opened.
GAMES: list[tuple[str, str]] = []
TWO_PLAYER_GAMES: set[str] = set()


# Built-in rules keep every currently shipped game documented even while older
# cogs are gradually moved to the structured HELP_META standard. A game's own
# HELP_META goal/how_to_play/rules values always take priority over these.
GAME_RULES: dict[str, dict[str, str]] = {
    "tictactoe": {
        "goal": "Be the first player to place 3 of your marks in a straight line.",
        "how_to_play": (
            "Players alternate pressing an empty square on the 3×3 board. Your mark is "
            "placed in that square and the turn passes to the other player. A line can be "
            "horizontal, vertical or diagonal."
        ),
        "rules": (
            "A player wins immediately when they make 3 in a row. If all 9 squares fill "
            "without a winning line, the game is a draw. You cannot play an occupied square "
            "or move when it is not your turn. Computer games are practice and do not "
            "change the leaderboard."
        ),
    },
    "connect4": {
        "goal": "Be the first player to connect 4 of your discs in a row.",
        "how_to_play": (
            "Take turns choosing one of the 7 columns. Your disc falls to the lowest empty "
            "space in that column. Build a line of four while blocking your opponent."
        ),
        "rules": (
            "Four connected discs can be horizontal, vertical or diagonal. Full columns "
            "cannot be chosen. If the board fills before either player connects four, the "
            "game is a draw. Computer games are practice and do not change the leaderboard."
        ),
    },
    "chess": {
        "goal": "Checkmate the opponent's king.",
        "how_to_play": (
            "Press **Move**, choose one of your movable pieces, then choose one of its legal "
            "destination squares. Pieces use normal chess movement. The bot handles legal "
            "moves, captures, check, castling, en passant and pawn promotion."
        ),
        "rules": (
            "You may not make a move that leaves your own king in check. The game ends when "
            "a king is checkmated or when the implemented draw condition is reached. Use "
            "Resign only when you intend to concede. Computer games are practice and do not "
            "change the leaderboard."
        ),
    },
    "checkers": {
        "goal": "Capture all opposing pieces or leave the opponent with no legal move.",
        "how_to_play": (
            "Press **Move**, choose one of your movable pieces, then choose a legal landing "
            "square. Men move diagonally forward. Jump over an adjacent enemy piece into the "
            "empty square beyond it to capture. Kings can move and capture both directions."
        ),
        "rules": (
            "Captures are compulsory when available. If another capture is available after a "
            "jump, the multi-jump continues. A man reaching the far back row is crowned as a "
            "king. The game uses English/American Checkers rules. Computer games are practice "
            "and do not change the leaderboard."
        ),
    },
    "othello": {
        "goal": "Finish the game with more discs of your colour than your opponent.",
        "how_to_play": (
            "Choose one of the legal moves marked on the 8×8 board. A legal placement must "
            "trap one or more opposing discs in a straight line between the new disc and one "
            "of your existing discs. Every trapped disc flips to your colour."
        ),
        "rules": (
            "Only legal trapping moves may be played. If a player has no legal move, their "
            "turn is passed. The game ends when neither player can move or the board is full; "
            "the player with the most discs wins. Computer games are practice and do not "
            "change the leaderboard."
        ),
    },
    "hangman": {
        "goal": "Solve the hidden word or phrase before the group reaches 7 misses.",
        "how_to_play": (
            "Press **Guess Letter** to try one letter or **Guess Word** to attempt the full "
            "answer. Correct letters are revealed to everyone. The first player to complete "
            "the answer gets the solve."
        ),
        "rules": (
            "A wrong letter costs 1 miss and a wrong full answer costs 1 miss. Repeating an "
            "already-tried guess does not cost another miss. At 7 misses the answer is "
            "revealed and the game ends. Anyone can participate in the channel-wide game."
        ),
    },
    "rebus": {
        "goal": "Solve the phrase or saying represented by the puzzle's visual layout.",
        "how_to_play": (
            "Press **Answer** to submit a guess, **Hint** to reveal the next public hint, or "
            "**Skip** if the group wants to abandon the puzzle. Look at position, spacing, "
            "repetition, numbers, missing letters and word order for clues."
        ),
        "rules": (
            "The first accepted answer wins the solve. Small typos and equivalent number "
            "wording may be accepted. The starter or a moderator can skip immediately; other "
            "players need 3 skip votes. Guesses, hints and skip votes are shared by the channel."
        ),
    },
    "dice": {
        "goal": "Roll higher than your opponent.",
        "how_to_play": (
            "Each player presses **Roll Dice** once. Your roll is locked in and kept hidden "
            "until both players have rolled. In Computer mode, the computer rolls when you do."
        ),
        "rules": (
            "The highest die wins. If both dice tie, both sides automatically reroll until "
            "there is a winner. A player cannot reroll a locked die. Computer games are "
            "practice and do not change the leaderboard."
        ),
    },
    "rps": {
        "goal": "Choose the option that defeats your opponent's choice.",
        "how_to_play": (
            "Each player privately locks in **Rock**, **Paper** or **Scissors**. Choices stay "
            "hidden until both players have selected. In Computer mode, the bot chooses at "
            "the same time as you."
        ),
        "rules": (
            "Rock beats Scissors, Scissors beats Paper, and Paper beats Rock. Matching choices "
            "are a draw. Once a choice is locked it cannot be changed. Computer games are "
            "practice and do not change the leaderboard."
        ),
    },
    "headsortails": {
        "goal": "Correctly predict which side of the coin will land face-up.",
        "how_to_play": (
            "Start the game and press **Heads** or **Tails**. The bot immediately flips the "
            "coin and reveals the result in the game message."
        ),
        "rules": (
            "You get one call per flip. Matching the result wins the flip; the other side "
            "loses. The result is generated randomly when your choice is made."
        ),
    },
    "yahtzee": {
        "goal": "Finish 13 scoring rounds with the highest total score.",
        "how_to_play": (
            "On your turn, roll five dice up to 3 times. Hold any dice you want to keep between "
            "rolls, then select one unused scoring category. Each category can be scored only "
            "once, even when the roll scores zero."
        ),
        "rules": (
            "The scorecard uses Ones through Sixes, 3/4 of a Kind, Full House, Small Straight, "
            "Large Straight, Yahtzee and Chance. An upper-section subtotal of at least 63 earns "
            "a 35-point bonus. After all 13 categories are filled, the higher total wins. "
            "Computer games are practice and do not change the leaderboard."
        ),
    },
    "battleships": {
        "goal": "Sink every ship in the opposing fleet before your own fleet is destroyed.",
        "how_to_play": (
            "Set up your private 10×10 fleet, then take turns firing at enemy coordinates. "
            "The public game shows hits, misses, sunk ships and whose turn it is while each "
            "player's own ship locations remain private."
        ),
        "rules": (
            "The fleet contains Carrier (5), Battleship (4), Cruiser (3), Submarine (3) and "
            "Destroyer (2). Ships cannot overlap. A shot can target each coordinate only once. "
            "A ship sinks when all of its cells have been hit; sinking the entire enemy fleet "
            "wins. Computer games are practice and do not change the leaderboard."
        ),
    },
}


def _help_meta_for_entry(entry: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    cog = entry.get("cog")
    raw = getattr(cog, "HELP_META", None)
    if callable(raw):
        try:
            raw = raw()
        except Exception:
            raw = None
    return dict(raw) if isinstance(raw, dict) else {}


def _rules_for_game(game_key: str, entry: dict[str, Any]) -> dict[str, str]:
    key = str(game_key or "").strip().lower()
    meta = _help_meta_for_entry(entry)
    built_in = GAME_RULES.get(key, {})

    goal = str(
        meta.get("goal")
        or built_in.get("goal")
        or entry.get("description")
        or "Play the game and complete its objective."
    ).strip()
    how_to_play = str(
        meta.get("how_to_play")
        or built_in.get("how_to_play")
        or meta.get("details")
        or "Use the controls shown on the game message."
    ).strip()
    rules_text = str(
        meta.get("rules")
        or built_in.get("rules")
        or "Follow the legal moves and controls enforced by the game."
    ).strip()

    return {
        "goal": goal[:1024],
        "how_to_play": how_to_play[:1024],
        "rules": rules_text[:1024],
    }


def build_how_to_play_embed(game_key: str, entry: dict[str, Any]) -> discord.Embed:
    label = str(entry.get("label") or game_label(game_key))[:100]
    help_text = _rules_for_game(game_key, entry)
    embed = discord.Embed(
        title=f"📖 {label} — How to Play",
        description=help_text["goal"],
        colour=discord.Colour.blurple(),
    )
    embed.add_field(
        name="How to Play",
        value=help_text["how_to_play"],
        inline=False,
    )
    embed.add_field(
        name="Rules",
        value=help_text["rules"],
        inline=False,
    )
    return embed


def _refresh_compatibility_constants(bot: commands.Bot) -> dict[str, dict[str, Any]]:
    registry = discover_games(bot)
    GAMES[:] = [
        (str(details["label"]), key)
        for key, details in registry.items()
    ]
    TWO_PLAYER_GAMES.clear()
    TWO_PLAYER_GAMES.update(
        key
        for key, details in registry.items()
        if bool(details.get("requires_opponent"))
    )
    return registry


def game_label(key: str | None) -> str:
    if not key:
        return "None"

    wanted = str(key).strip().lower()
    for name, game_key in GAMES:
        if game_key == wanted:
            return name

    details = get_game_catalog().get(wanted)
    if isinstance(details, dict):
        return str(details.get("label") or wanted)
    return wanted


def _computer_launcher(entry: dict[str, Any] | None):
    if not isinstance(entry, dict):
        return None

    cog = entry.get("cog")
    launcher = getattr(cog, "start_computer_game", None)
    if callable(launcher):
        return launcher

    service = getattr(cog, "service", None)
    launcher = getattr(service, "start_computer_game", None)
    if callable(launcher):
        return launcher

    return None


def _supports_computer(entry: dict[str, Any] | None) -> bool:
    return callable(_computer_launcher(entry))


class GameSelect(discord.ui.Select):
    def __init__(
        self,
        bot: commands.Bot,
        allowed_keys: set[str] | None = None,
    ):
        registry = _refresh_compatibility_constants(bot)
        if allowed_keys is not None:
            registry = {
                key: details
                for key, details in registry.items()
                if key in allowed_keys
            }
        options: list[discord.SelectOption] = []

        for key, details in registry.items():
            options.append(
                discord.SelectOption(
                    label=str(details["label"])[:100],
                    value=key,
                    description=str(details.get("description") or "Play this game")[:100],
                    emoji=str(details.get("emoji") or "") or None,
                )
            )
            if len(options) >= 25:
                break

        if not options:
            options.append(
                discord.SelectOption(
                    label="No games loaded",
                    value="__none__",
                    description="No playable game cogs were discovered.",
                )
            )

        super().__init__(
            placeholder="Choose a game...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        selected = self.values[0]
        view.selected_game = None if selected == "__none__" else selected
        view.opponent_id = None
        view.computer_mode = False
        view.sync_controls()
        await view.refresh(interaction)


class OpponentSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Pick an opponent after choosing a two-player game...",
            min_values=1,
            max_values=1,
            row=1,
            disabled=True,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        view.opponent_id = self.values[0].id
        view.computer_mode = False
        view.sync_controls()
        await view.refresh(interaction)


class ComputerButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Computer",
            emoji="🤖",
            style=discord.ButtonStyle.primary,
            row=2,
            disabled=True,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)

        entry = view.selected_entry()
        if not entry or not bool(entry.get("requires_opponent")):
            await interaction.followup.send(
                "❌ Choose a two-player game first.",
                ephemeral=True,
            )
            return

        if not _supports_computer(entry):
            await interaction.followup.send(
                "❌ Computer mode is not available for that game yet.",
                ephemeral=True,
            )
            return

        view.computer_mode = not view.computer_mode
        if view.computer_mode:
            view.opponent_id = None
        view.sync_controls()
        await view.refresh(interaction)


class HowToPlayButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="How to Play",
            emoji="📖",
            style=discord.ButtonStyle.secondary,
            row=2,
            disabled=True,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GamesView = self.view  # type: ignore[assignment]
        entry = view.selected_entry()
        if not view.selected_game or entry is None:
            await interaction.response.send_message(
                "❌ Choose a game first.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=build_how_to_play_embed(view.selected_game, entry),
            ephemeral=True,
        )


class GamesView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        author_id: int,
        guild_id: int | None = None,
        channel_id: int | None = None,
    ):
        # The private picker expires; started games control their own lifetime.
        super().__init__(timeout=300)
        self.bot = bot
        self.author_id = int(author_id)
        self.guild_id = int(guild_id) if guild_id is not None else None
        self.channel_id = int(channel_id) if channel_id is not None else None
        self.selected_game: str | None = None
        self.opponent_id: int | None = None
        self.computer_mode = False

        all_games = _refresh_compatibility_constants(bot)
        settings = getattr(bot, "settings", None)
        if settings is not None and self.guild_id is not None and self.channel_id is not None:
            allowed_keys = set(settings.available_games(self.guild_id, self.channel_id))
            self.registry = {
                key: details
                for key, details in all_games.items()
                if key in allowed_keys
            }
        else:
            allowed_keys = set(all_games)
            self.registry = all_games

        self.add_item(GameSelect(bot, allowed_keys))
        self.add_item(OpponentSelect())
        self.add_item(ComputerButton())
        self.add_item(HowToPlayButton())
        self.add_item(StartButton())
        self.add_item(CloseButton())
        self.sync_controls()

    def selected_entry(self) -> dict[str, Any] | None:
        if not self.selected_game or self.selected_game not in self.registry:
            return None
        entry = get_game_entry(self.bot, self.selected_game, self.guild_id)
        if entry is not None:
            self.registry[self.selected_game] = entry
        return entry

    def sync_controls(self) -> None:
        entry = self.registry.get(str(self.selected_game or ""))
        needs_opponent = bool(entry and entry.get("requires_opponent"))
        supports_computer = needs_opponent and _supports_computer(entry)

        if self.computer_mode and not supports_computer:
            self.computer_mode = False

        for child in self.children:
            if isinstance(child, OpponentSelect):
                child.disabled = not needs_opponent or self.computer_mode
                if not needs_opponent:
                    child.placeholder = "No opponent needed for this game"
                elif self.computer_mode:
                    child.placeholder = "Computer opponent selected"
                else:
                    child.placeholder = "Pick an opponent..."

            elif isinstance(child, ComputerButton):
                child.disabled = not supports_computer
                child.style = (
                    discord.ButtonStyle.success
                    if self.computer_mode
                    else discord.ButtonStyle.primary
                )
                child.label = "Computer ✓" if self.computer_mode else "Computer"

            elif isinstance(child, HowToPlayButton):
                child.disabled = entry is None

        if not needs_opponent:
            self.opponent_id = None
            self.computer_mode = False

    def disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    def render_content(self) -> str:
        entry = self.registry.get(str(self.selected_game or ""))
        if entry is None:
            return (
                "🎮 **Games Menu**\n"
                "Choose a game from the dropdown. The list is loaded automatically "
                "from the installed game cogs."
            )

        label = str(entry.get("label") or game_label(self.selected_game))
        if bool(entry.get("requires_opponent")):
            supports_computer = _supports_computer(entry)
            if self.computer_mode and supports_computer:
                ready = True
                opponent_line = "Opponent: 🤖 **Computer**"
                instruction = "ready."
            else:
                opponent = f"<@{self.opponent_id}>" if self.opponent_id else "_(none)_"
                ready = self.opponent_id is not None
                opponent_line = f"Opponent: {opponent}"
                if ready:
                    instruction = "ready."
                elif supports_computer:
                    instruction = "pick a player or choose **Computer**."
                else:
                    instruction = "pick an opponent."
        else:
            ready = True
            opponent_line = "Opponent: _not needed — the whole channel can play_"
            instruction = "ready."

        return (
            "🎮 **Games Menu**\n"
            "Pick a game, use **How to Play** for the rules, choose who you want to play, "
            "then press **Start**.\n"
            f"{opponent_line}\n\n"
            f"{'✅' if ready else '❌'} **{label}** — {instruction}"
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.edit_original_response(
                content=self.render_content(),
                view=self,
            )
        except discord.NotFound:
            return

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            try:
                await interaction.response.send_message(
                    "❌ This menu isn’t yours.",
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                await interaction.followup.send(
                    "❌ This menu isn’t yours.",
                    ephemeral=True,
                )
            return False
        return True


class StartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Start", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)

        if not view.selected_game:
            await interaction.followup.send("❌ Pick a game first.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.followup.send(
                "❌ This must be used in a server.",
                ephemeral=True,
            )
            return

        entry = view.selected_entry()
        if entry is None:
            await interaction.followup.send(
                "❌ That game cog is no longer loaded.",
                ephemeral=True,
            )
            return

        launcher = entry.get("launcher")
        if not callable(launcher):
            await interaction.followup.send(
                "❌ That game has no valid start method.",
                ephemeral=True,
            )
            return

        opponent: discord.Member | None = None
        computer_launcher = None

        if bool(entry.get("requires_opponent")):
            if view.computer_mode:
                computer_launcher = _computer_launcher(entry)
                if not callable(computer_launcher):
                    await interaction.followup.send(
                        "❌ Computer mode is not available for that game yet.",
                        ephemeral=True,
                    )
                    return
            else:
                if not view.opponent_id:
                    await interaction.followup.send(
                        "❌ Pick an opponent or choose Computer first.",
                        ephemeral=True,
                    )
                    return

                opponent = interaction.guild.get_member(view.opponent_id)
                if opponent is None:
                    try:
                        opponent = await interaction.guild.fetch_member(view.opponent_id)
                    except Exception:
                        opponent = None
                if opponent is None:
                    await interaction.followup.send(
                        "❌ Couldn’t resolve that opponent.",
                        ephemeral=True,
                    )
                    return

        view.disable_all()
        await view.refresh(interaction)

        try:
            if callable(computer_launcher):
                await computer_launcher(interaction)
            elif bool(entry.get("requires_opponent")):
                await launcher(interaction, opponent)
            else:
                await launcher(interaction)
        except Exception as exc:
            warn(
                f"Dynamic game launch failed for {view.selected_game}: {exc!r}"
            )
            await interaction.followup.send(
                "❌ The game failed to start. Check the Railway log.",
                ephemeral=True,
            )


class CloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Close", style=discord.ButtonStyle.danger, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: GamesView = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        view.disable_all()
        await view.refresh(interaction)


class GamesCog(commands.Cog):
    HELP_META = {
        "title": "Games Menu",
        "summary": "Automatically lists every loaded game cog and exposes each game's rules.",
        "goal": "Choose a game, check its rules, choose an opponent when needed, and start it.",
        "how_to_play": (
            "Use `/games`, select a game, press **How to Play** whenever you want its rules, "
            "then choose another player or **Computer** when supported and press **Start**."
        ),
        "rules": (
            "Only games enabled for the current channel are shown. The private launcher menu "
            "expires after 5 minutes; once a game starts, that game's own persistence and "
            "timeout rules apply."
        ),
        "details": (
            "Use `/games`, choose a game, read **How to Play**, then pick another player or "
            "Computer when the selected game supports solo play."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings

    @app_commands.command(
        name="games",
        description="Open the automatically generated games menu",
    )
    async def games(self, interaction: discord.Interaction) -> None:
        log_cmd("games", interaction)
        if not self.settings.is_games_menu_allowed(
            interaction.guild_id,
            interaction.channel_id,
        ):
            await interaction.response.send_message(
                "❌ `/games` can only be used in a configured games/game channel.",
                ephemeral=True,
            )
            return

        await ensure_deferred(interaction, ephemeral=True)
        view = GamesView(
            self.bot,
            author_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
        )
        await interaction.followup.send(
            content=view.render_content(),
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    from core.command_scope import bind_public_cog

    cog = GamesCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
