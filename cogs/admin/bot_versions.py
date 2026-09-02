# cogs/admin/bot_versions.py
from __future__ import annotations

import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_admin_cog
from core.logger import log_cmd
from core.version import BOT_NAME, BOT_VERSION, file_revision


__version__ = "1.1.0"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _module_file_path(module_name: str, module_path: Path) -> str:
    try:
        return module_path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return module_name.replace(".", "/") + ".py"


def _loaded_module_versions() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    for module_name, module in tuple(sys.modules.items()):
        if not module_name.startswith(("cogs.", "core.")):
            continue
        if module is None:
            continue

        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        module_path = Path(module_file)
        if module_path.suffix != ".py":
            continue

        found.append(
            (
                _module_file_path(module_name, module_path),
                file_revision(module_path),
            )
        )

    found.sort(
        key=lambda item: (
            0 if item[0].startswith("cogs/") else 1,
            item[0].lower(),
        )
    )
    return found


def _chunk_lines(lines: list[str], limit: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = ""

    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
        current = line

    if current:
        chunks.append(current)

    return chunks or ["No version information available."]


class BotVersionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_admin_guild(self, interaction: discord.Interaction) -> bool:
        config = getattr(self.bot, "hot_config", {}) or {}
        admin_guild_id = int(config.get("admin_guild_id", 0) or 0)
        return interaction.guild_id == admin_guild_id

    def _has_admin_role(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False

        if member.guild_permissions.administrator:
            return True

        config = getattr(self.bot, "hot_config", {}) or {}
        role_names = set(config.get("admin_role_names", []))
        return bool(role_names) and any(
            role.name in role_names
            for role in member.roles
        )

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if not self._is_admin_guild(interaction):
            await interaction.response.send_message(
                "Admin commands only work in the admin/test server.",
                ephemeral=True,
            )
            return False

        if not self._has_admin_role(interaction):
            await interaction.response.send_message(
                "Nope. Admin role only.",
                ephemeral=True,
            )
            return False

        return True

    @app_commands.command(
        name="bot_versions",
        description="Show private internal bot file versions.",
    )
    async def show_versions(self, interaction: discord.Interaction) -> None:
        # Discord.py reserves Python method names beginning with bot_ / cog_.
        # The public slash command still remains /bot_versions.
        log_cmd("bot_versions", interaction)

        if not await self._require_admin(interaction):
            return

        versioned = _loaded_module_versions()

        lines = [
            f"🔒 **{BOT_NAME} internal versions**",
            f"Deployment: **{BOT_VERSION}**",
            "",
        ]

        if versioned:
            lines.append("**Versioned loaded files:**")
            lines.extend(
                f"`{path}` — **v{version}**"
                for path, version in versioned
            )
        else:
            lines.append(
                "No loaded cogs/core modules were found."
            )

        lines.extend(
            [
                "",
                "Versions are generated automatically from each file's contents.",
            ]
        )

        chunks = _chunk_lines(lines)

        await interaction.response.send_message(
            chunks[0],
            ephemeral=True,
        )

        for chunk in chunks[1:]:
            await interaction.followup.send(
                chunk,
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    cog = BotVersionsCog(bot)
    bind_admin_cog(cog, bot)
    await bot.add_cog(cog)
