from __future__ import annotations

import asyncio
import json
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
SUGGESTION_POLLS_FILENAME = "suggestion_polls.json"
MAX_IDEA_LEN = 180
MAX_VISIBLE_IDEAS = 20


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


class SuggestionPollCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.data: Dict[str, Any] = {"polls": {}}
        self._persistent_views: Dict[int, SuggestionPollView] = {}
        self.load_data()

    async def cog_load(self) -> None:
        restored = self.restore_persistent_views()
        ok(f"Restored {restored} open suggestion poll button view(s)")
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
        poll.setdefault("shortlist_size", 3)
        poll.setdefault("allow_multi_vote", True)
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
        if not ranked:
            return []

        size = max(1, safe_int(poll.get("shortlist_size"), 3))
        base = ranked[:size]
        if len(ranked) <= size:
            return base

        cutoff_votes = base[-1][2]
        extra_ties = [
            row
            for row in ranked[size:]
            if cutoff_votes > 0 and row[2] == cutoff_votes
        ]
        return base + extra_ties

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
        else:
            embed.add_field(name="Status", value=status.title(), inline=True)

        embed.add_field(name="Poll ID", value=f"`{poll_id}`", inline=True)

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

        embed.set_footer(
            text="Use /suggestion add, /suggestion vote, or the buttons below."
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
        if channel is not None:
            await channel.send(embed=self.build_embed(poll_id, poll, final=True))

    async def close_poll(self, poll_id: str, post_result: bool = True) -> bool:
        async with self.lock:
            poll = self.get_poll(poll_id)
            if not poll or poll.get("status") != "open":
                return False

            poll["status"] = "closed"
            poll["closed_ts"] = now_ts()
            self.save_data()

        await self.update_poll_message(poll_id)
        if post_result:
            await self.post_final_result(poll_id, poll)
        return True

    @tasks.loop(minutes=5)
    async def poll_watcher(self) -> None:
        due = [
            poll_id
            for poll_id, poll in self.data.get("polls", {}).items()
            if poll.get("status") == "open"
            and safe_int(poll.get("end_ts")) <= now_ts()
        ]

        for poll_id in due:
            await self.close_poll(str(poll_id), post_result=True)

    @poll_watcher.before_loop
    async def before_poll_watcher(self) -> None:
        await self.bot.wait_until_ready()

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

            existing = {
                idea.get("text", "").strip().lower()
                for idea in poll.get("ideas", {}).values()
                if isinstance(idea, dict)
            }
            if idea_text.lower() in existing:
                return False, "That idea is already in the poll."

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
            "`/suggestion results` - show current results"
        ),
        inline=False,
    )
    embed.add_field(
        name="Default",
        value="A poll can run for 7 days, then it auto-posts the winner/shortlist.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
    shortlist_size: app_commands.Range[int, 1, 10] = 3,
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

    existing = cog.get_open_poll_for_channel(
        interaction.guild.id,
        interaction.channel.id,
    )
    if existing:
        poll_id, _ = existing
        await interaction.response.send_message(
            f"There is already an open suggestion poll in this channel: `{poll_id}`.",
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
