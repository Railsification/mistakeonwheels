import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.storage import configured_guild_ids, known_guild_dirs, load_guild_json, migrate_legacy_file_to_primary, save_guild_json


FEATURE_KEY = "suggestion_poll"
DATA_DIR = Path(os.getenv("HOTBOT_DATA_DIR", "."))
DATA_FILE = DATA_DIR / "suggestion_polls.json"
SUGGESTION_POLLS_FILENAME = "suggestion_polls.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

TECH_ROLE_NAMES = {"Tech"}  # change only if your actual hidden/support role is named differently
MAX_IDEA_LEN = 180
MAX_VISIBLE_IDEAS = 20


def now_ts() -> int:
    return int(time.time())


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
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

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.add_idea_from_ui(interaction, self.poll_id, str(self.idea.value))


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

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.vote_from_ui(interaction, self.poll_id, safe_int(str(self.idea_number.value), -1))


class SuggestionPollView(discord.ui.View):
    def __init__(self, cog: "SuggestionPollCog", poll_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.poll_id = poll_id

    @discord.ui.button(
        label="Add Idea",
        style=discord.ButtonStyle.primary,
        custom_id="suggestion_poll:add",
    )
    async def add_idea_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        poll_id = await self.cog.poll_id_from_message(interaction)
        if not poll_id:
            await interaction.response.send_message("Couldn’t find that poll.", ephemeral=True)
            return
        await interaction.response.send_modal(AddIdeaModal(self.cog, poll_id))

    @discord.ui.button(
        label="Vote",
        style=discord.ButtonStyle.success,
        custom_id="suggestion_poll:vote",
    )
    async def vote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        poll_id = await self.cog.poll_id_from_message(interaction)
        if not poll_id:
            await interaction.response.send_message("Couldn’t find that poll.", ephemeral=True)
            return
        await interaction.response.send_modal(VoteIdeaModal(self.cog, poll_id))

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        custom_id="suggestion_poll:refresh",
    )
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        poll_id = await self.cog.poll_id_from_message(interaction)
        if not poll_id:
            await interaction.response.send_message("Couldn’t find that poll.", ephemeral=True)
            return
        await self.cog.refresh_from_ui(interaction, poll_id)


class SuggestionPollCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.data: Dict[str, Any] = {"polls": {}}
        self.load_data()

    async def cog_load(self):
        self.poll_watcher.start()

    async def cog_unload(self):
        self.poll_watcher.cancel()

    def _poll_guild_ids(self) -> list[int]:
        ids = set(configured_guild_ids(self.bot)) | set(known_guild_dirs())
        legacy = None
        if DATA_FILE.exists():
            try:
                legacy = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            except Exception:
                legacy = None
        if isinstance(legacy, dict):
            for poll in (legacy.get("polls") or {}).values():
                try:
                    gid = int(poll.get("guild_id"))
                except Exception:
                    continue
                if gid:
                    ids.add(gid)
        return sorted(ids)

    def load_data(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        migrate_legacy_file_to_primary(SUGGESTION_POLLS_FILENAME, self.bot, {"polls": {}})
        self.data = {"polls": {}}

        for guild_id in self._poll_guild_ids():
            loaded = load_guild_json(guild_id, SUGGESTION_POLLS_FILENAME, {"polls": {}})
            if not isinstance(loaded, dict):
                continue
            polls = loaded.get("polls") or {}
            if isinstance(polls, dict):
                for poll_id, poll in polls.items():
                    if isinstance(poll, dict):
                        poll["guild_id"] = int(poll.get("guild_id") or guild_id)
                        self.data["polls"][str(poll_id)] = poll

    def save_data(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        by_guild: dict[int, dict] = {}
        for poll_id, poll in (self.data.get("polls") or {}).items():
            try:
                guild_id = int(poll.get("guild_id"))
            except Exception:
                continue
            by_guild.setdefault(guild_id, {"polls": {}})["polls"][str(poll_id)] = poll

        for guild_id in set(self._poll_guild_ids()) | set(by_guild.keys()):
            save_guild_json(guild_id, SUGGESTION_POLLS_FILENAME, by_guild.get(guild_id, {"polls": {}}))

    async def interaction_allowed(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not interaction.channel:
            return False

        guild_id = interaction.guild.id
        channel_id = interaction.channel.id

        settings = getattr(self.bot, "settings", None)
        if settings is not None and hasattr(settings, "is_feature_allowed"):
            try:
                return bool(settings.is_feature_allowed(guild_id, channel_id, FEATURE_KEY))
            except Exception:
                pass

        possible_helpers = [
            "feature_allowed",
            "is_feature_allowed",
            "feature_channel_allowed",
            "is_feature_channel",
            "check_feature_channel",
        ]

        for helper_name in possible_helpers:
            helper = getattr(self.bot, helper_name, None)
            if callable(helper):
                try:
                    result = helper(guild_id, channel_id, FEATURE_KEY)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return bool(result)
                except TypeError:
                    try:
                        result = helper(interaction, FEATURE_KEY)
                        if asyncio.iscoroutine(result):
                            result = await result
                        return bool(result)
                    except Exception:
                        pass
                except Exception:
                    pass

        return self.settings_json_allows(guild_id, channel_id)

    def settings_json_allows(self, guild_id: int, channel_id: int) -> bool:
        if not SETTINGS_FILE.exists():
            return False

        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                settings = json.load(f)
        except Exception:
            return False

        guild_keys = [str(guild_id), guild_id]
        guild_settings = None

        for key in guild_keys:
            if key in settings:
                guild_settings = settings[key]
                break

        if guild_settings is None:
            guild_settings = settings

        possible_paths = [
            ("feature_channels", FEATURE_KEY),
            ("features", FEATURE_KEY),
            ("channels", FEATURE_KEY),
            ("allowed_channels", FEATURE_KEY),
        ]

        for section, feature in possible_paths:
            section_data = guild_settings.get(section, {})
            if isinstance(section_data, dict):
                channels = section_data.get(feature, [])
                if isinstance(channels, dict):
                    channels = channels.get("channels", [])
                if str(channel_id) in [str(x) for x in channels]:
                    return True

        flat = guild_settings.get(FEATURE_KEY)
        if isinstance(flat, list) and str(channel_id) in [str(x) for x in flat]:
            return True

        return False

    async def require_feature_channel(self, interaction: discord.Interaction) -> bool:
        if await self.interaction_allowed(interaction):
            return True

        await interaction.response.send_message(
            "Suggestion polls are not enabled in this channel yet. Use the existing feature channel setup for `suggestion_poll`.",
            ephemeral=True,
        )
        return False

    def member_is_tech(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False

        member_role_names = {role.name for role in member.roles}
        return bool(member_role_names & TECH_ROLE_NAMES)

    async def require_tech(self, interaction: discord.Interaction) -> bool:
        if self.member_is_tech(interaction):
            return True

        await interaction.response.send_message("Nope. Tech role only.", ephemeral=True)
        return False

    def new_poll_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def get_poll(self, poll_id: str) -> Optional[Dict[str, Any]]:
        return self.data.get("polls", {}).get(poll_id)

    def get_open_poll_for_channel(self, guild_id: int, channel_id: int) -> Optional[Tuple[str, Dict[str, Any]]]:
        polls = self.data.get("polls", {})
        matches = []

        for poll_id, poll in polls.items():
            if (
                poll.get("guild_id") == guild_id
                and poll.get("channel_id") == channel_id
                and poll.get("status") == "open"
            ):
                matches.append((poll_id, poll))

        if not matches:
            return None

        matches.sort(key=lambda item: item[1].get("created_ts", 0), reverse=True)
        return matches[0]

    async def poll_id_from_message(self, interaction: discord.Interaction) -> Optional[str]:
        message = interaction.message
        if not message:
            return None

        for poll_id, poll in self.data.get("polls", {}).items():
            if poll.get("message_id") == message.id:
                return poll_id

        return None

    def sorted_ideas(self, poll: Dict[str, Any]) -> List[Tuple[int, Dict[str, Any]]]:
        ideas = poll.get("ideas", {})
        rows = []

        for idea_no, idea in ideas.items():
            rows.append((safe_int(idea_no), idea))

        rows.sort(key=lambda item: item[0])
        return rows

    def ranked_ideas(self, poll: Dict[str, Any]) -> List[Tuple[int, Dict[str, Any], int]]:
        rows = []

        for idea_no, idea in self.sorted_ideas(poll):
            voters = idea.get("voters", [])
            rows.append((idea_no, idea, len(voters)))

        rows.sort(key=lambda item: (-item[2], item[0]))
        return rows

    def shortlist(self, poll: Dict[str, Any]) -> List[Tuple[int, Dict[str, Any], int]]:
        ranked = self.ranked_ideas(poll)
        if not ranked:
            return []

        size = max(1, safe_int(poll.get("shortlist_size"), 3))
        base = ranked[:size]

        if len(ranked) <= size:
            return base

        cutoff_votes = base[-1][2]
        extra_ties = [row for row in ranked[size:] if row[2] == cutoff_votes and cutoff_votes > 0]
        return base + extra_ties

    def build_embed(self, poll_id: str, poll: Dict[str, Any], final: bool = False) -> discord.Embed:
        status = poll.get("status", "open")
        title = poll.get("title") or "WoS PFP Theme Suggestions"
        end_ts = poll.get("end_ts", 0)

        if final or status == "closed":
            embed_title = f"🏁 Closed: {title}"
            colour = discord.Colour.gold()
        elif status == "cancelled":
            embed_title = f"Cancelled: {title}"
            colour = discord.Colour.dark_grey()
        else:
            embed_title = f"📸 {title}"
            colour = discord.Colour.blurple()

        description = poll.get("description") or "Drop WoS profile picture theme ideas, then vote for the ones you want."
        embed = discord.Embed(
            title=embed_title,
            description=description,
            colour=colour,
            timestamp=datetime.now(timezone.utc),
        )

        if status == "open":
            embed.add_field(name="Ends", value=f"<t:{end_ts}:F>\n<t:{end_ts}:R>", inline=True)
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
            lines = []
            for idea_no, idea in ideas[:MAX_VISIBLE_IDEAS]:
                voters = idea.get("voters", [])
                vote_word = "vote" if len(voters) == 1 else "votes"
                text = truncate(idea.get("text", ""), 90)
                lines.append(f"**{idea_no}.** {text} — **{len(voters)}** {vote_word}")

            hidden = len(ideas) - MAX_VISIBLE_IDEAS
            if hidden > 0:
                lines.append(f"...and {hidden} more.")

            embed.add_field(name="Ideas", value="\n".join(lines), inline=False)

        if final or status == "closed":
            top = self.shortlist(poll)
            if not top:
                embed.add_field(name="Result", value="No ideas were added.", inline=False)
            else:
                top_lines = []
                for idea_no, idea, votes in top:
                    vote_word = "vote" if votes == 1 else "votes"
                    top_lines.append(f"**{idea_no}.** {truncate(idea.get('text', ''), 120)} — **{votes}** {vote_word}")
                embed.add_field(name="Winner / Shortlist", value="\n".join(top_lines), inline=False)

        embed.set_footer(text="Use /suggestion add, /suggestion vote, or the buttons below.")
        return embed

    async def update_poll_message(self, poll_id: str):
        poll = self.get_poll(poll_id)
        if not poll:
            return

        guild = self.bot.get_guild(poll.get("guild_id"))
        if not guild:
            return

        channel = guild.get_channel(poll.get("channel_id"))
        if not isinstance(channel, discord.TextChannel):
            return

        message_id = poll.get("message_id")
        if not message_id:
            return

        try:
            message = await channel.fetch_message(message_id)
        except Exception:
            return

        view = None if poll.get("status") != "open" else SuggestionPollView(self, poll_id)
        await message.edit(embed=self.build_embed(poll_id, poll), view=view)

    async def post_final_result(self, poll_id: str, poll: Dict[str, Any]):
        guild = self.bot.get_guild(poll.get("guild_id"))
        if not guild:
            return

        channel = guild.get_channel(poll.get("channel_id"))
        if not isinstance(channel, discord.TextChannel):
            return

        embed = self.build_embed(poll_id, poll, final=True)
        await channel.send(embed=embed)

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
    async def poll_watcher(self):
        await self.bot.wait_until_ready()

        due = []
        for poll_id, poll in self.data.get("polls", {}).items():
            if poll.get("status") == "open" and safe_int(poll.get("end_ts")) <= now_ts():
                due.append(poll_id)

        for poll_id in due:
            await self.close_poll(poll_id, post_result=True)

    @poll_watcher.before_loop
    async def before_poll_watcher(self):
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

            existing = [
                idea.get("text", "").strip().lower()
                for idea in poll.get("ideas", {}).values()
            ]

            if idea_text.lower() in existing:
                return False, "That idea is already in the poll."

            idea_no = str(poll.get("next_idea_no", 1))
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

            allow_multi = bool(poll.get("allow_multi_vote", True))

            if not allow_multi:
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

    async def add_idea_from_ui(self, interaction: discord.Interaction, poll_id: str, idea_text: str):
        if not await self.interaction_allowed(interaction):
            await interaction.response.send_message("Suggestion polls are not enabled in this channel.", ephemeral=True)
            return

        ok, message = await self.add_idea_core(poll_id, interaction.user.id, idea_text)
        await interaction.response.send_message(message, ephemeral=True)

    async def vote_from_ui(self, interaction: discord.Interaction, poll_id: str, idea_number: int):
        if not await self.interaction_allowed(interaction):
            await interaction.response.send_message("Suggestion polls are not enabled in this channel.", ephemeral=True)
            return

        ok, message = await self.vote_core(poll_id, interaction.user.id, idea_number)
        await interaction.response.send_message(message, ephemeral=True)

    async def refresh_from_ui(self, interaction: discord.Interaction, poll_id: str):
        if not await self.interaction_allowed(interaction):
            await interaction.response.send_message("Suggestion polls are not enabled in this channel.", ephemeral=True)
            return

        await self.update_poll_message(poll_id)
        await interaction.response.send_message("Refreshed.", ephemeral=True)


suggestion_group = app_commands.Group(
    name="suggestion",
    description="WoS PFP suggestion polls",
)


@suggestion_group.command(name="help", description="Show how suggestion polls work.")
async def suggestion_help(interaction: discord.Interaction):
    cog: SuggestionPollCog = interaction.client.get_cog("SuggestionPollCog")
    if not cog:
        await interaction.response.send_message("Suggestion poll cog is not loaded.", ephemeral=True)
        return

    if not await cog.require_feature_channel(interaction):
        return

    embed = discord.Embed(
        title="📸 WoS PFP Suggestion Polls",
        description=(
            "Use this to collect profile picture theme ideas, vote on them, then pick a winner or shortlist."
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
):
    cog: SuggestionPollCog = interaction.client.get_cog("SuggestionPollCog")
    if not cog:
        await interaction.response.send_message("Suggestion poll cog is not loaded.", ephemeral=True)
        return

    if not await cog.require_feature_channel(interaction):
        return

    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("Use this inside a server channel.", ephemeral=True)
        return

    existing = cog.get_open_poll_for_channel(interaction.guild.id, interaction.channel.id)
    if existing:
        poll_id, _ = existing
        await interaction.response.send_message(
            f"There is already an open suggestion poll in this channel: `{poll_id}`.",
            ephemeral=True,
        )
        return

    poll_id = cog.new_poll_id()
    end_ts = now_ts() + int(duration_days) * 86400

    poll = {
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel.id,
        "message_id": None,
        "title": title or "WoS PFP Theme Suggestions",
        "description": description or "Drop WoS profile picture theme ideas, then vote for the ones you want.",
        "created_by": interaction.user.id,
        "created_at": iso_now(),
        "created_ts": now_ts(),
        "end_ts": end_ts,
        "status": "open",
        "shortlist_size": int(shortlist_size),
        "allow_multi_vote": bool(allow_multi_vote),
        "ideas": {},
        "next_idea_no": 1,
    }

    async with cog.lock:
        cog.data.setdefault("polls", {})[poll_id] = poll
        cog.save_data()

    embed = cog.build_embed(poll_id, poll)
    view = SuggestionPollView(cog, poll_id)

    await interaction.response.send_message(embed=embed, view=view)
    sent = await interaction.original_response()

    async with cog.lock:
        poll["message_id"] = sent.id
        cog.save_data()


@suggestion_group.command(name="add", description="Add an idea to the open suggestion poll.")
@app_commands.describe(idea="Your WoS PFP theme idea")
async def suggestion_add(interaction: discord.Interaction, idea: str):
    cog: SuggestionPollCog = interaction.client.get_cog("SuggestionPollCog")
    if not cog:
        await interaction.response.send_message("Suggestion poll cog is not loaded.", ephemeral=True)
        return

    if not await cog.require_feature_channel(interaction):
        return

    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("Use this inside a server channel.", ephemeral=True)
        return

    active = cog.get_open_poll_for_channel(interaction.guild.id, interaction.channel.id)
    if not active:
        await interaction.response.send_message("No open suggestion poll in this channel.", ephemeral=True)
        return

    poll_id, _ = active
    ok, message = await cog.add_idea_core(poll_id, interaction.user.id, idea)
    await interaction.response.send_message(message, ephemeral=True)


@suggestion_group.command(name="vote", description="Vote for an idea number.")
@app_commands.describe(idea_number="The idea number shown on the poll")
async def suggestion_vote(interaction: discord.Interaction, idea_number: int):
    cog: SuggestionPollCog = interaction.client.get_cog("SuggestionPollCog")
    if not cog:
        await interaction.response.send_message("Suggestion poll cog is not loaded.", ephemeral=True)
        return

    if not await cog.require_feature_channel(interaction):
        return

    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("Use this inside a server channel.", ephemeral=True)
        return

    active = cog.get_open_poll_for_channel(interaction.guild.id, interaction.channel.id)
    if not active:
        await interaction.response.send_message("No open suggestion poll in this channel.", ephemeral=True)
        return

    poll_id, _ = active
    ok, message = await cog.vote_core(poll_id, interaction.user.id, idea_number)
    await interaction.response.send_message(message, ephemeral=True)


@suggestion_group.command(name="remove_vote", description="Remove your vote from an idea.")
@app_commands.describe(idea_number="The idea number shown on the poll")
async def suggestion_remove_vote(interaction: discord.Interaction, idea_number: int):
    cog: SuggestionPollCog = interaction.client.get_cog("SuggestionPollCog")
    if not cog:
        await interaction.response.send_message("Suggestion poll cog is not loaded.", ephemeral=True)
        return

    if not await cog.require_feature_channel(interaction):
        return

    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("Use this inside a server channel.", ephemeral=True)
        return

    active = cog.get_open_poll_for_channel(interaction.guild.id, interaction.channel.id)
    if not active:
        await interaction.response.send_message("No open suggestion poll in this channel.", ephemeral=True)
        return

    poll_id, _ = active
    ok, message = await cog.remove_vote_core(poll_id, interaction.user.id, idea_number)
    await interaction.response.send_message(message, ephemeral=True)


@suggestion_group.command(name="results", description="Show current suggestion poll results.")
async def suggestion_results(interaction: discord.Interaction):
    cog: SuggestionPollCog = interaction.client.get_cog("SuggestionPollCog")
    if not cog:
        await interaction.response.send_message("Suggestion poll cog is not loaded.", ephemeral=True)
        return

    if not await cog.require_feature_channel(interaction):
        return

    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("Use this inside a server channel.", ephemeral=True)
        return

    active = cog.get_open_poll_for_channel(interaction.guild.id, interaction.channel.id)
    if not active:
        await interaction.response.send_message("No open suggestion poll in this channel.", ephemeral=True)
        return

    poll_id, poll = active
    ranked = cog.ranked_ideas(poll)

    if not ranked:
        await interaction.response.send_message("No ideas yet.", ephemeral=True)
        return

    lines = []
    for idea_no, idea, votes in ranked[:10]:
        vote_word = "vote" if votes == 1 else "votes"
        lines.append(f"**{idea_no}.** {truncate(idea.get('text', ''), 100)} — **{votes}** {vote_word}")

    embed = discord.Embed(
        title="Current Suggestion Results",
        description="\n".join(lines),
        colour=discord.Colour.blurple(),
    )
    embed.set_footer(text=f"Poll ID: {poll_id}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


council_group = app_commands.Group(
    name="council",
    description="Bot council tools",
)


@council_group.command(name="suggestion_help_post", description="Post suggestion poll help into this channel.")
async def council_suggestion_help_post(interaction: discord.Interaction):
    cog: SuggestionPollCog = interaction.client.get_cog("SuggestionPollCog")
    if not cog:
        await interaction.response.send_message("Suggestion poll cog is not loaded.", ephemeral=True)
        return

    if not await cog.require_tech(interaction):
        return

    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📸 WoS PFP Suggestion Polls",
        description="Add PFP theme ideas, vote on them, then we use the winner or shortlist for the next theme.",
        colour=discord.Colour.blurple(),
    )
    embed.add_field(
        name="How to use",
        value=(
            "`/suggestion add idea:your idea`\n"
            "`/suggestion vote idea_number:1`\n"
            "`/suggestion results`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Notes",
        value="Polls usually run for a week. When the poll closes, the bot posts the winner/shortlist.",
        inline=False,
    )

    await interaction.channel.send(embed=embed)
    await interaction.response.send_message("Posted suggestion poll help.", ephemeral=True)


@council_group.command(name="suggestion_close", description="Force-close the active suggestion poll in this channel.")
async def council_suggestion_close(interaction: discord.Interaction):
    cog: SuggestionPollCog = interaction.client.get_cog("SuggestionPollCog")
    if not cog:
        await interaction.response.send_message("Suggestion poll cog is not loaded.", ephemeral=True)
        return

    if not await cog.require_tech(interaction):
        return

    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("Use this inside a server channel.", ephemeral=True)
        return

    active = cog.get_open_poll_for_channel(interaction.guild.id, interaction.channel.id)
    if not active:
        await interaction.response.send_message("No open suggestion poll in this channel.", ephemeral=True)
        return

    poll_id, _ = active
    closed = await cog.close_poll(poll_id, post_result=True)

    if closed:
        await interaction.response.send_message(f"Closed suggestion poll `{poll_id}`.", ephemeral=True)
    else:
        await interaction.response.send_message("Could not close that poll.", ephemeral=True)


@council_group.command(name="suggestion_cancel", description="Cancel the active suggestion poll without posting a winner.")
async def council_suggestion_cancel(interaction: discord.Interaction):
    cog: SuggestionPollCog = interaction.client.get_cog("SuggestionPollCog")
    if not cog:
        await interaction.response.send_message("Suggestion poll cog is not loaded.", ephemeral=True)
        return

    if not await cog.require_tech(interaction):
        return

    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("Use this inside a server channel.", ephemeral=True)
        return

    active = cog.get_open_poll_for_channel(interaction.guild.id, interaction.channel.id)
    if not active:
        await interaction.response.send_message("No open suggestion poll in this channel.", ephemeral=True)
        return

    poll_id, poll = active

    async with cog.lock:
        poll["status"] = "cancelled"
        poll["cancelled_ts"] = now_ts()
        cog.save_data()

    await cog.update_poll_message(poll_id)
    await interaction.response.send_message(f"Cancelled suggestion poll `{poll_id}`.", ephemeral=True)


class SuggestionPollBotCog(SuggestionPollCog):
    pass


async def setup(bot: commands.Bot):
    from core.command_scope import bind_group_public

    cog = SuggestionPollCog(bot)
    await bot.add_cog(cog)

    bind_group_public(suggestion_group, bot, include_admin=True)
    try:
        bot.tree.add_command(suggestion_group)
    except app_commands.CommandAlreadyRegistered:
        pass

    # Admin controls for suggestions live under /council in cogs/admin.py.
    # Do not register another /council group here.
