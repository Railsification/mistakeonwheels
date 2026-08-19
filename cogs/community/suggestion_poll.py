# cogs/community/suggestion_poll.py
from __future__ import annotations

import asyncio
import json
import re
from difflib import SequenceMatcher
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.command_scope import bind_group_public
from core.logger import ok, warn
from core.storage import (
    configured_guild_ids,
    known_guild_dirs,
    load_guild_json,
    migrate_legacy_file_to_primary,
    save_guild_json,
)

FEATURE_KEY = "suggestion_poll"
__version__ = "1.0.0"
SUGGESTION_POLLS_FILENAME = "suggestion_polls.json"
PREVIOUS_THEMES_FILENAME = "suggestion_previous_themes.json"
MAX_IDEA_LEN = 180
THEME_SIMILARITY_THRESHOLD = 0.86
MAX_VISIBLE_IDEAS = 20
TIEBREAK_DURATION_SECONDS = 24 * 60 * 60
RECOVER_CLOSED_POLLS_WITHIN_SECONDS = 7 * 24 * 60 * 60


def now_ts() -> int:
    return int(time.time())


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def normalise_theme_text(text: str) -> str:
    value = (text or "").casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    tokens = []
    for token in value.split():
        # Small plural normalisation catches things like `Care Bear` vs `Care Bears`.
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        tokens.append(token)
    return " ".join(tokens).strip()


def theme_similarity(left: str, right: str) -> float:
    a = normalise_theme_text(left)
    b = normalise_theme_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    seq = SequenceMatcher(None, a, b).ratio()
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    union = a_tokens | b_tokens
    jaccard = (len(a_tokens & b_tokens) / len(union)) if union else 0.0

    containment = 0.0
    if min(len(a), len(b)) >= 5 and (a in b or b in a):
        containment = 0.94

    return max(seq, jaccard, containment)


