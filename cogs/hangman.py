from __future__ import annotations

import asyncio
import io
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_admin_cog, bind_public_cog
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.storage import (
    load_global_json,
    load_guild_json,
    save_global_json,
    save_guild_json,
)
from core.utils import ensure_deferred
from core.vault import is_image

GAMES_FILENAME = "hangman_games.json"
WORDS_FILENAME = "hangman_words.json"
MEDIA_FILENAME = "hangman_media.json"
MAX_MISSES = 6

DEFAULT_WORDS: dict[str, list[str]] = {
    "Heroes": [
        "Jeronimo",
        "Natalia",
        "Molly",
        "Sergey",
        "Bahiti",
        "Gina",
        "Zinman",
        "Jessie",
        "Patrick",
        "Charlie",
        "Cloris",
        "Eugene",
        "Smith",
        "Seo Yoon",
        "Jasser",
        "Walis Bokan",
        "Flint",
        "Philly",
        "Mia",
        "Greg",
        "Logan",
        "Ahmose",
        "Reina",
        "Lynn",
        "Gwen",
        "Hector",
        "Norah",
        "Wu Ming",
        "Wayne",
        "Renee",
        "Bradley",
        "Sonya",
        "Gordon",
    ],
    "Buildings": [
        "Furnace",
        "Embassy",
        "Infirmary",
        "Command Center",
        "Research Center",
        "Storehouse",
        "Barracks",
        "Lancer Camp",
        "Marksman Camp",
        "Infantry Camp",
        "Chief House",
        "Lighthouse",
        "Arena",
    ],
    "Events": [
        "State of Power",
        "Sunfire Castle",
        "Foundry Battle",
        "Canyon Clash",
        "Bear Hunt",
        "Crazy Joe",
        "Alliance Mobilization",
        "King of Icefield",
        "Frostfire Mine",
        "Hall of Chiefs",
        "Fishing Tournament",
        "Swordland Showdown",
        "Tundra Trading Station",
    ],
    "General": [
        "Fire Crystal",
        "Refined Fire Crystal",
        "Chief Gear",
        "Chief Charm",
        "Hero Gear",
        "Essence Stone",
        "Mythic Shard",
        "Alliance",
        "Rally",
        "Garrison",
        "Icefield",
        "Tundra",
        "Exploration",
        "Expedition",
        "Survivor",
        "Resources",
        "Teleport",
        "Shield",
        "Troops",
        "Furnace Upgrade",
    ],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_phrase(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip().upper())
    return value


def _mask_word(word: str, guessed_letters: set[str]) -> str:
    output: list[str] = []
    for char in word:
        if char.isalpha():
            output.append(char if char in guessed_letters else "_")
        elif char == " ":
            output.append("   ")
        else:
            output.append(char)
    return " ".join(output)


def _word_is_complete(word: str, guessed_letters: set[str]) -> bool:
    return all(not char.isalpha() or char in guessed_letters for char in word)


def _safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip().upper()
        if text and text not in out:
            out.append(text)
    return out


class GuessLetterModal(discord.ui.Modal, title="Guess a letter"):
    letter = discord.ui.TextInput(
        label="Letter",
        placeholder="A",
        min_length=1,
        max_length=1,
        required=True,
    )

    def __init__(self, service: "HangmanService"):
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.submit_letter(interaction, str(self.letter.value))


class GuessWordModal(discord.ui.Modal, title="Guess the word"):
    word = discord.ui.TextInput(
        label="Word or phrase",
        placeholder="Type the full answer",
        min_length=1,
        max_length=80,
        required=True,
    )

    def __init__(self, service: "HangmanService"):
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.submit_word(interaction, str(self.word.value))


class CustomHangmanModal(discord.ui.Modal, title="Start custom Hangman"):
    secret_word = discord.ui.TextInput(
        label="Secret word or phrase",
        placeholder="Only you will see this while entering it",
        min_length=1,
        max_length=80,
        required=True,
    )
    hint = discord.ui.TextInput(
        label="Category or hint (optional)",
        placeholder="Example: WoS hero, building, event...",
        min_length=1,
        max_length=60,
        required=False,
    )

    def __init__(self, service: "HangmanService"):
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        await self.service.start_custom_game(
            interaction,
            str(self.secret_word.value),
            str(self.hint.value or ""),
        )


class HangmanView(discord.ui.View):
    def __init__(self, service: "HangmanService"):
        super().__init__(timeout=None)
        self.service = service

    @discord.ui.button(
        label="Guess Letter",
        style=discord.ButtonStyle.success,
        custom_id="hotbot:hangman:guess_letter:v1",
    )
    async def guess_letter(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not self.service.is_active_game_message(interaction):
            await interaction.response.send_message(
                "That is not the current Hangman game message.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(GuessLetterModal(self.service))

    @discord.ui.button(
        label="Guess Word",
        style=discord.ButtonStyle.primary,
        custom_id="hotbot:hangman:guess_word:v1",
    )
    async def guess_word(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not self.service.is_active_game_message(interaction):
            await interaction.response.send_message(
                "That is not the current Hangman game message.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(GuessWordModal(self.service))


class HangmanService:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self.media_channel_id = int((bot.hot_config or {}).get("media_channel_id") or 0)
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

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
        return self.settings.is_feature_allowed(guild_id, channel_id, "games")

    def _load_games_blob(self, guild_id: int) -> dict[str, Any]:
        raw = load_guild_json(guild_id, GAMES_FILENAME, {"games": {}})
        if not isinstance(raw, dict):
            raw = {"games": {}}
        games = raw.get("games")
        if not isinstance(games, dict):
            raw["games"] = {}
        return raw

    def _save_games_blob(self, guild_id: int, blob: dict[str, Any]) -> None:
        save_guild_json(guild_id, GAMES_FILENAME, blob)

    def _get_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        blob = self._load_games_blob(guild_id)
        game = blob["games"].get(str(channel_id))
        return game if isinstance(game, dict) else None

    def _set_game(self, guild_id: int, channel_id: int, game: dict[str, Any]) -> None:
        blob = self._load_games_blob(guild_id)
        blob["games"][str(channel_id)] = game
        self._save_games_blob(guild_id, blob)

    def _remove_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        blob = self._load_games_blob(guild_id)
        old = blob["games"].pop(str(channel_id), None)
        self._save_games_blob(guild_id, blob)
        return old if isinstance(old, dict) else None

    def has_game(self, guild_id: int | None, channel_id: int | None) -> bool:
        if not guild_id or not channel_id:
            return False
        return self._get_game(int(guild_id), int(channel_id)) is not None

    def is_active_game_message(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild_id or not interaction.channel_id or interaction.message is None:
            return False
        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if not game:
            return False
        return int(game.get("message_id") or 0) == interaction.message.id

    def start_issue(self, interaction: discord.Interaction) -> str | None:
        guild = interaction.guild
        channel_id = interaction.channel_id
        if guild is None or channel_id is None:
            return "This game must be started in a server channel."
        if not self.allowed(interaction.guild_id, channel_id):
            return "❌ Hangman can only be used in the configured games channel(s)."

        missing = self.missing_stages()
        if missing:
            stages = ", ".join(str(x) for x in missing)
            return (
                f"Hangman is missing vault image stage(s): **{stages}**. "
                "Upload them with `/hangman_image` in the admin/test server first."
            )

        existing = self._get_game(guild.id, channel_id)
        if existing:
            message_id = int(existing.get("message_id") or 0)
            jump = (
                f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"
                if message_id
                else ""
            )
            text = "A Hangman game is already open in this channel."
            if jump:
                text += f" [Open it]({jump})"
            return text
        return None

    def _load_words(self, guild_id: int) -> dict[str, list[str]]:
        raw = load_guild_json(guild_id, WORDS_FILENAME, None)
        if not isinstance(raw, dict) or not isinstance(raw.get("categories"), dict):
            raw = {"categories": DEFAULT_WORDS}
            save_guild_json(guild_id, WORDS_FILENAME, raw)

        categories: dict[str, list[str]] = {}
        for category, words in raw["categories"].items():
            if not isinstance(words, list):
                continue
            cleaned: list[str] = []
            for word in words:
                text = _normalise_phrase(str(word))
                if any(char.isalpha() for char in text) and text not in cleaned:
                    cleaned.append(text)
            if cleaned:
                categories[str(category).strip() or "General"] = cleaned

        if not categories:
            categories = {key: [_normalise_phrase(x) for x in values] for key, values in DEFAULT_WORDS.items()}
            save_guild_json(guild_id, WORDS_FILENAME, {"categories": categories})
        return categories

    def _pick_word(self, guild_id: int) -> tuple[str, str]:
        categories = self._load_words(guild_id)
        category = random.choice(list(categories.keys()))
        return category, random.choice(categories[category])

    def _load_media(self) -> dict[str, Any]:
        raw = load_global_json(MEDIA_FILENAME, {"stages": {}})
        if not isinstance(raw, dict):
            raw = {"stages": {}}
        if not isinstance(raw.get("stages"), dict):
            raw["stages"] = {}
        return raw

    def _save_media(self, data: dict[str, Any]) -> None:
        save_global_json(MEDIA_FILENAME, data)

    def missing_stages(self) -> list[int]:
        data = self._load_media()
        return [stage for stage in range(MAX_MISSES + 1) if not isinstance(data["stages"].get(str(stage)), dict)]

    async def stage_url(self, stage: int) -> str | None:
        stage = max(0, min(MAX_MISSES, int(stage)))
        data = self._load_media()
        entry = data["stages"].get(str(stage))
        if not isinstance(entry, dict):
            return None

        channel_id = int(entry.get("vault_channel_id") or 0)
        message_id = int(entry.get("vault_message_id") or 0)
        if channel_id and message_id:
            try:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
                if message.attachments:
                    current_url = message.attachments[0].url
                    if current_url != entry.get("url"):
                        entry["url"] = current_url
                        data["stages"][str(stage)] = entry
                        self._save_media(data)
                    return current_url
            except Exception as exc:
                warn(f"hangman stage {stage} vault refresh failed: {exc!r}")

        url = entry.get("url")
        return str(url) if url else None

    async def _build_embed(
        self,
        game: dict[str, Any],
        *,
        status: str = "active",
        ended_by: int | None = None,
    ) -> discord.Embed:
        word = _normalise_phrase(str(game.get("word") or ""))
        guessed = set(_safe_string_list(game.get("guessed_letters")))
        wrong_letters = _safe_string_list(game.get("wrong_letters"))
        wrong_words = _safe_string_list(game.get("wrong_words"))
        misses = max(0, min(MAX_MISSES, int(game.get("misses") or 0)))

        if status == "won":
            title = "🔥 Hangman — Solved!"
            colour = discord.Colour.green()
            description = f"The word was:\n```{word}```"
        elif status == "lost":
            title = "🥶 Hangman — Frozen Out"
            colour = discord.Colour.red()
            description = f"The word was:\n```{word}```"
        elif status == "ended":
            title = "🛑 Hangman — Ended"
            colour = discord.Colour.dark_grey()
            who = f" by <@{ended_by}>" if ended_by else ""
            description = f"Game ended{who}.\nThe word was:\n```{word}```"
        else:
            title = "❄️ WoS Hangman"
            colour = discord.Colour.blurple()
            description = f"```{_mask_word(word, guessed)}```"

        embed = discord.Embed(title=title, description=description, colour=colour)
        embed.add_field(name="Category", value=str(game.get("category") or "General"), inline=True)
        embed.add_field(name="Misses", value=f"{misses} / {MAX_MISSES}", inline=True)
        embed.add_field(name="Started by", value=f"<@{int(game.get('started_by') or 0)}>", inline=True)

        wrong_parts: list[str] = []
        if wrong_letters:
            wrong_parts.append("Letters: " + ", ".join(wrong_letters))
        if wrong_words:
            wrong_parts.append("Words: " + ", ".join(wrong_words))
        embed.add_field(
            name="Wrong guesses",
            value="\n".join(wrong_parts) if wrong_parts else "None yet",
            inline=False,
        )

        if status == "active":
            embed.set_footer(text="Anyone can guess. Anyone can use /hangman_end. No timeout.")
        else:
            embed.set_footer(text="Use /hangman to start the next game.")

        image_stage = MAX_MISSES if status == "lost" else misses
        image_url = await self.stage_url(image_stage)
        if image_url:
            embed.set_image(url=image_url)
        return embed

    async def _fetch_game_message(self, game: dict[str, Any]) -> discord.Message | None:
        channel_id = int(game.get("channel_id") or 0)
        message_id = int(game.get("message_id") or 0)
        if not channel_id or not message_id:
            return None
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            return await channel.fetch_message(message_id)  # type: ignore[attr-defined]
        except Exception as exc:
            warn(f"hangman fetch message failed: {exc!r}")
            return None

    async def _edit_game_message(
        self,
        game: dict[str, Any],
        *,
        status: str = "active",
        ended_by: int | None = None,
    ) -> None:
        message = await self._fetch_game_message(game)
        if message is None:
            return
        embed = await self._build_embed(game, status=status, ended_by=ended_by)
        view: discord.ui.View | None = HangmanView(self) if status == "active" else None
        try:
            await message.edit(embed=embed, view=view)
        except Exception as exc:
            warn(f"hangman edit message failed: {exc!r}")

    async def _start_with_word(
        self,
        interaction: discord.Interaction,
        *,
        category: str,
        word: str,
        source: str,
    ) -> None:
        guild = interaction.guild
        if guild is None or interaction.channel_id is None:
            await interaction.followup.send(
                "This game must be started in a server channel.",
                ephemeral=True,
            )
            return

        guild_id = guild.id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            existing = self._get_game(guild_id, channel_id)
            if existing:
                message_id = int(existing.get("message_id") or 0)
                jump = (
                    f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
                    if message_id
                    else ""
                )
                text = "A Hangman game is already open in this channel."
                if jump:
                    text += f" [Open it]({jump})"
                await interaction.followup.send(text, ephemeral=True)
                return

            game: dict[str, Any] = {
                "word": word,
                "category": category,
                "source": source,
                "guessed_letters": [],
                "wrong_letters": [],
                "wrong_words": [],
                "misses": 0,
                "started_by": interaction.user.id,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "message_id": 0,
                "created_at": _utc_now(),
            }
            message = await interaction.followup.send(
                embed=await self._build_embed(game),
                view=HangmanView(self),
                ephemeral=False,
                wait=True,
            )

            game["message_id"] = int(message.id)
            self._set_game(guild_id, channel_id, game)

    async def start_game(self, interaction: discord.Interaction) -> None:
        issue = self.start_issue(interaction)
        if issue:
            await interaction.followup.send(issue, ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "This game must be started in a server channel.",
                ephemeral=True,
            )
            return

        category, word = self._pick_word(guild.id)
        await self._start_with_word(
            interaction,
            category=category,
            word=word,
            source="automatic",
        )

    async def start_custom_game(
        self,
        interaction: discord.Interaction,
        raw_word: str,
        raw_hint: str,
    ) -> None:
        issue = self.start_issue(interaction)
        if issue:
            await interaction.followup.send(issue, ephemeral=True)
            return

        word = _normalise_phrase(raw_word)
        if not any(char.isalpha() for char in word):
            await interaction.followup.send(
                "The custom answer needs at least one letter.",
                ephemeral=True,
            )
            return

        hint = re.sub(r"\s+", " ", (raw_hint or "").strip())
        category = hint or "Custom word"
        await self._start_with_word(
            interaction,
            category=category,
            word=word,
            source="manual",
        )

    async def submit_letter(self, interaction: discord.Interaction, raw_letter: str) -> None:
        if not interaction.guild_id or not interaction.channel_id:
            await interaction.response.send_message("This only works in the game channel.", ephemeral=True)
            return

        letter = raw_letter.strip().upper()
        if len(letter) != 1 or not letter.isalpha():
            await interaction.response.send_message("Enter one letter from A to Z.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game:
                await interaction.followup.send("There is no active Hangman game here.", ephemeral=True)
                return

            word = _normalise_phrase(str(game.get("word") or ""))
            guessed = set(_safe_string_list(game.get("guessed_letters")))
            wrong_letters = _safe_string_list(game.get("wrong_letters"))

            if letter in guessed or letter in wrong_letters:
                await interaction.followup.send(f"**{letter}** has already been guessed.", ephemeral=True)
                return

            if letter in word:
                guessed.add(letter)
                game["guessed_letters"] = sorted(guessed)
                won = _word_is_complete(word, guessed)
                if won:
                    self._remove_game(guild_id, channel_id)
                    await self._edit_game_message(game, status="won")
                    await interaction.followup.send(f"Correct — **{letter}** solved it!", ephemeral=True)
                    return
                self._set_game(guild_id, channel_id, game)
                await self._edit_game_message(game)
                await interaction.followup.send(f"Correct — **{letter}** is in the word.", ephemeral=True)
                return

            wrong_letters.append(letter)
            game["wrong_letters"] = wrong_letters
            game["misses"] = min(MAX_MISSES, int(game.get("misses") or 0) + 1)
            lost = int(game["misses"]) >= MAX_MISSES
            if lost:
                self._remove_game(guild_id, channel_id)
                await self._edit_game_message(game, status="lost")
                await interaction.followup.send(f"No **{letter}**. That was the final miss.", ephemeral=True)
                return

            self._set_game(guild_id, channel_id, game)
            await self._edit_game_message(game)
            await interaction.followup.send(f"No **{letter}** in the word.", ephemeral=True)

    async def submit_word(self, interaction: discord.Interaction, raw_word: str) -> None:
        if not interaction.guild_id or not interaction.channel_id:
            await interaction.response.send_message("This only works in the game channel.", ephemeral=True)
            return

        guess = _normalise_phrase(raw_word)
        if not guess or not any(char.isalpha() for char in guess):
            await interaction.response.send_message("Enter a word or phrase.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id

        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game:
                await interaction.followup.send("There is no active Hangman game here.", ephemeral=True)
                return

            word = _normalise_phrase(str(game.get("word") or ""))
            if guess == word:
                game["guessed_letters"] = sorted({char for char in word if char.isalpha()})
                self._remove_game(guild_id, channel_id)
                await self._edit_game_message(game, status="won")
                await interaction.followup.send("Correct — you solved it!", ephemeral=True)
                return

            wrong_words = _safe_string_list(game.get("wrong_words"))
            if guess in wrong_words:
                await interaction.followup.send("That word has already been guessed.", ephemeral=True)
                return

            wrong_words.append(guess)
            game["wrong_words"] = wrong_words
            game["misses"] = min(MAX_MISSES, int(game.get("misses") or 0) + 1)
            lost = int(game["misses"]) >= MAX_MISSES
            if lost:
                self._remove_game(guild_id, channel_id)
                await self._edit_game_message(game, status="lost")
                await interaction.followup.send("Wrong word. That was the final miss.", ephemeral=True)
                return

            self._set_game(guild_id, channel_id, game)
            await self._edit_game_message(game)
            await interaction.followup.send("Wrong word — one miss added.", ephemeral=True)

    async def end_game(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id or not interaction.channel_id:
            await interaction.followup.send("This only works in the game channel.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._remove_game(guild_id, channel_id)
            if not game:
                await interaction.followup.send("There is no active Hangman game here.", ephemeral=True)
                return
            await self._edit_game_message(game, status="ended", ended_by=interaction.user.id)
            await interaction.followup.send("Hangman ended. Anyone can start the next one.", ephemeral=True)

    def _has_admin_role(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        role_names = set((self.bot.hot_config or {}).get("admin_role_names") or [])
        return any(role.name in role_names for role in member.roles)

    async def save_stage_image(
        self,
        interaction: discord.Interaction,
        stage: int,
        image: discord.Attachment,
    ) -> None:
        admin_guild_id = int((self.bot.hot_config or {}).get("admin_guild_id") or 0)
        if interaction.guild_id != admin_guild_id:
            await interaction.followup.send("This command only works in the admin/test server.", ephemeral=True)
            return
        if not self._has_admin_role(interaction):
            await interaction.followup.send("Nope. Admin/Tech role only.", ephemeral=True)
            return
        if not is_image(image):
            await interaction.followup.send("That attachment is not a PNG, JPG, GIF or WEBP image.", ephemeral=True)
            return
        if not self.media_channel_id:
            await interaction.followup.send("MEDIA_CHANNEL_ID is not configured.", ephemeral=True)
            return

        try:
            channel = self.bot.get_channel(self.media_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(self.media_channel_id)

            suffix = Path(image.filename or "image.png").suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                suffix = ".png"
            payload = await image.read()
            filename = f"hangman_stage_{stage}{suffix}"
            message = await channel.send(file=discord.File(io.BytesIO(payload), filename=filename))  # type: ignore[attr-defined]
            if not message.attachments:
                raise RuntimeError("Vault upload succeeded but returned no attachment.")

            attachment = message.attachments[0]
            media = self._load_media()
            old = media["stages"].get(str(stage))
            media["stages"][str(stage)] = {
                "url": attachment.url,
                "filename": attachment.filename,
                "vault_channel_id": message.channel.id,
                "vault_message_id": message.id,
                "updated_by": interaction.user.id,
                "updated_at": _utc_now(),
            }
            self._save_media(media)

            if isinstance(old, dict):
                old_channel_id = int(old.get("vault_channel_id") or 0)
                old_message_id = int(old.get("vault_message_id") or 0)
                if old_channel_id and old_message_id and old_message_id != message.id:
                    try:
                        old_channel = self.bot.get_channel(old_channel_id)
                        if old_channel is None:
                            old_channel = await self.bot.fetch_channel(old_channel_id)
                        old_message = await old_channel.fetch_message(old_message_id)  # type: ignore[attr-defined]
                        await old_message.delete()
                    except Exception as exc:
                        warn(f"hangman old vault image cleanup failed: {exc!r}")

            missing = self.missing_stages()
            if missing:
                remaining = ", ".join(str(x) for x in missing)
                await interaction.followup.send(
                    f"✅ Saved Hangman stage **{stage}** to the media vault. Missing: **{remaining}**.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"✅ Saved Hangman stage **{stage}**. All seven stages are ready.",
                    ephemeral=True,
                )
        except Exception as exc:
            warn(f"hangman image upload failed: {exc!r}")
            await interaction.followup.send(f"⚠️ Failed to save the image: {exc}", ephemeral=True)


class HangmanCog(commands.Cog):
    HELP_META = {
        "title": "WoS Hangman",
        "summary": "A permanent community Hangman game with button guesses and WoS vault artwork.",
        "details": "Use /hangman for a random WoS word or /hangman_custom for a private manual answer. Anyone can guess and anyone can use /hangman_end.",
    }

    def __init__(self, bot: commands.Bot, service: HangmanService):
        self.bot = bot
        self.service = service

    @app_commands.command(name="hangman", description="Start a WoS Hangman game in this channel")
    async def hangman(self, interaction: discord.Interaction) -> None:
        log_cmd("hangman", interaction)
        if not await ensure_deferred(interaction, ephemeral=False):
            return
        await self.service.start_game(interaction)

    @app_commands.command(
        name="hangman_custom",
        description="Start Hangman with a secret word or phrase you enter privately",
    )
    async def hangman_custom(self, interaction: discord.Interaction) -> None:
        log_cmd("hangman_custom", interaction)
        issue = self.service.start_issue(interaction)
        if issue:
            await interaction.response.send_message(issue, ephemeral=True)
            return
        await interaction.response.send_modal(CustomHangmanModal(self.service))

    @app_commands.command(name="hangman_end", description="End the current Hangman game so a new one can start")
    async def hangman_end(self, interaction: discord.Interaction) -> None:
        log_cmd("hangman_end", interaction)
        if not await ensure_deferred(interaction, ephemeral=True):
            return
        await self.service.end_game(interaction)


class HangmanMediaCog(commands.Cog):
    HELP_META = {
        "title": "Hangman Artwork",
        "summary": "Admin command for saving Hangman stage artwork into the media vault.",
    }

    def __init__(self, bot: commands.Bot, service: HangmanService):
        self.bot = bot
        self.service = service

    @app_commands.command(name="hangman_image", description="Save one Hangman stage image into the media vault")
    @app_commands.describe(
        stage="Stage number from 0 to 6",
        image="WoS Hangman image for this stage",
    )
    async def hangman_image(
        self,
        interaction: discord.Interaction,
        stage: app_commands.Range[int, 0, 6],
        image: discord.Attachment,
    ) -> None:
        log_cmd("hangman_image", interaction)
        if not await ensure_deferred(interaction, ephemeral=True):
            return
        await self.service.save_stage_image(interaction, int(stage), image)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    service = HangmanService(bot)

    # Registers the two permanent button custom IDs so old games still work
    # after Railway restarts or a new bot version is deployed.
    bot.add_view(HangmanView(service))

    public_cog = HangmanCog(bot, service)
    bind_public_cog(public_cog, bot, include_admin=True)
    await bot.add_cog(public_cog)

    admin_cog = HangmanMediaCog(bot, service)
    bind_admin_cog(admin_cog, bot)
    await bot.add_cog(admin_cog)
