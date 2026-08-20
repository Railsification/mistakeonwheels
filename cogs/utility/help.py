from __future__ import annotations

import difflib
import re
from typing import Optional, Iterable, Any

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.logger import log_cmd
from core.utils import ensure_deferred


UTILITY_COMMANDS = {"hello", "acktest"}
ADMIN_COMMANDS = {"council"}

COG_INFO: dict[str, dict[str, Any]] = {
    "AdminCog": {
        "title": "Council / Admin",
        "summary": "Admin/test-server controls for sync, setup, feature channels, and maintenance.",
        "aliases": {"admin", "council", "setup", "sync"},
    },
    "CanyonCog": {
        "title": "Canyon",
        "summary": "WoS Canyon scanning, lane lists, row setup, and balancing helpers.",
        "aliases": {"canyon", "lane", "lanes"},
    },
    "ChestPatternCog": {
        "title": "Chest Pattern",
        "summary": "Tracks WoS chest/puzzle results and suggests better opening order over time.",
        "aliases": {"chest", "pattern", "chests"},
    },
    "Connect4Cog": {
        "title": "Connect 4",
        "summary": "Connect 4 game commands.",
        "aliases": {"connect4", "connect 4"},
    },
    "GamesCog": {
        "title": "Games",
        "summary": "Game launcher and small Discord games.",
        "aliases": {"games", "game"},
    },
    "HelpCog": {
        "title": "Help",
        "summary": "Shows all command groups and every loaded cog, with detailed command/cog help.",
        "aliases": {"help"},
    },
    "ImagesCog": {
        "title": "Images / Profiles",
        "summary": "Profile image storage, member image tagging, and image helper commands.",
        "aliases": {"images", "profiles", "profile", "tag"},
    },
    "JoinsCog": {
        "title": "Join Facts",
        "summary": "Posts join facts/topics when members join configured channels.",
        "aliases": {"join", "joins", "facts"},
    },
    "MiscCog": {
        "title": "Misc",
        "summary": "Small utility commands such as sanity checks and facts.",
        "aliases": {"misc", "fact", "hello"},
    },
    "PfpCog": {
        "title": "PFP Theme",
        "summary": "WoS profile-picture theme prompts and current theme helpers.",
        "aliases": {"pfp", "theme", "profile picture"},
    },
    "Polls": {
        "title": "Image Polls",
        "summary": "Image polls with votes, timers, refresh, cancel, and result handling.",
        "aliases": {"poll", "polls", "image poll"},
    },
    "SpeechCog": {
        "title": "Speech Convert",
        "summary": "Per-server speech style conversion in configured channels.",
        "aliases": {"speech", "speech convert", "text convert"},
    },
    "SuggestionPollCog": {
        "title": "Suggestion Polls",
        "summary": "Collect WoS PFP ideas, let members add options, vote, and produce a winner/shortlist.",
        "aliases": {"suggestion", "suggestions", "idea", "ideas"},
    },
    "TicTacToeCog": {
        "title": "Tic Tac Toe",
        "summary": "Tic Tac Toe game commands.",
        "aliases": {"tictactoe", "tic tac toe", "noughts"},
    },
    "WOSFurnaceCalculator": {
        "title": "WoS Furnace Calculator",
        "summary": "Fire Crystal/refine planning, furnace profile saving, and upgrade forecasts.",
        "aliases": {"furnace", "furance", "forge", "fire crystal", "refines"},
    },
}

COG_COMMAND_ALIASES = {
    "AdminCog": {"council"},
    "HelpCog": {"help"},
    "SuggestionPollCog": {"suggestion"},
    "WOSFurnaceCalculator": {
        "furnace_set",
        "furnace_view",
        "furnace_help",
        "furnace_refines_needed",
        "furnace_upgrade_forecast",
    },
}

INPUT_ALIASES = {
    "furance": "furnace",
    "furancecalculator": "furnace",
    "wosfurnace": "furnace",
    "suggestions": "suggestion",
    "suggestionpoll": "suggestion",
    "suggestionpolls": "suggestion",
    "imagepoll": "image_poll",
    "polls": "poll",
    "speechconvert": "speech",
    "pfptheme": "pfp",
    "chestpattern": "chest",
    "tic tac toe": "tictactoe",
    "connect 4": "connect4",
}

