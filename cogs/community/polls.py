from __future__ import annotations

import asyncio
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.utils import DATA_DIR, load_json, save_json
from core.storage import (
    configured_guild_ids,
    guild_json_path,
    known_guild_dirs,
    load_guild_json,
    migrate_legacy_file_to_primary,
    save_guild_json,
)

POLLS_FILE = DATA_DIR / "polls.json"
POLLS_FILENAME = "polls.json"
NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def parse_duration(raw: str) -> int:
    raw = raw.strip().lower()
    if len(raw) < 2:
        raise ValueError("Duration must look like 30s, 5m, 2h, or 1d")

    value = int(raw[:-1])
    unit = raw[-1]

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }

    if unit not in multipliers:
        raise ValueError("Duration unit must be s, m, h, or d")

    seconds = value * multipliers[unit]

    if seconds < 10 or seconds > 3 * 86400:
        raise ValueError("Duration must be between 10 seconds and 3 days")

    return seconds


def canonical_cdn_url(url: str) -> str:
    return url.split("?", 1)[0]


def humanize_secs(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)


class Polls(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_poll_tasks: dict[str, asyncio.Task] = {}

    @property
    def media_vault_channel_id(self) -> int:
        cfg = getattr(self.bot, "hot_config", {}) or {}
        return int(cfg.get("media_channel_id", 0) or 0)

    async def cog_load(self) -> None:
        asyncio.create_task(self._resume_after_ready())

    async def _resume_after_ready(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(2)
        await self.resume_active_polls()

    def cog_unload(self) -> None:
        for task in self.active_poll_tasks.values():
            task.cancel()
        self.active_poll_tasks.clear()

    async def safe_ack(self, interaction: discord.Interaction, content: str) -> bool:
        age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        cmd_name = interaction.command.name if interaction.command else "unknown"
        print(f"[polls] {cmd_name} start age={age:.3f}s latency={self.bot.latency:.3f}s")

        try:
            await interaction.response.send_message(content, ephemeral=True)
            return True
        except discord.NotFound:
            print(f"[polls] {cmd_name}: interaction expired before initial response")
            return False
        except Exception as e:
            print(f"[polls] {cmd_name}: initial response failed: {e!r}")
            return False

    def _poll_guild_ids(self) -> list[int]:
        ids = set(configured_guild_ids(self.bot)) | set(known_guild_dirs())
        legacy = load_json(POLLS_FILE, [])
        if isinstance(legacy, list):
            for poll in legacy:
                try:
                    gid = int(poll.get("guild_id"))
                except Exception:
                    continue
                if gid:
                    ids.add(gid)
        return sorted(ids)

    def load_polls(self) -> list[dict]:
        migrate_legacy_file_to_primary(POLLS_FILENAME, self.bot, [])
        records: list[dict] = []

        for guild_id in self._poll_guild_ids():
            path = guild_json_path(guild_id, POLLS_FILENAME)
            print(f"[polls] loading from: {path}")
            data = load_guild_json(guild_id, POLLS_FILENAME, [])
            if not isinstance(data, list):
                print(f"[polls] {path} is not a list: {type(data).__name__}")
                data = []

            changed = False
            for poll in data:
                poll["guild_id"] = int(poll.get("guild_id") or guild_id)
                if self.upgrade_poll_record(poll):
                    changed = True
            if changed:
                save_guild_json(guild_id, POLLS_FILENAME, data)
            records.extend(data)

        print(f"[polls] loaded {len(records)} poll record(s) across guild storage")
        return records

    def save_polls(self, polls: list[dict]) -> None:
        by_guild: dict[int, list[dict]] = {}
        for poll in polls:
            try:
                guild_id = int(poll.get("guild_id"))
            except Exception:
                continue
            by_guild.setdefault(guild_id, []).append(poll)

        for guild_id in set(self._poll_guild_ids()) | set(by_guild.keys()):
            save_guild_json(guild_id, POLLS_FILENAME, by_guild.get(guild_id, []))

    def upgrade_poll_record(self, poll: dict) -> bool:
        changed = False
        option_count = len(poll.get("message_ids", []))

        defaults = {
            "message_ids": [],
            "emoji_list": [],
            "attachment_urls": [],
            "vault_message_ids": [],
            "filenames": [],
            "option_names": [],
        }

        for key, default in defaults.items():
            if key not in poll or not isinstance(poll[key], list):
                poll[key] = list(default)
                changed = True

        if "vault_channel_id" not in poll:
            poll["vault_channel_id"] = self.media_vault_channel_id
            changed = True

        while len(poll["emoji_list"]) < option_count and len(poll["emoji_list"]) < len(NUMBER_EMOJIS):
            poll["emoji_list"].append(NUMBER_EMOJIS[len(poll["emoji_list"])])
            changed = True

        while len(poll["attachment_urls"]) < option_count:
            poll["attachment_urls"].append("")
            changed = True

        while len(poll["vault_message_ids"]) < option_count:
            poll["vault_message_ids"].append(None)
            changed = True

        while len(poll["filenames"]) < option_count:
            poll["filenames"].append(None)
            changed = True

        while len(poll["option_names"]) < option_count:
            poll["option_names"].append(None)
            changed = True

        return changed

    def get_guild_polls(self, guild_id: int) -> list[dict]:
        return [p for p in self.load_polls() if p.get("guild_id") == guild_id]

    def resolve_poll(
        self,
        polls: list[dict],
        poll_id: str,
        guild_id: Optional[int] = None,
    ) -> Optional[dict]:
        matches = []
        for poll in polls:
            if guild_id is not None and poll.get("guild_id") != guild_id:
                continue

            pid = str(poll.get("id", ""))
            if pid == poll_id or pid.endswith(poll_id):
                matches.append(poll)

        if not matches:
            return None

        if len(matches) > 1:
            raise ValueError("Multiple polls match that suffix. Use the full poll ID.")

        return matches[0]

    def remove_poll_by_id(self, polls: list[dict], poll_id: str) -> list[dict]:
        return [p for p in polls if p.get("id") != poll_id]

    async def get_message_channel(self, channel_id: int) -> discord.TextChannel | discord.Thread:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise TypeError(f"Channel {channel_id} is not a text channel or thread")

        return channel

    async def store_attachment_in_vault(self, attachment: discord.Attachment) -> dict:
        vault_channel_id = self.media_vault_channel_id
        if not vault_channel_id:
            raise RuntimeError("MEDIA_CHANNEL_ID is not set in the bot config")

        vault_channel = await self.get_message_channel(vault_channel_id)

        file = await attachment.to_file()
        vault_msg = await vault_channel.send(
            content=f"poll-media | {attachment.filename}",
            file=file,
        )

        if not vault_msg.attachments:
            raise RuntimeError("Vault upload succeeded but no attachment was returned")

        stored_attachment = vault_msg.attachments[0]

        return {
            "vault_message_id": vault_msg.id,
            "vault_channel_id": vault_channel.id,
            "url": canonical_cdn_url(stored_attachment.url),
            "filename": stored_attachment.filename,
        }

    async def get_attachment_url_for_option(self, poll: dict, index: int) -> str:
        vault_message_ids = poll.get("vault_message_ids", [])
        vault_channel_id = poll.get("vault_channel_id") or self.media_vault_channel_id

        if index < len(vault_message_ids) and vault_message_ids[index]:
            try:
                vault_channel = await self.get_message_channel(vault_channel_id)
                vault_msg = await vault_channel.fetch_message(vault_message_ids[index])
                if vault_msg.attachments:
                    return canonical_cdn_url(vault_msg.attachments[0].url)
            except Exception:
                pass

        attachment_urls = poll.get("attachment_urls", [])
        if index < len(attachment_urls):
            return canonical_cdn_url(attachment_urls[index])

        return ""

    def get_option_name(self, poll: dict, index: int) -> Optional[str]:
        option_names = poll.get("option_names", [])
        if index < len(option_names):
            return option_names[index]
        return None

    def build_option_embed(
        self,
        option_number: int,
        title: str,
        image_url: str,
        total_options: int,
        end_ts: float,
        option_name: Optional[str] = None,
    ) -> discord.Embed:
        description_lines = [title]

        if option_name:
            description_lines.append(f"**Label:** {option_name}")

        description_lines.append(f"🗳️ Voting started with {total_options} options! Ends <t:{int(end_ts)}:R>.")

        embed = discord.Embed(
            title=f"Option {option_number}",
            description="\n\n".join(description_lines),
        )

        if image_url:
            embed.set_image(url=image_url)

        return embed

    async def refresh_poll_messages(self, poll: dict) -> None:
        poll_channel = await self.get_message_channel(poll["channel_id"])
        total_options = len(poll["message_ids"])

        for index, message_id in enumerate(poll["message_ids"]):
            try:
                msg = await poll_channel.fetch_message(message_id)
                image_url = await self.get_attachment_url_for_option(poll, index)
                option_name = self.get_option_name(poll, index)

                embed = self.build_option_embed(
                    option_number=index + 1,
                    title=poll["title"],
                    image_url=image_url,
                    total_options=total_options,
                    end_ts=poll["end_ts"],
                    option_name=option_name,
                )
                await msg.edit(embed=embed)
            except Exception as e:
                print(f"[polls] refresh failed for message {message_id}: {e!r}")
                continue

    async def finalize_poll(self, poll_id: str) -> None:
        polls = self.load_polls()
        poll = next((p for p in polls if p.get("id") == poll_id), None)

        if poll is None:
            self.active_poll_tasks.pop(poll_id, None)
            return

        try:
            poll_channel = await self.get_message_channel(poll["channel_id"])
        except Exception:
            updated = self.remove_poll_by_id(polls, poll_id)
            self.save_polls(updated)
            self.active_poll_tasks.pop(poll_id, None)
            return

        counts: list[int] = []
        emojis = poll.get("emoji_list", [])

        for index, message_id in enumerate(poll.get("message_ids", [])):
            emoji = emojis[index] if index < len(emojis) else NUMBER_EMOJIS[index]

            try:
                msg = await poll_channel.fetch_message(message_id)
                reaction = next((r for r in msg.reactions if str(r.emoji) == emoji), None)
                vote_count = max(0, reaction.count - 1) if reaction else 0
            except Exception:
                vote_count = 0

            counts.append(vote_count)

        top = max(counts, default=0)
        winners = [i for i, count in enumerate(counts) if count == top]

        if top == 0:
            result_text = "🧐 No votes."
        elif len(winners) > 1:
            result_text = f"🤝 Tie! {top} vote(s) each."
        else:
            winner_name = self.get_option_name(poll, winners[0])
            if winner_name:
                result_text = f"🏆 Winner: Option {winners[0] + 1} — {winner_name} with {top} vote(s)."
            else:
                result_text = f"🏆 Winner: Option {winners[0] + 1} with {top} vote(s)."

        embed = discord.Embed(
            title="📊 Poll Results",
            description=result_text,
        )

        def bar(value: int, max_value: int, width: int = 14) -> str:
            if max_value <= 0:
                return ""
            return "█" * max(0, round(width * (value / max_value)))

        for index, count in enumerate(counts):
            emoji = emojis[index] if index < len(emojis) else NUMBER_EMOJIS[index]
            option_name = self.get_option_name(poll, index)
            field_name = f"{emoji} Option {index + 1}"
            if option_name:
                field_name += f" — {option_name}"

            embed.add_field(
                name=field_name,
                value=f"`{bar(count, top)}` {count}",
                inline=False,
            )

        try:
            await poll_channel.send(embed=embed)
        except Exception:
            pass

        if top > 0:
            for winner_index in winners:
                try:
                    winner_url = await self.get_attachment_url_for_option(poll, winner_index)
                    if winner_url:
                        await poll_channel.send(winner_url)
                except Exception:
                    continue

        updated = self.remove_poll_by_id(polls, poll_id)
        self.save_polls(updated)
        self.active_poll_tasks.pop(poll_id, None)

    async def schedule_poll(self, poll_id: str) -> None:
        try:
            polls = self.load_polls()
            poll = next((p for p in polls if p.get("id") == poll_id), None)

            if poll is None:
                self.active_poll_tasks.pop(poll_id, None)
                return

            delay = max(0, float(poll.get("end_ts", 0)) - time.time())
            await asyncio.sleep(delay)
            await self.finalize_poll(poll_id)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[polls] schedule_poll failed for {poll_id}: {e!r}")
            self.active_poll_tasks.pop(poll_id, None)

    async def resume_active_polls(self) -> None:
        polls = self.load_polls()
        now = time.time()

        for poll in polls:
            poll_id = poll.get("id")
            if not poll_id:
                continue

            existing = self.active_poll_tasks.get(poll_id)
            if existing and not existing.done():
                continue

            end_ts = float(poll.get("end_ts", 0) or 0)

            if end_ts <= now:
                self.active_poll_tasks[poll_id] = asyncio.create_task(self.finalize_poll(poll_id))
            else:
                self.active_poll_tasks[poll_id] = asyncio.create_task(self.schedule_poll(poll_id))

    @app_commands.command(name="image_poll", description="Create an image poll")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def image_poll(
        self,
        interaction: discord.Interaction,
        title: str,
        duration: str,
        image_1: discord.Attachment,
        image_2: discord.Attachment,
        image_3: Optional[discord.Attachment] = None,
        image_4: Optional[discord.Attachment] = None,
        image_5: Optional[discord.Attachment] = None,
        image_6: Optional[discord.Attachment] = None,
        image_7: Optional[discord.Attachment] = None,
        image_8: Optional[discord.Attachment] = None,
        image_9: Optional[discord.Attachment] = None,
        image_10: Optional[discord.Attachment] = None,
    ):
        if not await self.safe_ack(interaction, "Creating poll..."):
            return

        try:
            duration_seconds = parse_duration(duration)

            attachments = [
                image_1, image_2, image_3, image_4, image_5,
                image_6, image_7, image_8, image_9, image_10,
            ]
            attachments = [a for a in attachments if a is not None]

            if len(attachments) < 2:
                raise ValueError("You need at least 2 images")
            if len(attachments) > 10:
                raise ValueError("Max 10 images")
            if not interaction.guild:
                raise ValueError("This command must be used in a server")

            poll_channel = await self.get_message_channel(interaction.channel_id)
            polls = self.load_polls()

            poll_id = f"{interaction.guild.id}-{int(time.time())}-{random.randint(1000, 9999)}"
            poll = {
                "id": poll_id,
                "guild_id": interaction.guild.id,
                "channel_id": interaction.channel_id,
                "message_ids": [],
                "emoji_list": [],
                "end_ts": time.time() + duration_seconds,
                "title": title,
                "attachment_urls": [],
                "vault_message_ids": [],
                "vault_channel_id": self.media_vault_channel_id,
                "filenames": [],
                "option_names": [],
            }

            for idx, attachment in enumerate(attachments, start=1):
                stored = await self.store_attachment_in_vault(attachment)

                embed = self.build_option_embed(
                    option_number=idx,
                    title=title,
                    image_url=stored["url"],
                    total_options=len(attachments),
                    end_ts=poll["end_ts"],
                    option_name=None,
                )

                msg = await poll_channel.send(embed=embed)
                await msg.add_reaction(NUMBER_EMOJIS[idx - 1])

                poll["message_ids"].append(msg.id)
                poll["emoji_list"].append(NUMBER_EMOJIS[idx - 1])
                poll["attachment_urls"].append(stored["url"])
                poll["vault_message_ids"].append(stored["vault_message_id"])
                poll["filenames"].append(stored["filename"])
                poll["option_names"].append(None)

            polls.append(poll)
            self.save_polls(polls)
            self.active_poll_tasks[poll_id] = asyncio.create_task(self.schedule_poll(poll_id))

            await interaction.edit_original_response(
                content=f"Poll created.\nPoll ID: `{poll_id}`"
            )

        except Exception as e:
            await interaction.edit_original_response(
                content=f"Failed to create poll: {e}"
            )

    @app_commands.command(name="poll_add_option", description="Add an option to a live image poll")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def poll_add_option(
        self,
        interaction: discord.Interaction,
        poll_id: str,
        option_name: str,
        image: discord.Attachment,
    ):
        if not await self.safe_ack(interaction, "Adding option..."):
            return

        try:
            if not interaction.guild:
                raise ValueError("This command must be used in a server")

            polls = self.load_polls()
            poll = self.resolve_poll(polls, poll_id, guild_id=interaction.guild.id)

            if poll is None:
                raise ValueError("Poll not found")

            if time.time() >= float(poll["end_ts"]):
                raise ValueError("That poll has already ended")

            current_count = len(poll["message_ids"])
            new_option_number = current_count + 1

            if new_option_number > 10:
                raise ValueError("Max 10 options")

            stored = await self.store_attachment_in_vault(image)

            self.upgrade_poll_record(poll)
            poll["vault_channel_id"] = self.media_vault_channel_id
            poll["title"] = f"{poll['title']}; {new_option_number} = {option_name}"

            poll_channel = await self.get_message_channel(poll["channel_id"])
            embed = self.build_option_embed(
                option_number=new_option_number,
                title=poll["title"],
                image_url=stored["url"],
                total_options=new_option_number,
                end_ts=poll["end_ts"],
                option_name=option_name,
            )

            new_msg = await poll_channel.send(embed=embed)
            await new_msg.add_reaction(NUMBER_EMOJIS[new_option_number - 1])

            poll["message_ids"].append(new_msg.id)
            poll["emoji_list"].append(NUMBER_EMOJIS[new_option_number - 1])
            poll["attachment_urls"].append(stored["url"])
            poll["vault_message_ids"].append(stored["vault_message_id"])
            poll["filenames"].append(stored["filename"])
            poll["option_names"].append(option_name)

            self.save_polls(polls)
            await self.refresh_poll_messages(poll)

            await interaction.edit_original_response(
                content=f"Added option {new_option_number} to poll `{poll['id']}`"
            )

        except Exception as e:
            await interaction.edit_original_response(
                content=f"Failed to add option: {e}"
            )

    @app_commands.command(name="poll_refresh", description="Refresh an existing poll's messages")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def poll_refresh(self, interaction: discord.Interaction, poll_id: str):
        if not await self.safe_ack(interaction, "Refreshing poll..."):
            return

        try:
            if not interaction.guild:
                raise ValueError("This command must be used in a server")

            polls = self.load_polls()
            poll = self.resolve_poll(polls, poll_id, guild_id=interaction.guild.id)

            if poll is None:
                raise ValueError("Poll not found")

            await self.refresh_poll_messages(poll)

            await interaction.edit_original_response(
                content=f"Refreshed poll `{poll['id']}`"
            )

        except Exception as e:
            await interaction.edit_original_response(
                content=f"Failed to refresh poll: {e}"
            )

    @app_commands.command(name="poll_list", description="List active polls")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def poll_list(self, interaction: discord.Interaction):
        if not await self.safe_ack(interaction, "Checking active polls..."):
            return

        try:
            if not interaction.guild:
                raise ValueError("This command must be used in a server")

            polls = self.get_guild_polls(interaction.guild.id)

            if not polls:
                await interaction.edit_original_response(content="No active polls.")
                return

            now = time.time()
            lines = []
            for poll in polls:
                remaining = humanize_secs(int(float(poll.get("end_ts", 0)) - now))
                lines.append(
                    f"{poll['id'][-6:]} | {len(poll.get('message_ids', []))} opts | ends in {remaining} | full: {poll['id']}"
                )

            await interaction.edit_original_response(
                content="Active polls:\n```" + "\n".join(lines) + "```"
            )

        except Exception as e:
            await interaction.edit_original_response(
                content=f"Failed to list polls: {e}"
            )

    @app_commands.command(name="poll_cancel", description="Cancel a poll or end it now")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def poll_cancel(
        self,
        interaction: discord.Interaction,
        poll_id: str,
        finalize_now: bool = True,
    ):
        if not await self.safe_ack(interaction, "Processing poll..."):
            return

        try:
            if not interaction.guild:
                raise ValueError("This command must be used in a server")

            polls = self.load_polls()
            poll = self.resolve_poll(polls, poll_id, guild_id=interaction.guild.id)

            if poll is None:
                raise ValueError("Poll not found")

            real_poll_id = poll["id"]

            task = self.active_poll_tasks.pop(real_poll_id, None)
            if task:
                task.cancel()

            if finalize_now:
                await self.finalize_poll(real_poll_id)
                await interaction.edit_original_response(
                    content=f"Poll `{real_poll_id}` ended."
                )
            else:
                updated = self.remove_poll_by_id(polls, real_poll_id)
                self.save_polls(updated)
                await interaction.edit_original_response(
                    content=f"Poll `{real_poll_id}` cancelled."
                )

        except Exception as e:
            await interaction.edit_original_response(
                content=f"Failed to cancel poll: {e}"
            )


async def setup(bot: commands.Bot):
    from core.command_scope import bind_public_cog

    cog = Polls(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
