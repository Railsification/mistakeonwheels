from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.logger import log_cmd
from core.utils import ensure_deferred


UTILITY_COMMANDS = {"hello", "acktest"}
ADMIN_COMMANDS = {"council"}


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _commands_for_guild(self, guild_id: int):
        commands_for_guild = []
        for command in self.bot.tree.get_commands():
            gids = getattr(command, "_guild_ids", None)
            if gids is None or guild_id in gids:
                if command.name not in ADMIN_COMMANDS:
                    commands_for_guild.append(command)
        return sorted(commands_for_guild, key=lambda c: c.name.lower())

    def _find_command(self, guild_id: int, wanted: str):
        wanted = wanted.strip().lower().lstrip("/")
        if not wanted:
            return None
        parts = wanted.split()
        top = parts[0]
        for command in self._commands_for_guild(guild_id):
            if command.name.lower() != top:
                continue
            current = command
            for part in parts[1:]:
                children = getattr(current, "commands", []) or []
                current = next((c for c in children if c.name.lower() == part), None)
                if current is None:
                    return None
            return current
        return None

    def _command_signature(self, command) -> str:
        params = []
        for param in getattr(command, "parameters", []) or []:
            name = getattr(param, "display_name", None) or getattr(param, "name", "param")
            required = getattr(param, "required", False)
            params.append(f"<{name}>" if required else f"[{name}]")
        return f"/{command.qualified_name} {' '.join(params)}".strip()

    def _subcommands(self, command) -> list:
        return list(getattr(command, "commands", []) or [])

    @app_commands.command(name="help", description="Show bot help or details for one command.")
    @app_commands.describe(command="Optional command/group name, like suggestion or furnace_set")
    async def help_cmd(self, interaction: discord.Interaction, command: Optional[str] = None):
        log_cmd("help", interaction)
        await ensure_deferred(interaction, ephemeral=True)

        if interaction.guild_id is None:
            await interaction.followup.send("Use `/help` inside a server.", ephemeral=True)
            return

        if command:
            found = self._find_command(interaction.guild_id, command)
            if found is None:
                await interaction.followup.send(f"No command found for `{command}`.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"Help: /{found.qualified_name}",
                description=found.description or "No description set.",
                colour=discord.Colour.blurple(),
            )
            embed.add_field(name="Usage", value=f"`{self._command_signature(found)}`", inline=False)

            subs = self._subcommands(found)
            if subs:
                lines = [f"`/{sub.qualified_name}` — {sub.description or 'No description'}" for sub in sorted(subs, key=lambda c: c.name.lower())]
                embed.add_field(name="Subcommands", value="\n".join(lines), inline=False)

            params = getattr(found, "parameters", []) or []
            if params:
                lines = []
                for param in params:
                    name = getattr(param, "display_name", None) or getattr(param, "name", "param")
                    desc = getattr(param, "description", None) or "No description"
                    required = "required" if getattr(param, "required", False) else "optional"
                    lines.append(f"`{name}` — {desc} ({required})")
                embed.add_field(name="Options", value="\n".join(lines[:15]), inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        commands_for_guild = self._commands_for_guild(interaction.guild_id)
        main = [c for c in commands_for_guild if c.name not in UTILITY_COMMANDS]
        util = [c for c in commands_for_guild if c.name in UTILITY_COMMANDS]

        embed = discord.Embed(
            title="HotBot Help",
            description="Main commands first. Use `/help command:<name>` for details.",
            colour=discord.Colour.blurple(),
        )

        if main:
            lines = []
            for cmd in main:
                subs = self._subcommands(cmd)
                if subs:
                    lines.append(f"`/{cmd.name}` — {cmd.description or 'Command group'} ({len(subs)} subcommands)")
                else:
                    lines.append(f"`/{cmd.name}` — {cmd.description or 'No description'}")
            embed.add_field(name="Main Commands", value="\n".join(lines[:25]), inline=False)

        if util:
            lines = [f"`/{cmd.name}` — {cmd.description or 'No description'}" for cmd in util]
            embed.add_field(name="Utility", value="\n".join(lines), inline=False)

        embed.set_footer(text="Help auto-builds from loaded slash commands, so new cogs appear after they load/sync.")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    cog = HelpCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