MAIN_ORDER = [
    "help",
    "image_poll",
    "suggestion",
    "pfp",
    "pfp_theme",
    "furnace_set",
    "furnace_view",
    "furnace_refines_needed",
    "furnace_upgrade_forecast",
    "speech_convert",
    "speech_lookup",
    "canyon_scan",
    "canyon_list",
    "games",
    "connect4",
    "tictactoe",
    "fact",
    "council",
]


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _canonical_input(value: str) -> str:
    raw = (value or "").strip().lower().lstrip("/")
    norm = _normalise(raw)
    if raw in INPUT_ALIASES:
        return INPUT_ALIASES[raw]
    if norm in INPUT_ALIASES:
        return INPUT_ALIASES[norm]
    return raw


def _split_camel(value: str) -> str:
    value = value.replace("Cog", "").strip()
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    value = value.replace("W O S", "WoS").replace("Pfp", "PFP")
    return value.strip() or "Cog"


def _chunks(lines: Iterable[str], limit: int = 950) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for line in lines:
        add_size = len(line) + 1
        if current and size + add_size > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += add_size

    if current:
        chunks.append("\n".join(current))

    return chunks


class HelpCog(commands.Cog):
    HELP_META = {
        "title": "Help",
        "summary": "Shows main commands, every loaded cog, and detailed command/cog help.",
        "details": "Use `/help` for the overview or `/help command:<name>` for a command group or cog.",
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- guild/visibility helpers ----------

    def _admin_guild_id(self) -> int:
        cfg = getattr(self.bot, "hot_config", {}) or {}
        return int(cfg.get("admin_guild_id") or cfg.get("guild_id") or 0)

    def _is_admin_guild(self, guild_id: int | None) -> bool:
        return bool(guild_id and guild_id == self._admin_guild_id())

    def _visible_in_guild(self, command: Any, guild_id: int) -> bool:
        gids = getattr(command, "_guild_ids", None)
        if gids is None:
            # This should not happen in v1.7.3, but treat global commands as visible
            # so /help can still expose accidental registrations during testing.
            return True
        return guild_id in set(int(x) for x in gids)

    def _visible_top_commands(self, guild_id: int, *, include_admin: bool | None = None) -> list[Any]:
        if include_admin is None:
            include_admin = self._is_admin_guild(guild_id)

        out = []
        for command in self.bot.tree.get_commands():
            if not self._visible_in_guild(command, guild_id):
                continue
            if command.name in ADMIN_COMMANDS and not include_admin:
                continue
            out.append(command)
        return sorted(out, key=self._command_sort_key)

    def _command_sort_key(self, command: Any) -> tuple[int, str]:
        name = getattr(command, "name", "")
        try:
            index = MAIN_ORDER.index(name)
        except ValueError:
            index = 999
        return (index, name.lower())

    def _find_command(self, guild_id: int, wanted: str):
        wanted = _canonical_input(wanted)
        if not wanted:
            return None

        parts = wanted.split()
        top = parts[0]

        for command in self._visible_top_commands(guild_id):
            if command.name.lower() != top:
                continue
            current = command
            for part in parts[1:]:
                children = getattr(current, "commands", []) or []
                current = next((c for c in children if c.name.lower() == part), None)
                if current is None:
                    return None
            return current

        # Also support old flat command names like furnace_set or fuzzy aliases like furnace.
        wanted_norm = _normalise(wanted)
        for command in self._visible_top_commands(guild_id):
            if _normalise(command.qualified_name) == wanted_norm:
                return command
            if _normalise(command.name) == wanted_norm:
                return command
            for child in getattr(command, "commands", []) or []:
                if _normalise(child.qualified_name) == wanted_norm:
                    return child

        # If user asks for a feature name, return the best matching command under that feature.
        alias_candidates = []
        for command in self._visible_top_commands(guild_id):
            alias_candidates.append(command.name)
            for child in getattr(command, "commands", []) or []:
                alias_candidates.append(child.qualified_name)
        best = difflib.get_close_matches(wanted_norm, [_normalise(x) for x in alias_candidates], n=1, cutoff=0.86)
        if best:
            for command in self._visible_top_commands(guild_id):
                if _normalise(command.name) == best[0] or _normalise(command.qualified_name) == best[0]:
                    return command
                for child in getattr(command, "commands", []) or []:
                    if _normalise(child.qualified_name) == best[0]:
                        return child

        return None

    # ---------- cog helpers ----------

    def _meta(self, cog: commands.Cog) -> dict[str, Any]:
        raw = getattr(cog, "HELP_META", None) or getattr(cog, "help_meta", None)
        if callable(raw):
            try:
                raw = raw()
            except Exception:
                raw = None
        return raw if isinstance(raw, dict) else {}

    def _cog_info(self, cog_name: str) -> dict[str, Any]:
        return COG_INFO.get(cog_name, {})

    def _cog_title(self, cog_name: str, cog: commands.Cog) -> str:
        meta = self._meta(cog)
        title = meta.get("title") or meta.get("name") or self._cog_info(cog_name).get("title")
        if title:
            return str(title)
        return _split_camel(cog_name)

    def _cog_summary(self, cog_name: str, cog: commands.Cog) -> str:
        meta = self._meta(cog)
        summary = meta.get("summary") or meta.get("description") or self._cog_info(cog_name).get("summary")
        if summary:
            return str(summary)

        listeners = len(cog.get_listeners())
        if listeners:
            return "Background/listener feature."
        return "Command module."

    def _cog_tokens(self, cog_name: str, cog: commands.Cog) -> set[str]:
        title = self._cog_title(cog_name, cog)
        base = cog_name.lower().replace("cog", "")
        split = _split_camel(cog_name).lower()
        aliases = set(self._cog_info(cog_name).get("aliases", set()))
        tokens = {_normalise(cog_name), _normalise(title), _normalise(base), _normalise(split)}
        tokens.update(_normalise(x) for x in aliases if x)
        tokens.update(_normalise(x) for x in re.split(r"\s+", split) if x)
        tokens.discard("")
        return tokens

    def _commands_for_cog(self, cog_name: str, cog: commands.Cog, guild_id: int) -> list[Any]:
        commands_for_guild = self._visible_top_commands(guild_id, include_admin=self._is_admin_guild(guild_id))
        result: list[Any] = []
        seen: set[str] = set()

        aliases = set(COG_COMMAND_ALIASES.get(cog_name, set()))
        tokens = self._cog_tokens(cog_name, cog)

        for command in commands_for_guild:
            belongs = False

            if getattr(command, "binding", None) is cog:
                belongs = True

            for cog_command in cog.get_app_commands():
                if getattr(cog_command, "qualified_name", None) == getattr(command, "qualified_name", None):
                    belongs = True
                    break
                if getattr(cog_command, "name", None) == getattr(command, "name", None):
                    belongs = True
                    break

            if command.name in aliases:
                belongs = True

            command_norm = _normalise(command.name)
            if command_norm in tokens:
                belongs = True
            else:
                for token in tokens:
                    if token and len(token) >= 4 and command_norm.startswith(token):
                        belongs = True
                        break

            if belongs:
                qn = getattr(command, "qualified_name", command.name)
                if qn not in seen:
                    result.append(command)
                    seen.add(qn)

        return sorted(result, key=self._command_sort_key)

    def _find_cog(self, guild_id: int, wanted: str):
        wanted = _canonical_input(wanted)
        needle = _normalise(wanted)
        if not needle:
            return None

        candidates: dict[str, tuple[str, commands.Cog]] = {}
        for cog_name, cog in self.bot.cogs.items():
            names = {
                _normalise(cog_name),
                _normalise(self._cog_title(cog_name, cog)),
                *self._cog_tokens(cog_name, cog),
            }
            aliases = {_normalise(x) for x in COG_COMMAND_ALIASES.get(cog_name, set())}
            names.update(aliases)

            for name in names:
                if name:
                    candidates[name] = (cog_name, cog)

            if needle in names:
                return cog_name, cog

            for command in self._commands_for_cog(cog_name, cog, guild_id):
                if needle == _normalise(command.name) or needle == _normalise(command.qualified_name):
                    return cog_name, cog
                if _normalise(command.name).startswith(needle) and len(needle) >= 4:
                    return cog_name, cog

        close = difflib.get_close_matches(needle, list(candidates.keys()), n=1, cutoff=0.82)
        if close:
            return candidates[close[0]]

        return None

    # ---------- formatting ----------

    def _command_signature(self, command: Any) -> str:
        params = []
        for param in getattr(command, "parameters", []) or []:
            name = getattr(param, "display_name", None) or getattr(param, "name", "param")
            required = getattr(param, "required", False)
            params.append(f"<{name}>" if required else f"[{name}]")
        return f"/{command.qualified_name} {' '.join(params)}".strip()

    def _subcommands(self, command: Any) -> list[Any]:
        return list(getattr(command, "commands", []) or [])

    def _short_command_line(self, command: Any) -> str:
        subs = self._subcommands(command)
        if subs:
            return f"`/{command.name}` — {command.description or 'Command group'} ({len(subs)} subcommands)"
        return f"`/{command.name}` — {command.description or 'No description'}"

    def _cog_line(self, cog_name: str, cog: commands.Cog, guild_id: int) -> str:
        title = self._cog_title(cog_name, cog)
        summary = self._cog_summary(cog_name, cog)

        if cog_name == "AdminCog" and not self._is_admin_guild(guild_id):
            return f"**{title}** — admin/test-server only; no public commands."

        commands_for_cog = self._commands_for_cog(cog_name, cog, guild_id)
        if commands_for_cog:
            names = []
            for command in commands_for_cog[:8]:
                subs = self._subcommands(command)
                suffix = f" +{len(subs)}" if subs else ""
                names.append(f"`/{command.name}`{suffix}")
            extra = len(commands_for_cog) - 8
            more = f" +{extra} more" if extra > 0 else ""
            return f"**{title}** — {summary} Commands: " + ", ".join(names) + more

        return f"**{title}** — {summary}"

    def _detailed_cog_embed(self, guild_id: int, cog_name: str, cog: commands.Cog) -> discord.Embed:
        title = self._cog_title(cog_name, cog)
        summary = self._cog_summary(cog_name, cog)
        embed = discord.Embed(
            title=f"Help: {title}",
            description=summary,
            colour=discord.Colour.blurple(),
        )

        if cog_name == "AdminCog" and not self._is_admin_guild(guild_id):
            embed.add_field(name="Commands", value="Admin/test-server only. Not available in this server.", inline=False)
            return embed

        commands_for_cog = self._commands_for_cog(cog_name, cog, guild_id)
        if commands_for_cog:
            lines: list[str] = []
            for command in commands_for_cog:
                lines.append(f"`{self._command_signature(command)}` — {command.description or 'No description'}")
                for sub in self._subcommands(command):
                    lines.append(f"  `/{sub.qualified_name}` — {sub.description or 'No description'}")
            for index, chunk in enumerate(_chunks(lines), start=1):
                name = "Commands" if index == 1 else f"Commands {index}"
                embed.add_field(name=name, value=chunk, inline=False)
        else:
            embed.add_field(name="Commands", value="No public slash commands for this cog.", inline=False)

        listeners = cog.get_listeners()
        if listeners:
            names = [f"`{name}`" for name, _ in listeners[:10]]
            more = f" +{len(listeners) - 10} more" if len(listeners) > 10 else ""
            embed.add_field(name="Background listeners", value=", ".join(names) + more, inline=False)

        details = self._meta(cog).get("details")
        if details:
            embed.add_field(name="Details", value=str(details)[:1024], inline=False)

        return embed

    def _not_found_message(self, guild_id: int, wanted: str) -> str:
        names: list[str] = []
        for command in self._visible_top_commands(guild_id):
            names.append(command.name)
            for sub in self._subcommands(command):
                names.append(sub.qualified_name)
        for cog_name, cog in self.bot.cogs.items():
            names.append(self._cog_title(cog_name, cog))
            names.extend(self._cog_info(cog_name).get("aliases", set()))

        wanted_norm = _normalise(wanted)
        choices = sorted(set(names))
        close = difflib.get_close_matches(wanted_norm, [_normalise(x) for x in choices], n=3, cutoff=0.65)
        suggestions = []
        for close_norm in close:
            for name in choices:
                if _normalise(name) == close_norm:
                    suggestions.append(name)
                    break
        if suggestions:
            return f"No command or cog found for `{wanted}`. Closest: " + ", ".join(f"`{x}`" for x in suggestions)
        return f"No command or loaded cog found for `{wanted}`."

    # ---------- slash command ----------

    @app_commands.command(name="help", description="Show every loaded cog, or details for one command/cog.")
    @app_commands.describe(command="Optional command or cog name, like suggestion, furnace, canyon, speech, or help")
    async def help_cmd(self, interaction: discord.Interaction, command: Optional[str] = None):
        log_cmd("help", interaction)
        await ensure_deferred(interaction, ephemeral=True)

        if interaction.guild_id is None:
            await interaction.followup.send("Use `/help` inside a server.", ephemeral=True)
            return

        guild_id = int(interaction.guild_id)

        if command:
            found_command = self._find_command(guild_id, command)
            if found_command is not None:
                embed = discord.Embed(
                    title=f"Help: /{found_command.qualified_name}",
                    description=found_command.description or "No description set.",
                    colour=discord.Colour.blurple(),
                )
                embed.add_field(name="Usage", value=f"`{self._command_signature(found_command)}`", inline=False)

                subs = self._subcommands(found_command)
                if subs:
                    lines = [
                        f"`/{sub.qualified_name}` — {sub.description or 'No description'}"
                        for sub in sorted(subs, key=lambda c: c.name.lower())
                    ]
                    for index, chunk in enumerate(_chunks(lines), start=1):
                        embed.add_field(name="Subcommands" if index == 1 else f"Subcommands {index}", value=chunk, inline=False)

                params = getattr(found_command, "parameters", []) or []
                if params:
                    lines = []
                    for param in params:
                        name = getattr(param, "display_name", None) or getattr(param, "name", "param")
                        desc = getattr(param, "description", None) or "No description"
                        required = "required" if getattr(param, "required", False) else "optional"
                        lines.append(f"`{name}` — {desc} ({required})")
                    for index, chunk in enumerate(_chunks(lines), start=1):
                        embed.add_field(name="Options" if index == 1 else f"Options {index}", value=chunk, inline=False)

                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            found_cog = self._find_cog(guild_id, command)
            if found_cog is not None:
                cog_name, cog = found_cog
                await interaction.followup.send(embed=self._detailed_cog_embed(guild_id, cog_name, cog), ephemeral=True)
                return

            await interaction.followup.send(self._not_found_message(guild_id, command), ephemeral=True)
            return

        visible_commands = self._visible_top_commands(guild_id)
        main = [c for c in visible_commands if c.name not in UTILITY_COMMANDS and c.name not in ADMIN_COMMANDS]
        utility = [c for c in visible_commands if c.name in UTILITY_COMMANDS]
        admin = [c for c in visible_commands if c.name in ADMIN_COMMANDS]

        embed = discord.Embed(
            title="HotBot Help",
            description="Main commands first. Then every loaded cog/feature. Use `/help command:<name>` for details.",
            colour=discord.Colour.blurple(),
        )

        if main:
            lines = [self._short_command_line(cmd) for cmd in main]
            for index, chunk in enumerate(_chunks(lines), start=1):
                embed.add_field(name="Main Commands" if index == 1 else f"Main Commands {index}", value=chunk, inline=False)
        else:
            embed.add_field(name="Main Commands", value="No public commands synced for this server yet.", inline=False)

        cog_lines = []
        for cog_name, cog in sorted(self.bot.cogs.items(), key=lambda item: self._cog_title(item[0], item[1]).lower()):
            cog_lines.append(self._cog_line(cog_name, cog, guild_id))

        for index, chunk in enumerate(_chunks(cog_lines), start=1):
            embed.add_field(name="Cogs / Features" if index == 1 else f"Cogs / Features {index}", value=chunk, inline=False)

        if utility:
            lines = [self._short_command_line(cmd) for cmd in utility]
            embed.add_field(name="Utility", value="\n".join(lines), inline=False)

        if admin and self._is_admin_guild(guild_id):
            lines = [self._short_command_line(cmd) for cmd in admin]
            embed.add_field(name="Admin/Test Commands", value="\n".join(lines), inline=False)

        embed.set_footer(text="New cogs appear automatically after load/sync. Detailed help accepts command names and cog names.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @help_cmd.autocomplete("command")
    async def help_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild_id is None:
            return []

        current_norm = _normalise(current or "")
        choices: list[app_commands.Choice[str]] = []
        seen: set[str] = set()

        def add_choice(name: str, value: str):
            if not name or value in seen:
                return
            if current_norm and current_norm not in _normalise(name) and current_norm not in _normalise(value):
                return
            seen.add(value)
            choices.append(app_commands.Choice(name=name[:100], value=value[:100]))

        guild_id = int(interaction.guild_id)
        for command in self._visible_top_commands(guild_id):
            add_choice(f"/{command.name}", command.name)
            for sub in self._subcommands(command):
                add_choice(f"/{sub.qualified_name}", sub.qualified_name)

        for cog_name, cog in sorted(self.bot.cogs.items(), key=lambda item: self._cog_title(item[0], item[1]).lower()):
            title = self._cog_title(cog_name, cog)
            add_choice(title, title.lower())
            for alias in self._cog_info(cog_name).get("aliases", set()):
                add_choice(str(alias), str(alias).lower())

        return choices[:25]


async def setup(bot: commands.Bot):
    cog = HelpCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