class AddIdeaModal(discord.ui.Modal, title="Add WoS PFP idea"):
    idea = discord.ui.TextInput(
        label="Your idea",
        placeholder="Example: Pokémon duo, turtle theme, villain couple, etc.",
        max_length=MAX_IDEA_LEN,
        required=True,
    )

    def __init__(self, cog: "SuggestionPollCog", poll_id: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.poll_id = poll_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.add_idea_from_ui(
            interaction,
            self.poll_id,
            str(self.idea.value),
        )


class VoteIdeaModal(discord.ui.Modal, title="Vote for an idea"):
    idea_number = discord.ui.TextInput(
        label="Idea number",
        placeholder="Example: 3",
        max_length=5,
        required=True,
    )

    def __init__(self, cog: "SuggestionPollCog", poll_id: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.poll_id = poll_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.vote_from_ui(
            interaction,
            self.poll_id,
            safe_int(str(self.idea_number.value), -1),
        )


class SuggestionPollView(discord.ui.View):
    def __init__(self, cog: "SuggestionPollCog", poll_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.poll_id = poll_id

    async def resolve_poll_id(
        self,
        interaction: discord.Interaction,
    ) -> Optional[str]:
        poll = self.cog.get_poll(self.poll_id)
        if poll and poll.get("status") == "open":
            return self.poll_id
        return await self.cog.poll_id_from_message(interaction)

    @discord.ui.button(
        label="Add Idea",
        style=discord.ButtonStyle.primary,
        custom_id="suggestion_poll:add",
    )
    async def add_idea_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        poll_id = await self.resolve_poll_id(interaction)
        if not poll_id:
            await interaction.response.send_message(
                "Couldn’t find that poll.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(AddIdeaModal(self.cog, poll_id))

    @discord.ui.button(
        label="Vote",
        style=discord.ButtonStyle.success,
        custom_id="suggestion_poll:vote",
    )
    async def vote_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        poll_id = await self.resolve_poll_id(interaction)
        if not poll_id:
            await interaction.response.send_message(
                "Couldn’t find that poll.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(VoteIdeaModal(self.cog, poll_id))

    @discord.ui.button(
        label="Previous Themes",
        emoji="🕘",
        style=discord.ButtonStyle.secondary,
        custom_id="suggestion_poll:previous_themes",
    )
    async def previous_themes_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.cog.interaction_allowed(interaction):
            await interaction.response.send_message(
                "Suggestion polls are not enabled in this channel.",
                ephemeral=True,
            )
            return
        if not interaction.guild_id:
            await interaction.response.send_message(
                "Use this inside a server channel.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=self.cog.build_previous_themes_embed(int(interaction.guild_id)),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        custom_id="suggestion_poll:refresh",
    )
    async def refresh_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        poll_id = await self.resolve_poll_id(interaction)
        if not poll_id:
            await interaction.response.send_message(
                "Couldn’t find that poll.",
                ephemeral=True,
            )
            return
        await self.cog.refresh_from_ui(interaction, poll_id)


class TieBreakVoteModal(discord.ui.Modal, title="Vote in the tie-break"):
    idea_number = discord.ui.TextInput(
        label="Idea number",
        placeholder="Enter one of the tie-break idea numbers",
        max_length=5,
        required=True,
    )

    def __init__(self, cog: "SuggestionPollCog", poll_id: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.poll_id = poll_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.tiebreak_vote_from_ui(
            interaction,
            self.poll_id,
            safe_int(str(self.idea_number.value), -1),
        )


class TieBreakView(discord.ui.View):
    def __init__(self, cog: "SuggestionPollCog", poll_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.poll_id = poll_id

    async def resolve_poll_id(
        self,
        interaction: discord.Interaction,
    ) -> Optional[str]:
        poll = self.cog.get_poll(self.poll_id)
        tiebreak = poll.get("tiebreak") if isinstance(poll, dict) else None
        if (
            poll
            and poll.get("status") == "tiebreak"
            and isinstance(tiebreak, dict)
            and tiebreak.get("status") == "open"
        ):
            return self.poll_id
        return await self.cog.tiebreak_poll_id_from_message(interaction)

    @discord.ui.button(
        label="Vote",
        style=discord.ButtonStyle.success,
        custom_id="suggestion_tiebreak:vote",
    )
    async def vote_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        poll_id = await self.resolve_poll_id(interaction)
        if not poll_id:
            await interaction.response.send_message(
                "Couldn’t find that tie-break.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(TieBreakVoteModal(self.cog, poll_id))

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        custom_id="suggestion_tiebreak:refresh",
    )
    async def refresh_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        poll_id = await self.resolve_poll_id(interaction)
        if not poll_id:
            await interaction.response.send_message(
                "Couldn’t find that tie-break.",
                ephemeral=True,
            )
            return
        await self.cog.refresh_tiebreak_from_ui(interaction, poll_id)


class SuggestionPollCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.data: Dict[str, Any] = {"polls": {}}
        self._persistent_views: Dict[int, SuggestionPollView] = {}
        self._tiebreak_views: Dict[int, TieBreakView] = {}
        self._recovery_complete = False
        self.load_data()

    async def cog_load(self) -> None:
        restored = self.restore_persistent_views()
        tiebreak_restored = self.restore_tiebreak_views()
        ok(f"Restored {restored} open suggestion poll button view(s)")
        ok(f"Restored {tiebreak_restored} open suggestion tie-break view(s)")
        if not self.poll_watcher.is_running():
            self.poll_watcher.start()

    async def cog_unload(self) -> None:
        if self.poll_watcher.is_running():
            self.poll_watcher.cancel()

        for message_id, view in list(self._persistent_views.items()):
            try:
                self.bot.remove_view(view, message_id=message_id)
            except Exception:
                pass
        self._persistent_views.clear()

        for message_id, view in list(self._tiebreak_views.items()):
            try:
                self.bot.remove_view(view, message_id=message_id)
            except Exception:
                pass
        self._tiebreak_views.clear()

    def _poll_guild_ids(self) -> List[int]:
        return sorted(
            set(configured_guild_ids(self.bot))
            | set(known_guild_dirs())
        )

    @staticmethod
    def _normalise_poll(poll: Dict[str, Any], guild_id: int) -> Dict[str, Any]:
        poll["guild_id"] = safe_int(poll.get("guild_id"), guild_id)
        poll["channel_id"] = safe_int(poll.get("channel_id"))
        poll["message_id"] = safe_int(poll.get("message_id")) or None
        poll["created_ts"] = safe_int(poll.get("created_ts"))
        poll["end_ts"] = safe_int(poll.get("end_ts"))
        poll["next_idea_no"] = max(1, safe_int(poll.get("next_idea_no"), 1))
        poll.setdefault("status", "open")
        poll.setdefault("ideas", {})
        poll.setdefault("shortlist_size", 4)
        poll.setdefault("allow_multi_vote", True)

        final_shortlist = poll.get("final_shortlist")
        if isinstance(final_shortlist, list):
            poll["final_shortlist"] = [
                safe_int(idea_no)
                for idea_no in final_shortlist
                if safe_int(idea_no) > 0
            ]

        tiebreak = poll.get("tiebreak")
        if isinstance(tiebreak, dict):
            tiebreak.setdefault("status", "open")
            tiebreak["round"] = max(1, safe_int(tiebreak.get("round"), 1))
            tiebreak["slots_remaining"] = max(1, safe_int(tiebreak.get("slots_remaining"), 1))
            tiebreak["end_ts"] = safe_int(tiebreak.get("end_ts"))
            tiebreak["message_id"] = safe_int(tiebreak.get("message_id")) or None
            tiebreak["locked_idea_nos"] = [
                safe_int(idea_no)
                for idea_no in tiebreak.get("locked_idea_nos", [])
                if safe_int(idea_no) > 0
            ]
            tiebreak["candidate_idea_nos"] = [
                safe_int(idea_no)
                for idea_no in tiebreak.get("candidate_idea_nos", [])
                if safe_int(idea_no) > 0
            ]
            votes = tiebreak.get("votes")
            if not isinstance(votes, dict):
                votes = {}
            tiebreak["votes"] = {
                str(safe_int(idea_no)): [
                    safe_int(user_id)
                    for user_id in voters
                    if safe_int(user_id) > 0
                ]
                for idea_no, voters in votes.items()
                if safe_int(idea_no) > 0 and isinstance(voters, list)
            }

        return poll

    def _load_legacy_root_files(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {"polls": {}}
        candidates = {
            Path(SUGGESTION_POLLS_FILENAME),
            Path("data") / SUGGESTION_POLLS_FILENAME,
        }

        env_dir = os.getenv("HOTBOT_DATA_DIR")
        if env_dir:
            candidates.add(Path(env_dir) / SUGGESTION_POLLS_FILENAME)

        for path in candidates:
            if not path.exists() or not path.is_file():
                continue
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                warn(f"Could not read legacy suggestion poll file {path}: {exc!r}")
                continue

            polls = loaded.get("polls") if isinstance(loaded, dict) else None
            if not isinstance(polls, dict):
                continue

            for poll_id, poll in polls.items():
                if isinstance(poll, dict):
                    merged["polls"].setdefault(str(poll_id), poll)

        return merged

    def load_data(self) -> None:
        migrate_legacy_file_to_primary(
            SUGGESTION_POLLS_FILENAME,
            self.bot,
            {"polls": {}},
        )

        self.data = {"polls": {}}

        for guild_id in self._poll_guild_ids():
            loaded = load_guild_json(
                guild_id,
                SUGGESTION_POLLS_FILENAME,
                {"polls": {}},
            )
            if not isinstance(loaded, dict):
                continue

            polls = loaded.get("polls")
            if not isinstance(polls, dict):
                continue

            for poll_id, poll in polls.items():
                if not isinstance(poll, dict):
                    continue
                self.data["polls"][str(poll_id)] = self._normalise_poll(
                    poll,
                    guild_id,
                )

        imported_legacy = False
        legacy = self._load_legacy_root_files()
        for poll_id, poll in legacy.get("polls", {}).items():
            if poll_id in self.data["polls"] or not isinstance(poll, dict):
                continue

            guild_id = safe_int(poll.get("guild_id"))
            if not guild_id:
                continue

            self.data["polls"][poll_id] = self._normalise_poll(
                poll,
                guild_id,
            )
            imported_legacy = True

        if imported_legacy:
            self.save_data()

    def save_data(self) -> None:
        by_guild: Dict[int, Dict[str, Any]] = {}

        for poll_id, poll in self.data.get("polls", {}).items():
            guild_id = safe_int(poll.get("guild_id"))
            if not guild_id:
                continue

            by_guild.setdefault(guild_id, {"polls": {}})["polls"][str(poll_id)] = poll

        guild_ids = set(self._poll_guild_ids()) | set(by_guild)
        for guild_id in guild_ids:
            save_guild_json(
                guild_id,
                SUGGESTION_POLLS_FILENAME,
                by_guild.get(guild_id, {"polls": {}}),
            )

    def restore_persistent_views(self) -> int:
        restored = 0

        for poll_id, poll in self.data.get("polls", {}).items():
            if poll.get("status") != "open":
                continue

            message_id = safe_int(poll.get("message_id"))
            if not message_id:
                continue

            view = SuggestionPollView(self, str(poll_id))
            try:
                self.bot.add_view(view, message_id=message_id)
            except Exception as exc:
                warn(
                    f"Could not restore suggestion poll {poll_id} "
                    f"for message {message_id}: {exc!r}"
                )
                continue

            self._persistent_views[message_id] = view
            restored += 1

        return restored

    def remember_persistent_view(self, poll_id: str, message_id: int) -> None:
        old_view = self._persistent_views.pop(message_id, None)
        if old_view is not None:
            try:
                self.bot.remove_view(old_view, message_id=message_id)
            except Exception:
                pass

        view = SuggestionPollView(self, poll_id)
        self.bot.add_view(view, message_id=message_id)
        self._persistent_views[message_id] = view

    def restore_tiebreak_views(self) -> int:
        restored = 0

        for poll_id, poll in self.data.get("polls", {}).items():
            tiebreak = poll.get("tiebreak")
            if (
                poll.get("status") != "tiebreak"
                or not isinstance(tiebreak, dict)
                or tiebreak.get("status") != "open"
            ):
                continue

            message_id = safe_int(tiebreak.get("message_id"))
            if not message_id:
                continue

            view = TieBreakView(self, str(poll_id))
            try:
                self.bot.add_view(view, message_id=message_id)
            except Exception as exc:
                warn(
                    f"Could not restore suggestion tie-break {poll_id} "
                    f"for message {message_id}: {exc!r}"
                )
                continue

            self._tiebreak_views[message_id] = view
            restored += 1

        return restored

    def remember_tiebreak_view(self, poll_id: str, message_id: int) -> None:
        old_view = self._tiebreak_views.pop(message_id, None)
        if old_view is not None:
            try:
                self.bot.remove_view(old_view, message_id=message_id)
            except Exception:
                pass

        view = TieBreakView(self, poll_id)
        self.bot.add_view(view, message_id=message_id)
        self._tiebreak_views[message_id] = view

    async def interaction_allowed(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not interaction.channel:
            return False

        settings = getattr(self.bot, "settings", None)
        if settings is None or not hasattr(settings, "is_feature_allowed"):
            return False

        try:
            return bool(
                settings.is_feature_allowed(
                    interaction.guild.id,
                    interaction.channel.id,
                    FEATURE_KEY,
                )
            )
        except Exception:
            return False

    async def require_feature_channel(self, interaction: discord.Interaction) -> bool:
        if await self.interaction_allowed(interaction):
            return True

        await interaction.response.send_message(
            "Suggestion polls are not enabled in this channel yet. "
            "Use the existing feature channel setup for `suggestion_poll`.",
            ephemeral=True,
        )
        return False

    def can_manage_previous_themes(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        permissions = member.guild_permissions
        return bool(permissions.administrator or permissions.manage_guild)

    def previous_themes(self, guild_id: int) -> List[Dict[str, Any]]:
        raw = load_guild_json(
            int(guild_id),
            PREVIOUS_THEMES_FILENAME,
            {"themes": []},
        )
        themes = raw.get("themes") if isinstance(raw, dict) else None
        if not isinstance(themes, list):
            return []

        cleaned: List[Dict[str, Any]] = []
        for entry in themes:
            if isinstance(entry, str):
                text = truncate(entry.strip(), MAX_IDEA_LEN)
                if text:
                    cleaned.append({"theme": text})
                continue
            if not isinstance(entry, dict):
                continue
            text = truncate(str(entry.get("theme") or entry.get("text") or "").strip(), MAX_IDEA_LEN)
            if not text:
                continue
            cleaned.append(
                {
                    "theme": text,
                    "note": truncate(str(entry.get("note") or "").strip(), 120),
                    "added_by": safe_int(entry.get("added_by")) or None,
                    "added_at": str(entry.get("added_at") or ""),
                }
            )
        return cleaned

    def save_previous_themes(
        self,
        guild_id: int,
        themes: List[Dict[str, Any]],
    ) -> None:
        save_guild_json(
            int(guild_id),
            PREVIOUS_THEMES_FILENAME,
            {"themes": themes},
        )

    def find_previous_theme_match(
        self,
        guild_id: int,
        theme: str,
    ) -> Optional[Tuple[Dict[str, Any], float]]:
        best: Optional[Tuple[Dict[str, Any], float]] = None
        for entry in self.previous_themes(guild_id):
            score = theme_similarity(theme, str(entry.get("theme") or ""))
            if best is None or score > best[1]:
                best = (entry, score)
        if best and best[1] >= THEME_SIMILARITY_THRESHOLD:
            return best
        return None

    def find_current_idea_match(
        self,
        poll: Dict[str, Any],
        theme: str,
    ) -> Optional[Tuple[int, Dict[str, Any], float]]:
        best: Optional[Tuple[int, Dict[str, Any], float]] = None
        for idea_no, idea in self.sorted_ideas(poll):
            score = theme_similarity(theme, str(idea.get("text") or ""))
            if best is None or score > best[2]:
                best = (idea_no, idea, score)
        if best and best[2] >= THEME_SIMILARITY_THRESHOLD:
            return best
        return None

    def build_previous_themes_embed(self, guild_id: int) -> discord.Embed:
        themes = self.previous_themes(guild_id)
        embed = discord.Embed(
            title="🕘 Previous Winning Themes",
            colour=discord.Colour.blurple(),
        )
        if not themes:
            embed.description = (
                "No previous winners have been added yet. "
                "Server staff can add the old winners with `/suggestion previous_add`."
            )
            return embed

        lines: List[str] = []
        # Most recently entered themes first.
        for index, entry in reversed(list(enumerate(themes, start=1))):
            text = truncate(str(entry.get("theme") or ""), 100)
            note = truncate(str(entry.get("note") or ""), 70)
            line = f"**{index}.** {text}"
            if note:
                line += f" — {note}"
            lines.append(line)

        # Discord embed descriptions cap at 4096 chars.
        rendered = "\n".join(lines)
        embed.description = truncate(rendered, 3900)
        embed.set_footer(text=f"{len(themes)} previous winning theme(s)")
        return embed

    def new_poll_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def get_poll(self, poll_id: str) -> Optional[Dict[str, Any]]:
        return self.data.get("polls", {}).get(str(poll_id))

    def get_open_poll_for_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        matches: List[Tuple[str, Dict[str, Any]]] = []

        for poll_id, poll in self.data.get("polls", {}).items():
            if (
                safe_int(poll.get("guild_id")) == guild_id
                and safe_int(poll.get("channel_id")) == channel_id
                and poll.get("status") == "open"
            ):
                matches.append((str(poll_id), poll))

        if not matches:
            return None

        matches.sort(
            key=lambda item: safe_int(item[1].get("created_ts")),
            reverse=True,
        )
        return matches[0]

    def get_active_poll_for_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        matches: List[Tuple[str, Dict[str, Any]]] = []

        for poll_id, poll in self.data.get("polls", {}).items():
            if (
                safe_int(poll.get("guild_id")) == guild_id
                and safe_int(poll.get("channel_id")) == channel_id
                and poll.get("status") in {"open", "tiebreak"}
            ):
                matches.append((str(poll_id), poll))

        if not matches:
            return None

        matches.sort(
            key=lambda item: safe_int(item[1].get("created_ts")),
            reverse=True,
        )
        return matches[0]

    async def poll_id_from_message(
        self,
        interaction: discord.Interaction,
    ) -> Optional[str]:
        if not interaction.message:
            return None

        message_id = interaction.message.id
        for poll_id, poll in self.data.get("polls", {}).items():
            if safe_int(poll.get("message_id")) == message_id:
                return str(poll_id)
        return None

    async def tiebreak_poll_id_from_message(
        self,
        interaction: discord.Interaction,
    ) -> Optional[str]:
        if not interaction.message:
            return None

        message_id = interaction.message.id
        for poll_id, poll in self.data.get("polls", {}).items():
            tiebreak = poll.get("tiebreak")
            if (
                isinstance(tiebreak, dict)
                and safe_int(tiebreak.get("message_id")) == message_id
            ):
                return str(poll_id)
        return None

    def sorted_ideas(
        self,
        poll: Dict[str, Any],
    ) -> List[Tuple[int, Dict[str, Any]]]:
        rows = [
            (safe_int(idea_no), idea)
            for idea_no, idea in poll.get("ideas", {}).items()
            if isinstance(idea, dict)
        ]
        rows.sort(key=lambda item: item[0])
        return rows

    def ranked_ideas(
        self,
        poll: Dict[str, Any],
    ) -> List[Tuple[int, Dict[str, Any], int]]:
        rows = [
            (idea_no, idea, len(idea.get("voters", [])))
            for idea_no, idea in self.sorted_ideas(poll)
        ]
        rows.sort(key=lambda item: (-item[2], item[0]))
        return rows

    def shortlist(
        self,
        poll: Dict[str, Any],
    ) -> List[Tuple[int, Dict[str, Any], int]]:
        ranked = self.ranked_ideas(poll)
        by_number = {row[0]: row for row in ranked}

        final_shortlist = poll.get("final_shortlist")
        if isinstance(final_shortlist, list):
            return [
                by_number[idea_no]
                for idea_no in final_shortlist
                if idea_no in by_number
            ]

        size = max(1, safe_int(poll.get("shortlist_size"), 4))
        positive = [row for row in ranked if row[2] > 0]
        return positive[:size]

    def shortlist_plan(self, poll: Dict[str, Any]) -> Dict[str, Any]:
        size = max(1, safe_int(poll.get("shortlist_size"), 4))
        positive = [row for row in self.ranked_ideas(poll) if row[2] > 0]
        target_size = min(size, len(positive))

        if target_size == 0:
            return {
                "needs_tiebreak": False,
                "final_idea_nos": [],
                "locked_idea_nos": [],
                "candidate_idea_nos": [],
                "slots_remaining": 0,
            }

        if len(positive) <= target_size:
            return {
                "needs_tiebreak": False,
                "final_idea_nos": [row[0] for row in positive],
                "locked_idea_nos": [row[0] for row in positive],
                "candidate_idea_nos": [],
                "slots_remaining": 0,
            }

        cutoff_votes = positive[target_size - 1][2]
        locked = [row[0] for row in positive if row[2] > cutoff_votes]
        tied = [row[0] for row in positive if row[2] == cutoff_votes]
        slots_remaining = target_size - len(locked)

        if len(tied) <= slots_remaining:
            final_idea_nos = [row[0] for row in positive[:target_size]]
            return {
                "needs_tiebreak": False,
                "final_idea_nos": final_idea_nos,
                "locked_idea_nos": final_idea_nos,
                "candidate_idea_nos": [],
                "slots_remaining": 0,
            }

        return {
            "needs_tiebreak": True,
            "final_idea_nos": [],
            "locked_idea_nos": locked,
            "candidate_idea_nos": tied,
            "slots_remaining": slots_remaining,
        }

    def prepare_poll_finalisation(self, poll: Dict[str, Any]) -> bool:
        plan = self.shortlist_plan(poll)
        if not plan["needs_tiebreak"]:
            poll["status"] = "closed"
            poll["final_shortlist"] = plan["final_idea_nos"]
            poll["finalized_ts"] = now_ts()
            poll.pop("tiebreak", None)
            return False

        candidates = plan["candidate_idea_nos"]
        poll["status"] = "tiebreak"
        poll.pop("final_shortlist", None)
        poll.pop("final_message_id", None)
        poll["tiebreak"] = {
            "status": "open",
            "round": 1,
            "slots_remaining": plan["slots_remaining"],
            "locked_idea_nos": plan["locked_idea_nos"],
            "candidate_idea_nos": candidates,
            "votes": {str(idea_no): [] for idea_no in candidates},
            "end_ts": now_ts() + TIEBREAK_DURATION_SECONDS,
            "message_id": None,
        }
        return True

    def build_embed(
        self,
        poll_id: str,
        poll: Dict[str, Any],
        final: bool = False,
    ) -> discord.Embed:
        status = poll.get("status", "open")
        title = poll.get("title") or "WoS PFP Theme Suggestions"

        if final or status == "closed":
            embed_title = f"🏁 Closed: {title}"
            colour = discord.Colour.gold()
        elif status == "tiebreak":
            embed_title = f"⚖️ Tie-break: {title}"
            colour = discord.Colour.orange()
        elif status == "cancelled":
            embed_title = f"Cancelled: {title}"
            colour = discord.Colour.dark_grey()
        else:
            embed_title = f"📸 {title}"
            colour = discord.Colour.blurple()

        embed = discord.Embed(
            title=embed_title,
            description=(
                poll.get("description")
                or "Drop WoS profile picture theme ideas, then vote for the ones you want."
            ),
            colour=colour,
            timestamp=datetime.now(timezone.utc),
        )

        if status == "open":
            end_ts = safe_int(poll.get("end_ts"))
            embed.add_field(
                name="Ends",
                value=f"<t:{end_ts}:F>\n<t:{end_ts}:R>",
                inline=True,
            )
        elif status == "tiebreak":
            embed.add_field(name="Status", value="Tie-break in progress", inline=True)
        else:
            embed.add_field(name="Status", value=status.title(), inline=True)

        embed.add_field(name="Poll ID", value=f"`{poll_id}`", inline=True)

        guild_id = safe_int(poll.get("guild_id"))
        previous = self.previous_themes(guild_id) if guild_id else []
        if previous and status == "open":
            recent = previous[-5:]
            embed.add_field(
                name="Previous Winning Themes",
                value="\n".join(
                    f"• {truncate(str(entry.get('theme') or ''), 90)}"
                    for entry in reversed(recent)
                ) + "\n*Use **Previous Themes** below for the full list.*",
                inline=False,
            )

        ideas = self.sorted_ideas(poll)
        if not ideas:
            embed.add_field(
                name="Ideas",
                value="No ideas yet. Use `/suggestion add` or hit **Add Idea**.",
                inline=False,
            )
        else:
            lines: List[str] = []
            for idea_no, idea in ideas[:MAX_VISIBLE_IDEAS]:
                votes = len(idea.get("voters", []))
                vote_word = "vote" if votes == 1 else "votes"
                lines.append(
                    f"**{idea_no}.** {truncate(idea.get('text', ''), 90)} "
                    f"— **{votes}** {vote_word}"
                )

            hidden = len(ideas) - MAX_VISIBLE_IDEAS
            if hidden > 0:
                lines.append(f"...and {hidden} more.")

            embed.add_field(name="Ideas", value="\n".join(lines), inline=False)

        if status == "tiebreak":
            tiebreak = poll.get("tiebreak", {})
            locked_numbers = tiebreak.get("locked_idea_nos", [])
            candidates = tiebreak.get("candidate_idea_nos", [])
            by_number = {row[0]: row for row in self.ranked_ideas(poll)}

            locked_lines = []
            for idea_no in locked_numbers:
                row = by_number.get(safe_int(idea_no))
                if row:
                    locked_lines.append(
                        f"**{row[0]}.** {truncate(row[1].get('text', ''), 120)} "
                        f"— **{row[2]}** {'vote' if row[2] == 1 else 'votes'}"
                    )

            candidate_lines = []
            for idea_no in candidates:
                row = by_number.get(safe_int(idea_no))
                if row:
                    candidate_lines.append(
                        f"**{row[0]}.** {truncate(row[1].get('text', ''), 120)}"
                    )

            if locked_lines:
                embed.add_field(
                    name="Locked In",
                    value="\n".join(locked_lines),
                    inline=False,
                )
            embed.add_field(
                name="Tie-break Required",
                value=(
                    "\n".join(candidate_lines)
                    + f"\n\n**{safe_int(tiebreak.get('slots_remaining'), 1)} "
                    "shortlist spot(s) remain.** Vote in the tie-break message."
                ),
                inline=False,
            )

        if final or status == "closed":
            top = self.shortlist(poll)
            if not top:
                embed.add_field(
                    name="Result",
                    value="No ideas were added.",
                    inline=False,
                )
            else:
                top_lines = []
                for idea_no, idea, votes in top:
                    vote_word = "vote" if votes == 1 else "votes"
                    top_lines.append(
                        f"**{idea_no}.** {truncate(idea.get('text', ''), 120)} "
                        f"— **{votes}** {vote_word}"
                    )
                embed.add_field(
                    name="Winner / Shortlist",
                    value="\n".join(top_lines),
                    inline=False,
                )

        if status == "tiebreak":
            embed.set_footer(text="Use the tie-break message to fill the remaining shortlist spot(s).")
        elif status == "closed" or final:
            embed.set_footer(text="Final shortlist complete.")
        else:
            embed.set_footer(
                text="Use /suggestion add, /suggestion vote, or the buttons below."
            )
        return embed

    def build_tiebreak_embed(
        self,
        poll_id: str,
        poll: Dict[str, Any],
    ) -> discord.Embed:
        tiebreak = poll.get("tiebreak")
        if not isinstance(tiebreak, dict):
            return discord.Embed(
                title="Tie-break unavailable",
                description="This tie-break could not be loaded.",
                colour=discord.Colour.dark_grey(),
            )

        round_no = max(1, safe_int(tiebreak.get("round"), 1))
        slots_remaining = max(1, safe_int(tiebreak.get("slots_remaining"), 1))
        is_open = poll.get("status") == "tiebreak" and tiebreak.get("status") == "open"
        title = poll.get("title") or "WoS PFP Theme Suggestions"

        embed = discord.Embed(
            title=(
                f"⚖️ Tie-break Round {round_no}: {title}"
                if is_open
                else f"🏁 Tie-break Complete: {title}"
            ),
            description=(
                "The main vote tied at the shortlist cutoff. "
                "Each person gets one tie-break vote and may change it before the round closes."
                if is_open
                else "This tie-break round has finished."
            ),
            colour=discord.Colour.orange() if is_open else discord.Colour.gold(),
            timestamp=datetime.now(timezone.utc),
        )

        by_number = {row[0]: row for row in self.ranked_ideas(poll)}

        if not is_open:
            final_lines = []
            for idea_no in poll.get("final_shortlist", []):
                row = by_number.get(safe_int(idea_no))
                if row:
                    final_lines.append(
                        f"**{row[0]}.** {truncate(row[1].get('text', ''), 110)}"
                    )
            embed.add_field(
                name="Final Shortlist",
                value="\n".join(final_lines) or "No positive-vote ideas qualified.",
                inline=False,
            )
            embed.set_footer(text=f"Poll ID: {poll_id} • Final shortlist complete")
            return embed

        locked_lines = []
        for idea_no in tiebreak.get("locked_idea_nos", []):
            row = by_number.get(safe_int(idea_no))
            if row:
                locked_lines.append(
                    f"**{row[0]}.** {truncate(row[1].get('text', ''), 100)}"
                )
        if locked_lines:
            embed.add_field(
                name="Already Locked In",
                value="\n".join(locked_lines),
                inline=False,
            )

        votes = tiebreak.get("votes") if isinstance(tiebreak.get("votes"), dict) else {}
        candidate_rows = []
        for idea_no in tiebreak.get("candidate_idea_nos", []):
            idea_no = safe_int(idea_no)
            row = by_number.get(idea_no)
            if not row:
                continue
            vote_count = len(votes.get(str(idea_no), []))
            candidate_rows.append((idea_no, row[1], vote_count))
        candidate_rows.sort(key=lambda item: (-item[2], item[0]))

        candidate_lines = []
        for idea_no, idea, vote_count in candidate_rows:
            vote_word = "vote" if vote_count == 1 else "votes"
            candidate_lines.append(
                f"**{idea_no}.** {truncate(idea.get('text', ''), 110)} "
                f"— **{vote_count}** {vote_word}"
            )

        embed.add_field(
            name=f"Candidates — {slots_remaining} spot(s) available",
            value="\n".join(candidate_lines) or "No eligible candidates.",
            inline=False,
        )

        end_ts = safe_int(tiebreak.get("end_ts"))
        embed.add_field(
            name="Ends",
            value=f"<t:{end_ts}:F>\n<t:{end_ts}:R>",
            inline=False,
        )
        embed.set_footer(
            text=f"Poll ID: {poll_id} • One vote per person • Use the Vote button"
        )

        return embed

    async def get_poll_channel(
        self,
        poll: Dict[str, Any],
    ) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(safe_int(poll.get("guild_id")))
        if guild is None:
            return None

        channel = guild.get_channel(safe_int(poll.get("channel_id")))
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def update_poll_message(self, poll_id: str) -> None:
        poll = self.get_poll(poll_id)
        if not poll:
            return

        channel = await self.get_poll_channel(poll)
        message_id = safe_int(poll.get("message_id"))
        if channel is None or not message_id:
            return

        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        view: Optional[discord.ui.View]
        if poll.get("status") == "open":
            view = SuggestionPollView(self, poll_id)
        else:
            view = None
            stored_view = self._persistent_views.pop(message_id, None)
            if stored_view is not None:
                try:
                    self.bot.remove_view(stored_view, message_id=message_id)
                except Exception:
                    pass

        await message.edit(embed=self.build_embed(poll_id, poll), view=view)

    async def post_final_result(
        self,
        poll_id: str,
        poll: Dict[str, Any],
    ) -> None:
        channel = await self.get_poll_channel(poll)
        if channel is None:
            return

        final_message_id = safe_int(poll.get("final_message_id"))
        if final_message_id:
            try:
                message = await channel.fetch_message(final_message_id)
                await message.edit(embed=self.build_embed(poll_id, poll, final=True), view=None)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        sent = await channel.send(embed=self.build_embed(poll_id, poll, final=True))
        async with self.lock:
            current = self.get_poll(poll_id)
            if current is not None:
                current["final_message_id"] = sent.id
                self.save_data()

    async def post_tiebreak_message(
        self,
        poll_id: str,
        poll: Dict[str, Any],
    ) -> None:
        channel = await self.get_poll_channel(poll)
        tiebreak = poll.get("tiebreak")
        if channel is None or not isinstance(tiebreak, dict):
            return

        message_id = safe_int(tiebreak.get("message_id"))
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(
                    embed=self.build_tiebreak_embed(poll_id, poll),
                    view=TieBreakView(self, poll_id),
                )
                self.remember_tiebreak_view(poll_id, message.id)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        sent = await channel.send(
            embed=self.build_tiebreak_embed(poll_id, poll),
            view=TieBreakView(self, poll_id),
        )
        async with self.lock:
            current = self.get_poll(poll_id)
            if current is not None:
                current_tiebreak = current.get("tiebreak")
                if isinstance(current_tiebreak, dict):
                    current_tiebreak["message_id"] = sent.id
                    self.save_data()
        self.remember_tiebreak_view(poll_id, sent.id)

    async def update_tiebreak_message(self, poll_id: str) -> None:
        poll = self.get_poll(poll_id)
        if not poll:
            return

        tiebreak = poll.get("tiebreak")
        if not isinstance(tiebreak, dict):
            return

        channel = await self.get_poll_channel(poll)
        message_id = safe_int(tiebreak.get("message_id"))
        if channel is None or not message_id:
            return

        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        is_open = poll.get("status") == "tiebreak" and tiebreak.get("status") == "open"
        view: Optional[discord.ui.View] = TieBreakView(self, poll_id) if is_open else None

        if not is_open:
            stored_view = self._tiebreak_views.pop(message_id, None)
            if stored_view is not None:
                try:
                    self.bot.remove_view(stored_view, message_id=message_id)
                except Exception:
                    pass

        await message.edit(embed=self.build_tiebreak_embed(poll_id, poll), view=view)
        if is_open:
            self.remember_tiebreak_view(poll_id, message_id)

    async def close_poll(self, poll_id: str, post_result: bool = True) -> bool:
        async with self.lock:
            poll = self.get_poll(poll_id)
            if not poll or poll.get("status") != "open":
                return False

            poll["closed_ts"] = now_ts()
            needs_tiebreak = self.prepare_poll_finalisation(poll)
            self.save_data()

        await self.update_poll_message(poll_id)
        if needs_tiebreak:
            await self.post_tiebreak_message(poll_id, poll)
        elif post_result:
            await self.post_final_result(poll_id, poll)
        return True

    async def tiebreak_vote_core(
        self,
        poll_id: str,
        user_id: int,
        idea_number: int,
    ) -> Tuple[bool, str]:
        if idea_number <= 0:
            return False, "Use one of the idea numbers shown in the tie-break."

        async with self.lock:
            poll = self.get_poll(poll_id)
            if not poll or poll.get("status") != "tiebreak":
                return False, "That tie-break is closed."

            tiebreak = poll.get("tiebreak")
            if not isinstance(tiebreak, dict) or tiebreak.get("status") != "open":
                return False, "That tie-break is closed."

            candidates = [safe_int(value) for value in tiebreak.get("candidate_idea_nos", [])]
            if idea_number not in candidates:
                return False, "That idea is not in the current tie-break round."

            votes = tiebreak.setdefault("votes", {})
            for candidate in candidates:
                voters = votes.setdefault(str(candidate), [])
                if user_id in voters:
                    voters.remove(user_id)

            votes.setdefault(str(idea_number), []).append(user_id)
            self.save_data()

        await self.update_tiebreak_message(poll_id)
        return True, f"Your tie-break vote is now on idea **{idea_number}**."

    async def tiebreak_vote_from_ui(
        self,
        interaction: discord.Interaction,
        poll_id: str,
        idea_number: int,
    ) -> None:
        if not await self.interaction_allowed(interaction):
            await interaction.response.send_message(
                "Suggestion polls are not enabled in this channel.",
                ephemeral=True,
            )
            return

        _, message = await self.tiebreak_vote_core(
            poll_id,
            interaction.user.id,
            idea_number,
        )
        await interaction.response.send_message(message, ephemeral=True)

    async def refresh_tiebreak_from_ui(
        self,
        interaction: discord.Interaction,
        poll_id: str,
    ) -> None:
        if not await self.interaction_allowed(interaction):
            await interaction.response.send_message(
                "Suggestion polls are not enabled in this channel.",
                ephemeral=True,
            )
            return

        await self.update_tiebreak_message(poll_id)
        await interaction.response.send_message("Tie-break refreshed.", ephemeral=True)

    async def resolve_tiebreak(self, poll_id: str) -> bool:
        async with self.lock:
            poll = self.get_poll(poll_id)
            if not poll or poll.get("status") != "tiebreak":
                return False

            tiebreak = poll.get("tiebreak")
            if not isinstance(tiebreak, dict) or tiebreak.get("status") != "open":
                return False

            candidates = [
                safe_int(value)
                for value in tiebreak.get("candidate_idea_nos", [])
                if safe_int(value) > 0
            ]
            slots_remaining = max(1, safe_int(tiebreak.get("slots_remaining"), 1))
            votes = tiebreak.get("votes") if isinstance(tiebreak.get("votes"), dict) else {}
            ranked = sorted(
                (
                    (idea_no, len(votes.get(str(idea_no), [])))
                    for idea_no in candidates
                ),
                key=lambda item: (-item[1], item[0]),
            )

            locked = [
                safe_int(value)
                for value in tiebreak.get("locked_idea_nos", [])
                if safe_int(value) > 0
            ]

            if len(ranked) <= slots_remaining:
                selected = [idea_no for idea_no, _ in ranked]
                poll["final_shortlist"] = locked + selected
                poll["status"] = "closed"
                poll["finalized_ts"] = now_ts()
                tiebreak["status"] = "closed"
                finished = True
            else:
                cutoff_votes = ranked[slots_remaining - 1][1]
                definite = [idea_no for idea_no, count in ranked if count > cutoff_votes]
                tied = [idea_no for idea_no, count in ranked if count == cutoff_votes]
                remaining_after_definite = slots_remaining - len(definite)
                locked.extend(definite)

                if len(tied) <= remaining_after_definite:
                    poll["final_shortlist"] = locked + tied
                    poll["status"] = "closed"
                    poll["finalized_ts"] = now_ts()
                    tiebreak["locked_idea_nos"] = locked + tied
                    tiebreak["status"] = "closed"
                    finished = True
                else:
                    tiebreak["round"] = max(1, safe_int(tiebreak.get("round"), 1)) + 1
                    tiebreak["slots_remaining"] = remaining_after_definite
                    tiebreak["locked_idea_nos"] = locked
                    tiebreak["candidate_idea_nos"] = tied
                    tiebreak["votes"] = {str(idea_no): [] for idea_no in tied}
                    tiebreak["end_ts"] = now_ts() + TIEBREAK_DURATION_SECONDS
                    tiebreak["status"] = "open"
                    finished = False

            self.save_data()

        await self.update_poll_message(poll_id)
        await self.update_tiebreak_message(poll_id)
        if finished:
            await self.post_final_result(poll_id, poll)
        return True

    async def remove_untracked_final_result(
        self,
        poll_id: str,
        poll: Dict[str, Any],
    ) -> None:
        channel = await self.get_poll_channel(poll)
        if channel is None or self.bot.user is None:
            return

        original_message_id = safe_int(poll.get("message_id"))
        expected_title = f"🏁 Closed: {poll.get('title') or 'WoS PFP Theme Suggestions'}"
        expected_poll_id = f"`{poll_id}`"

        try:
            async for message in channel.history(limit=50):
                if message.id == original_message_id:
                    continue
                if message.author.id != self.bot.user.id:
                    continue

                for embed in message.embeds:
                    if embed.title != expected_title:
                        continue
                    has_poll_id = any(
                        field.name == "Poll ID" and field.value == expected_poll_id
                        for field in embed.fields
                    )
                    if not has_poll_id:
                        continue
                    try:
                        await message.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                    break
        except (discord.Forbidden, discord.HTTPException):
            return

    async def recover_recent_unresolved_polls(self) -> None:
        to_update: List[Tuple[str, Dict[str, Any], bool]] = []
        cutoff = now_ts() - RECOVER_CLOSED_POLLS_WITHIN_SECONDS

        async with self.lock:
            changed = False
            for poll_id, poll in self.data.get("polls", {}).items():
                if poll.get("status") != "closed":
                    continue
                if isinstance(poll.get("final_shortlist"), list):
                    continue
                if isinstance(poll.get("tiebreak"), dict):
                    continue
                if safe_int(poll.get("closed_ts")) < cutoff:
                    continue

                needs_tiebreak = self.prepare_poll_finalisation(poll)
                to_update.append((str(poll_id), poll, needs_tiebreak))
                changed = True

            if changed:
                self.save_data()

        for poll_id, poll, needs_tiebreak in to_update:
            if needs_tiebreak:
                await self.remove_untracked_final_result(poll_id, poll)
            await self.update_poll_message(poll_id)
            if needs_tiebreak:
                await self.post_tiebreak_message(poll_id, poll)


    @tasks.loop(minutes=5)
    async def poll_watcher(self) -> None:
        due_polls = [
            poll_id
            for poll_id, poll in self.data.get("polls", {}).items()
            if poll.get("status") == "open"
            and safe_int(poll.get("end_ts")) <= now_ts()
        ]
        due_tiebreaks = [
            poll_id
            for poll_id, poll in self.data.get("polls", {}).items()
            if poll.get("status") == "tiebreak"
            and isinstance(poll.get("tiebreak"), dict)
            and poll["tiebreak"].get("status") == "open"
            and safe_int(poll["tiebreak"].get("end_ts")) <= now_ts()
        ]
        missing_tiebreak_messages = [
            (str(poll_id), poll)
            for poll_id, poll in self.data.get("polls", {}).items()
            if poll.get("status") == "tiebreak"
            and isinstance(poll.get("tiebreak"), dict)
            and poll["tiebreak"].get("status") == "open"
            and not safe_int(poll["tiebreak"].get("message_id"))
        ]

        for poll_id in due_polls:
            await self.close_poll(str(poll_id), post_result=True)
        for poll_id in due_tiebreaks:
            await self.resolve_tiebreak(str(poll_id))
        for poll_id, poll in missing_tiebreak_messages:
            await self.post_tiebreak_message(poll_id, poll)

    @poll_watcher.before_loop
    async def before_poll_watcher(self) -> None:
        await self.bot.wait_until_ready()
        if not self._recovery_complete:
            await self.recover_recent_unresolved_polls()
            self._recovery_complete = True

    async def add_idea_core(
        self,
        poll_id: str,
        user_id: int,
        idea_text: str,
    ) -> Tuple[bool, str]:
        idea_text = truncate(idea_text.strip(), MAX_IDEA_LEN)
        if not idea_text:
            return False, "Idea cannot be empty."

        async with self.lock:
            poll = self.get_poll(poll_id)
            if not poll:
                return False, "Poll not found."
            if poll.get("status") != "open":
                return False, "That poll is closed."

            current_match = self.find_current_idea_match(poll, idea_text)
            if current_match:
                match_no, match_idea, score = current_match
                existing_text = truncate(str(match_idea.get("text") or ""), 120)
                if score >= 0.995:
                    return False, f"That theme is already idea **{match_no}**: **{existing_text}**."
                return False, (
                    f"⚠️ That looks very similar to idea **{match_no}** already in this poll: "
                    f"**{existing_text}**. If you mean something genuinely different, make the difference clearer."
                )

            guild_id = safe_int(poll.get("guild_id"))
            previous_match = self.find_previous_theme_match(guild_id, idea_text) if guild_id else None
            if previous_match:
                entry, score = previous_match
                old_theme = truncate(str(entry.get("theme") or ""), 120)
                if score >= 0.995:
                    return False, (
                        f"⚠️ **{old_theme}** has already been a winning theme before. "
                        "Pick something different so we don't repeat a previous theme."
                    )
                return False, (
                    f"⚠️ That looks very similar to a previous winning theme: **{old_theme}**. "
                    "If you mean a genuinely different theme, add enough detail to make that clear."
                )

            idea_no = str(max(1, safe_int(poll.get("next_idea_no"), 1)))
            poll.setdefault("ideas", {})[idea_no] = {
                "text": idea_text,
                "author_id": user_id,
                "created_at": iso_now(),
                "voters": [],
            }
            poll["next_idea_no"] = safe_int(idea_no) + 1
            self.save_data()

        await self.update_poll_message(poll_id)
        return True, f"Added idea **{idea_no}**."

    async def vote_core(
        self,
        poll_id: str,
        user_id: int,
        idea_number: int,
    ) -> Tuple[bool, str]:
        if idea_number <= 0:
            return False, "Use the idea number from the poll."

        async with self.lock:
            poll = self.get_poll(poll_id)
            if not poll:
                return False, "Poll not found."
            if poll.get("status") != "open":
                return False, "That poll is closed."

            ideas = poll.get("ideas", {})
            idea_key = str(idea_number)
            if idea_key not in ideas:
                return False, "That idea number does not exist."

            if not bool(poll.get("allow_multi_vote", True)):
                for idea in ideas.values():
                    voters = idea.setdefault("voters", [])
                    if user_id in voters:
                        voters.remove(user_id)

            voters = ideas[idea_key].setdefault("voters", [])
            if user_id in voters:
                return False, f"You already voted for idea **{idea_number}**."

            voters.append(user_id)
            self.save_data()

        await self.update_poll_message(poll_id)
        return True, f"Voted for idea **{idea_number}**."

    async def remove_vote_core(
        self,
        poll_id: str,
        user_id: int,
        idea_number: int,
    ) -> Tuple[bool, str]:
        if idea_number <= 0:
            return False, "Use the idea number from the poll."

        async with self.lock:
            poll = self.get_poll(poll_id)
            if not poll:
                return False, "Poll not found."
            if poll.get("status") != "open":
                return False, "That poll is closed."

            ideas = poll.get("ideas", {})
            idea_key = str(idea_number)
            if idea_key not in ideas:
                return False, "That idea number does not exist."

            voters = ideas[idea_key].setdefault("voters", [])
            if user_id not in voters:
                return False, f"You have not voted for idea **{idea_number}**."

            voters.remove(user_id)
            self.save_data()

        await self.update_poll_message(poll_id)
        return True, f"Removed your vote from idea **{idea_number}**."

    async def add_idea_from_ui(
        self,
        interaction: discord.Interaction,
        poll_id: str,
        idea_text: str,
    ) -> None:
        if not await self.interaction_allowed(interaction):
            await interaction.response.send_message(
                "Suggestion polls are not enabled in this channel.",
                ephemeral=True,
            )
            return

        _, message = await self.add_idea_core(
            poll_id,
            interaction.user.id,
            idea_text,
        )
        await interaction.response.send_message(message, ephemeral=True)

    async def vote_from_ui(
        self,
        interaction: discord.Interaction,
        poll_id: str,
        idea_number: int,
    ) -> None:
        if not await self.interaction_allowed(interaction):
            await interaction.response.send_message(
                "Suggestion polls are not enabled in this channel.",
                ephemeral=True,
            )
            return

        _, message = await self.vote_core(
            poll_id,
            interaction.user.id,
            idea_number,
        )
        await interaction.response.send_message(message, ephemeral=True)

    async def refresh_from_ui(
        self,
        interaction: discord.Interaction,
        poll_id: str,
    ) -> None:
        if not await self.interaction_allowed(interaction):
            await interaction.response.send_message(
                "Suggestion polls are not enabled in this channel.",
                ephemeral=True,
            )
            return

        await self.update_poll_message(poll_id)
        await interaction.response.send_message("Refreshed.", ephemeral=True)


suggestion_group = app_commands.Group(
    name="suggestion",
    description="WoS PFP suggestion polls",
)


async def get_suggestion_cog(
    interaction: discord.Interaction,
) -> Optional[SuggestionPollCog]:
    cog = interaction.client.get_cog("SuggestionPollCog")
    if isinstance(cog, SuggestionPollCog):
        return cog

    await interaction.response.send_message(
        "Suggestion poll cog is not loaded.",
        ephemeral=True,
    )
    return None


@suggestion_group.command(name="help", description="Show how suggestion polls work.")
async def suggestion_help(interaction: discord.Interaction) -> None:
    cog = await get_suggestion_cog(interaction)
    if not cog or not await cog.require_feature_channel(interaction):
        return

    embed = discord.Embed(
        title="📸 WoS PFP Suggestion Polls",
        description=(
            "Use this to collect profile picture theme ideas, vote on them, "
            "then pick a winner or shortlist."
        ),
        colour=discord.Colour.blurple(),
    )
    embed.add_field(
        name="Commands",
        value=(
            "`/suggestion start` - start a new suggestion poll\n"
            "`/suggestion add` - add an idea\n"
            "`/suggestion vote` - vote for an idea number\n"
            "`/suggestion remove_vote` - remove your vote\n"
            "`/suggestion results` - show current results\n"
            "`/suggestion previous` - show previous winning themes"
        ),
        inline=False,
    )
    embed.add_field(
        name="Default",
        value="A poll can run for 7 days, then it auto-posts the winner/shortlist.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@suggestion_group.command(name="previous", description="Show previous winning PFP themes.")
async def suggestion_previous(interaction: discord.Interaction) -> None:
    cog = await get_suggestion_cog(interaction)
    if not cog or not await cog.require_feature_channel(interaction):
        return
    if not interaction.guild_id:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=cog.build_previous_themes_embed(int(interaction.guild_id)),
        ephemeral=True,
    )


@suggestion_group.command(
    name="previous_add",
    description="Staff: add a previous winning PFP theme.",
)
@app_commands.describe(
    theme="Previous winning theme",
    note="Optional note, date, server, or winner detail",
)
async def suggestion_previous_add(
    interaction: discord.Interaction,
    theme: str,
    note: Optional[str] = None,
) -> None:
    cog = await get_suggestion_cog(interaction)
    if not cog or not await cog.require_feature_channel(interaction):
        return
    if not interaction.guild_id:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    if not cog.can_manage_previous_themes(interaction):
        await interaction.response.send_message(
            "You need **Manage Server** or **Administrator** to edit previous themes.",
            ephemeral=True,
        )
        return

    theme = truncate(theme.strip(), MAX_IDEA_LEN)
    if not theme:
        await interaction.response.send_message("Theme cannot be empty.", ephemeral=True)
        return

    guild_id = int(interaction.guild_id)
    existing_match = cog.find_previous_theme_match(guild_id, theme)
    if existing_match:
        entry, _score = existing_match
        await interaction.response.send_message(
            f"That is already in Previous Themes as **{entry.get('theme')}**.",
            ephemeral=True,
        )
        return

    themes = cog.previous_themes(guild_id)
    themes.append(
        {
            "theme": theme,
            "note": truncate((note or "").strip(), 120),
            "added_by": interaction.user.id,
            "added_at": iso_now(),
        }
    )
    cog.save_previous_themes(guild_id, themes)

    # Refresh any open poll in this channel so the recent-history field appears immediately.
    if interaction.channel_id:
        active = cog.get_open_poll_for_channel(guild_id, int(interaction.channel_id))
        if active:
            await cog.update_poll_message(active[0])

    await interaction.response.send_message(
        f"Added previous winning theme: **{theme}**.",
        ephemeral=True,
    )


@suggestion_group.command(
    name="previous_remove",
    description="Staff: remove a theme from the previous-winners list.",
)
@app_commands.describe(theme="Theme to remove")
async def suggestion_previous_remove(
    interaction: discord.Interaction,
    theme: str,
) -> None:
    cog = await get_suggestion_cog(interaction)
    if not cog or not await cog.require_feature_channel(interaction):
        return
    if not interaction.guild_id:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    if not cog.can_manage_previous_themes(interaction):
        await interaction.response.send_message(
            "You need **Manage Server** or **Administrator** to edit previous themes.",
            ephemeral=True,
        )
        return

    guild_id = int(interaction.guild_id)
    themes = cog.previous_themes(guild_id)
    target = normalise_theme_text(theme)
    remove_index = None
    for index, entry in enumerate(themes):
        if normalise_theme_text(str(entry.get("theme") or "")) == target:
            remove_index = index
            break

    if remove_index is None:
        await interaction.response.send_message(
            "I couldn't find that exact theme in Previous Themes.",
            ephemeral=True,
        )
        return

    removed = themes.pop(remove_index)
    cog.save_previous_themes(guild_id, themes)
    await interaction.response.send_message(
        f"Removed previous theme: **{removed.get('theme')}**.",
        ephemeral=True,
    )


@suggestion_group.command(name="start", description="Start a WoS PFP suggestion poll.")
@app_commands.describe(
    title="Poll title",
    duration_days="How many days the poll should stay open",
    shortlist_size="How many top ideas to keep at the end",
    allow_multi_vote="Can people vote for more than one idea?",
    description="Optional description",
)
async def suggestion_start(
    interaction: discord.Interaction,
    title: Optional[str] = "WoS PFP Theme Suggestions",
    duration_days: app_commands.Range[int, 1, 30] = 7,
    shortlist_size: app_commands.Range[int, 1, 10] = 4,
    allow_multi_vote: bool = True,
    description: Optional[str] = None,
) -> None:
    cog = await get_suggestion_cog(interaction)
    if not cog or not await cog.require_feature_channel(interaction):
        return

    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "Use this inside a server text channel.",
            ephemeral=True,
        )
        return

    existing = cog.get_active_poll_for_channel(
        interaction.guild.id,
        interaction.channel.id,
    )
    if existing:
        poll_id, _ = existing
        await interaction.response.send_message(
            f"There is already an active suggestion poll or tie-break in this channel: `{poll_id}`.",
            ephemeral=True,
        )
        return

    poll_id = cog.new_poll_id()
    poll: Dict[str, Any] = {
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel.id,
        "message_id": None,
        "title": title or "WoS PFP Theme Suggestions",
        "description": (
            description
            or "Drop WoS profile picture theme ideas, then vote for the ones you want."
        ),
        "created_by": interaction.user.id,
        "created_at": iso_now(),
        "created_ts": now_ts(),
        "end_ts": now_ts() + int(duration_days) * 86400,
        "status": "open",
        "shortlist_size": int(shortlist_size),
        "allow_multi_vote": bool(allow_multi_vote),
        "ideas": {},
        "next_idea_no": 1,
    }

    async with cog.lock:
        cog.data.setdefault("polls", {})[poll_id] = poll
        cog.save_data()

    await interaction.response.send_message(
        embed=cog.build_embed(poll_id, poll),
        view=SuggestionPollView(cog, poll_id),
    )
    sent = await interaction.original_response()

    async with cog.lock:
        poll["message_id"] = sent.id
        cog.save_data()

    cog.remember_persistent_view(poll_id, sent.id)


@suggestion_group.command(name="add", description="Add an idea to the open suggestion poll.")
@app_commands.describe(idea="Your WoS PFP theme idea")
async def suggestion_add(
    interaction: discord.Interaction,
    idea: str,
) -> None:
    cog = await get_suggestion_cog(interaction)
    if not cog or not await cog.require_feature_channel(interaction):
        return
    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message(
            "Use this inside a server channel.",
            ephemeral=True,
        )
        return

    active = cog.get_open_poll_for_channel(
        interaction.guild.id,
        interaction.channel.id,
    )
    if not active:
        await interaction.response.send_message(
            "No open suggestion poll in this channel.",
            ephemeral=True,
        )
        return

    poll_id, _ = active
    _, message = await cog.add_idea_core(poll_id, interaction.user.id, idea)
    await interaction.response.send_message(message, ephemeral=True)


@suggestion_group.command(name="vote", description="Vote for an idea number.")
@app_commands.describe(idea_number="The idea number shown on the poll")
async def suggestion_vote(
    interaction: discord.Interaction,
    idea_number: int,
) -> None:
    cog = await get_suggestion_cog(interaction)
    if not cog or not await cog.require_feature_channel(interaction):
        return
    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message(
            "Use this inside a server channel.",
            ephemeral=True,
        )
        return

    active = cog.get_open_poll_for_channel(
        interaction.guild.id,
        interaction.channel.id,
    )
    if not active:
        await interaction.response.send_message(
            "No open suggestion poll in this channel.",
            ephemeral=True,
        )
        return

    poll_id, _ = active
    _, message = await cog.vote_core(
        poll_id,
        interaction.user.id,
        idea_number,
    )
    await interaction.response.send_message(message, ephemeral=True)


@suggestion_group.command(
    name="remove_vote",
    description="Remove your vote from an idea.",
)
@app_commands.describe(idea_number="The idea number shown on the poll")
async def suggestion_remove_vote(
    interaction: discord.Interaction,
    idea_number: int,
) -> None:
    cog = await get_suggestion_cog(interaction)
    if not cog or not await cog.require_feature_channel(interaction):
        return
    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message(
            "Use this inside a server channel.",
            ephemeral=True,
        )
        return

    active = cog.get_open_poll_for_channel(
        interaction.guild.id,
        interaction.channel.id,
    )
    if not active:
        await interaction.response.send_message(
            "No open suggestion poll in this channel.",
            ephemeral=True,
        )
        return

    poll_id, _ = active
    _, message = await cog.remove_vote_core(
        poll_id,
        interaction.user.id,
        idea_number,
    )
    await interaction.response.send_message(message, ephemeral=True)


@suggestion_group.command(
    name="results",
    description="Show current suggestion poll results.",
)
async def suggestion_results(interaction: discord.Interaction) -> None:
    cog = await get_suggestion_cog(interaction)
    if not cog or not await cog.require_feature_channel(interaction):
        return
    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message(
            "Use this inside a server channel.",
            ephemeral=True,
        )
        return

    active = cog.get_open_poll_for_channel(
        interaction.guild.id,
        interaction.channel.id,
    )
    if not active:
        await interaction.response.send_message(
            "No open suggestion poll in this channel.",
            ephemeral=True,
        )
        return

    poll_id, poll = active
    ranked = cog.ranked_ideas(poll)
    if not ranked:
        await interaction.response.send_message("No ideas yet.", ephemeral=True)
        return

    lines = []
    for idea_no, idea, votes in ranked[:10]:
        vote_word = "vote" if votes == 1 else "votes"
        lines.append(
            f"**{idea_no}.** {truncate(idea.get('text', ''), 100)} "
            f"— **{votes}** {vote_word}"
        )

    embed = discord.Embed(
        title="Current Suggestion Results",
        description="\n".join(lines),
        colour=discord.Colour.blurple(),
    )
    embed.set_footer(text=f"Poll ID: {poll_id}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    cog = SuggestionPollCog(bot)
    await bot.add_cog(cog)

    bind_group_public(suggestion_group, bot, include_admin=True)
    try:
        bot.tree.add_command(suggestion_group)
    except app_commands.CommandAlreadyRegistered:
        pass
