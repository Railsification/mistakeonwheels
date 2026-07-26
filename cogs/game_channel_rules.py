from __future__ import annotations

from types import MethodType

from discord.ext import commands

from core.logger import warn


class GameChannelRulesCog(commands.Cog):
    """Applies the individual Hangman channel gate to the existing service."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._patched = False

    def _patch_hangman(self) -> None:
        if self._patched:
            return

        hangman_cog = self.bot.get_cog("HangmanCog")
        service = getattr(hangman_cog, "service", None)
        if service is None:
            return

        if getattr(service, "_hotbot_individual_channel_gate", False):
            self._patched = True
            return

        def allowed(
            current_service,
            guild_id: int | None,
            channel_id: int | None,
        ) -> bool:
            return current_service.settings.is_game_allowed(
                guild_id,
                channel_id,
                "hangman",
            )

        service.allowed = MethodType(allowed, service)
        service._hotbot_individual_channel_gate = True
        self._patched = True

    async def cog_load(self) -> None:
        self._patch_hangman()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._patch_hangman()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GameChannelRulesCog(bot))
