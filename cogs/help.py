from __future__ import annotations

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

FRIENDLY_COG_NAMES = {
    "AdminCog": "Council / Admin",
    "CanyonCog": "Canyon",
    "ChestPatternCog": "Chest Pattern",
    "Connect4Cog": "Connect 4",
    "GamesCog": "Games",
    "HelpCog": "Help",
    "ImagesCog": "Images / Profiles",
    "JoinsCog": "Join Facts",
    "MiscCog": "Misc",
    "PfpCog": "PFP Theme",
    "Polls": "Image Polls",
    "SpeechCog": "Speech Convert",
    "SuggestionPollCog": "Suggestion Polls",
    "TicTacToeCog": "Tic Tac Toe",
    "WOSFurnaceCalculator": "WoS Furnace Calculator",
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
        wanted = wanted.strip().lower().lstrip("/")
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

        # Also support old flat command names like furnace_set.
        for command in self._visible_top_commands(guild_id):
            if command.qualified_name.lower() == wanted:
                return command
            for child in getattr(command, "commands", []) or []:
                if child.qualified_name.lower() == wanted:
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

    def _cog_title(self, cog_name: str, cog: commands.Cog) -> str:
        meta = self._meta(cog)
        title = meta.get("title") or meta.get("name")
        if title:
            return str(title)
        return FRIENDLY_COG_NAMES.get(cog_name) or _split_camel(cog_name)

    def _cog_summary(self, cog_name: str, cog: commands.Cog) -> str:
        meta = self._meta(cog)
        summary = meta.get("summary") or meta.get("description")
        if summary:
            return str(summary)

        listeners = len(cog.get_listeners())
        if listeners:
            return f"Loaded with {listeners} listener(s)."
        return "Loaded."

    def _cog_tokens(self, cog_name: str, cog: commands.Cog) -> set[str]:
        title = self._cog_title(cog_name, cog)
        base = cog_name.lower().replace("cog", "")
        split = _split_camel(cog_name).lower()
        tokens = {_normalise(cog_name), _normalise(title), _normalise(base), _normalise(split)}
        tokens.update(_normalise(x) for x in re.split(r"\s+", split) if x)
        tokens.discard("")
        return tokens

    def _commands_for_cog(self, cog_name: str, cog: commands.Cog, guild_id: int) -> list[Any]:
        commands_for_guild = self._visible_top_commands(guild_id, include_admin=True)
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

    def _cog_visible_in_guild(self, cog_name: str, cog: commands.Cog, guild_id: int) -> bool:
        if cog_name == "AdminCog" and not self._is_admin_guild(guild_id):
            return True  # show as loaded/admin-only, but do not expose /council details
        return True

    def _find_cog(self, guild_id: int, wanted: str):
        needle = _normalise(wanted.strip().lower().lstrip("/"))
        if not needle:
            return None

        for cog_name, cog in self.bot.cogs.items():
            if not self._cog_visible_in_guild(cog_name, cog, guild_id):
                continue

            names = {
                _normalise(cog_name),
                _normalise(self._cog_title(cog_name, cog)),
                *self._cog_tokens(cog_name, cog),
            }
            aliases = {_normalise(x) for x in COG_COMMAND_ALIASES.get(cog_name, set())}
            names.update(aliases)

            if needle in names:
                return cog_name, cog

            # Match /canyon to CanyonCog or /furnace to WoS Furnace Calculator.
            for command in self._commands_for_cog(cog_name, cog, guild_id):
                if needle == _normalise(command.name) or needle == _normalise(command.qualified_name):
                    return cog_name, cog
                if _normalise(command.name).startswith(needle) and len(needle) >= 4:
                    return cog_name, cog

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

        if cog_name == "AdminCog" and not self._is_admin_guild(guild_id):
            return f"**{title}** — loaded; admin/test server only."

        commands_for_cog = self._commands_for_cog(cog_name, cog, guild_id)
        if commands_for_cog:
            names = []
            for command in commands_for_cog[:8]:
                subs = self._subcommands(command)
                suffix = f" +{len(subs)}" if subs else ""
                names.append(f"`/{command.name}`{suffix}")
            extra = len(commands_for_cog) - 8
            more = f" +{extra} more" if extra > 0 else ""
            return f"**{title}** — " + ", ".join(names) + more

        listeners = len(cog.get_listeners())
        if listeners:
            return f"**{title}** — loaded; listener/background feature only."
        return f"**{title}** — loaded; no slash commands."

    def _detailed_cog_embed(self, guild_id: int, cog_name: str, cog: commands.Cog) -> discord.Embed:
        title = self._cog_title(cog_name, cog)
        summary = self._cog_summary(cog_name, cog)
        embed = discord.Embed(
            title=f"Help: {title}",
            description=summary,
            colour=discord.Colour.blurple(),
        )

        embed.add_field(name="Loaded cog", value=f"`{cog_name}`", inline=True)
        listeners = cog.get_listeners()
        embed.add_field(name="Listeners", value=str(len(listeners)), inline=True)

        if cog_name == "AdminCog" and not self._is_admin_guild(guild_id):
            embed.add_field(name="Commands", value="Admin/test server only.", inline=False)
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
            embed.add_field(name="Commands", value="No slash commands registered for this cog.", inline=False)

        if listeners:
            names = [f"`{name}`" for name, _ in listeners[:10]]
            more = f" +{len(listeners) - 10} more" if len(listeners) > 10 else ""
            embed.add_field(name="Listeners", value=", ".join(names) + more, inline=False)

        details = self._meta(cog).get("details")
        if details:
            embed.add_field(name="Details", value=str(details)[:1024], inline=False)

        return embed

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

            await interaction.followup.send(f"No command or loaded cog found for `{command}`.", ephemeral=True)
            return

        visible_commands = self._visible_top_commands(guild_id)
        main = [c for c in visible_commands if c.name not in UTILITY_COMMANDS and c.name not in ADMIN_COMMANDS]
        utility = [c for c in visible_commands if c.name in UTILITY_COMMANDS]
        admin = [c for c in visible_commands if c.name in ADMIN_COMMANDS]

        embed = discord.Embed(
            title="HotBot Help",
            description="Main commands first. Every loaded cog is listed below. Use `/help command:<name>` for details.",
            colour=discord.Colour.blurple(),
        )

        if main:
            lines = [self._short_command_line(cmd) for cmd in main]
            for index, chunk in enumerate(_chunks(lines), start=1):
                embed.add_field(name="Main Commands" if index == 1 else f"Main Commands {index}", value=chunk, inline=False)

        if admin and self._is_admin_guild(guild_id):
            lines = [self._short_command_line(cmd) for cmd in admin]
            embed.add_field(name="Admin/Test Commands", value="\n".join(lines), inline=False)

        cog_lines = []
        for cog_name, cog in sorted(self.bot.cogs.items(), key=lambda item: self._cog_title(item[0], item[1]).lower()):
            cog_lines.append(self._cog_line(cog_name, cog, guild_id))

        for index, chunk in enumerate(_chunks(cog_lines), start=1):
            embed.add_field(name="Loaded Cogs" if index == 1 else f"Loaded Cogs {index}", value=chunk, inline=False)

        if utility:
            lines = [self._short_command_line(cmd) for cmd in utility]
            embed.add_field(name="Utility", value="\n".join(lines), inline=False)

        embed.set_footer(text="New cogs appear here automatically after they load/sync. Detailed help accepts command names and cog names.")
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
            add_choice(self._cog_title(cog_name, cog), self._cog_title(cog_name, cog).lower())

        return choices[:25]


async def setup(bot: commands.Bot):
    cog = HelpCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
