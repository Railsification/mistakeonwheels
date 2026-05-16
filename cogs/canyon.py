from __future__ import annotations

import base64
import difflib
import io
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

from core.storage import load_guild_json, migrate_legacy_file_to_primary, save_guild_json

SESSIONS_FILE = DATA_DIR / "canyon_sessions.json"
SESSIONS_FILENAME = "canyon_sessions.json"
LANES = ["Left", "Left middle", "Right middle", "Right"]
MAX_SCAN_IMAGES = 10
MAX_SCAN_BATCH_HISTORY = 25
DEFAULT_HISTORY_MESSAGES = 40


@dataclass(slots=True)
class Player:
    name: str
    power: int


@dataclass(slots=True)
class GroupItem:
    members: list[Player]

    @property
    def power(self) -> int:
        return sum(p.power for p in self.members)


def load_sessions(bot, guild_id: int) -> dict:
    migrate_legacy_file_to_primary(SESSIONS_FILENAME, bot, {})
    data = load_guild_json(guild_id, SESSIONS_FILENAME, {})
    return data if isinstance(data, dict) else {}


def save_sessions(guild_id: int, data: dict) -> None:
    save_guild_json(guild_id, SESSIONS_FILENAME, data)


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def clean_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\s+", " ", name)
    return name


def power_to_int(text: str) -> int:
    s = text.strip().lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([kmbt]?)", s)
    if not match:
        raise ValueError(f"Could not parse power: {text}")

    value = float(match.group(1))
    suffix = match.group(2)

    mult = {
        "": 1,
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
        "t": 1_000_000_000_000,
    }[suffix]

    return int(round(value * mult))


def format_power(value: int) -> str:
    if value >= 1_000_000_000:
        txt = f"{value / 1_000_000_000:.3f}".rstrip("0").rstrip(".")
        return f"{txt}b"
    if value >= 1_000_000:
        txt = f"{value / 1_000_000:.3f}".rstrip("0").rstrip(".")
        return f"{txt}m"
    if value >= 1_000:
        txt = f"{value / 1_000:.3f}".rstrip("0").rstrip(".")
        return f"{txt}k"
    return str(value)


def dedupe_players(players: list[Player]) -> list[Player]:
    seen: dict[str, Player] = {}
    for p in players:
        key = normalize_name(p.name)
        prev = seen.get(key)
        if prev is None or p.power > prev.power:
            seen[key] = p
    return sorted(seen.values(), key=lambda p: (-p.power, p.name.lower()))


def merge_players(
    existing: list[Player],
    incoming: list[Player],
) -> tuple[list[Player], int, int, int]:
    """Merge a new scan batch into the saved roster.

    Returns: merged players, added count, updated count, ignored duplicate count.
    If the same normalised name appears twice, the higher power record is kept.
    """
    seen: dict[str, Player] = {normalize_name(p.name): p for p in existing}
    added = 0
    updated = 0
    ignored = 0

    for p in incoming:
        key = normalize_name(p.name)
        prev = seen.get(key)

        if prev is None:
            seen[key] = p
            added += 1
            continue

        if p.power > prev.power:
            seen[key] = p
            updated += 1
        else:
            ignored += 1

    merged = sorted(seen.values(), key=lambda player: (-player.power, player.name.lower()))
    return merged, added, updated, ignored


def get_player_lookup(players: list[Player]) -> dict[str, Player]:
    return {normalize_name(p.name): p for p in players}


def resolve_player(name: str, players: list[Player]) -> Player:
    wanted = normalize_name(name)
    lookup = get_player_lookup(players)

    if wanted in lookup:
        return lookup[wanted]

    close = difflib.get_close_matches(wanted, list(lookup.keys()), n=1, cutoff=0.80)
    if close:
        return lookup[close[0]]

    raise ValueError(f"Player not found: {name}")


def parse_csv_names(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def parse_semicolon_groups(raw: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for group in raw.split(";"):
        group = group.strip()
        if not group:
            continue
        members = [x.strip() for x in group.split("+") if x.strip()]
        if len(members) < 2:
            raise ValueError(f"Invalid combine group: {group}")
        groups.append(members)
    return groups


def roster_text(players: list[Player]) -> str:
    return "\n".join(f"{p.name} - {format_power(p.power)}" for p in players)


def rows_text(rows: dict[str, list[Player]], totals: dict[str, int]) -> str:
    out: list[str] = []

    for lane in LANES:
        out.append(lane)
        for p in rows[lane]:
            out.append(p.name)
        out.append("")

    out.append("Totals")
    for lane in LANES:
        out.append(f"{lane} - {format_power(totals[lane])}")

    return "\n".join(out).strip()


def is_image_attachment(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True

    filename = attachment.filename.lower()
    return filename.endswith((".png", ".jpg", ".jpeg", ".webp"))


def get_attachment_mime(attachment: discord.Attachment) -> str:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return attachment.content_type

    filename = attachment.filename.lower()
    if filename.endswith(".png"):
        return "image/png"
    if filename.endswith(".jpg") or filename.endswith(".jpeg"):
        return "image/jpeg"
    if filename.endswith(".webp"):
        return "image/webp"

    raise ValueError(f"Unsupported image type: {attachment.filename}")


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def parse_scan_payload(raw_text: str) -> list[Player]:
    raw_text = strip_code_fences(raw_text)

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw_text, flags=re.S)
        if not match:
            raise ValueError("Model did not return valid JSON.")
        payload = json.loads(match.group(0))

    raw_players = payload.get("players", [])
    if not isinstance(raw_players, list):
        raise ValueError("Invalid JSON shape: players must be a list.")

    players: list[Player] = []

    for item in raw_players:
        if not isinstance(item, dict):
            continue

        name = clean_name(str(item.get("name", "")).strip())
        power_text = str(item.get("power_text", "")).strip()

        if not name or not power_text:
            continue

        try:
            power = power_to_int(power_text)
        except Exception:
            continue

        players.append(Player(name=name, power=power))

    return dedupe_players(players)


async def send_long_message(
    interaction: discord.Interaction,
    title: str,
    body: str,
    *,
    ephemeral: bool = False,
) -> None:
    content = f"**{title}**\n```text\n{body}\n```"
    if len(content) <= 1900:
        await interaction.followup.send(content, ephemeral=ephemeral)
        return

    fp = io.BytesIO(body.encode("utf-8"))
    safe_title = re.sub(r"[^a-z0-9_ -]", "", title.lower()).strip().replace(" ", "_") or "canyon"
    file = discord.File(fp, filename=f"{safe_title}.txt")
    await interaction.followup.send(f"**{title}**", file=file, ephemeral=ephemeral)


def build_balanced_rows(
    players: list[Player],
    leaders_csv: str,
    combine_raw: Optional[str] = None,
    exclude_csv: Optional[str] = None,
) -> tuple[dict[str, list[Player]], dict[str, int], list[Player]]:
    working = players[:]

    if exclude_csv:
        excludes = {normalize_name(x) for x in parse_csv_names(exclude_csv)}
        working = [p for p in working if normalize_name(p.name) not in excludes]

    if len(working) < 4:
        raise ValueError("Need at least 4 players after exclusions.")

    leader_names = parse_csv_names(leaders_csv)
    if len(leader_names) != 4:
        raise ValueError(
            "Leaders must be exactly 4 names in lane order: "
            "Left, Left middle, Right middle, Right"
        )

    leaders = [resolve_player(name, working) for name in leader_names]
    leader_norms = [normalize_name(p.name) for p in leaders]

    if len(set(leader_norms)) != 4:
        raise ValueError("Leader list contains duplicates.")

    rows: dict[str, list[Player]] = {}
    totals: dict[str, int] = {}

    for lane, leader in zip(LANES, leaders):
        rows[lane] = [leader]
        totals[lane] = leader.power

    non_leaders = [p for p in working if normalize_name(p.name) not in set(leader_norms)]

    grouped_player_norms: set[str] = set()
    items: list[GroupItem] = []

    if combine_raw:
        for group_names in parse_semicolon_groups(combine_raw):
            members = [resolve_player(name, non_leaders) for name in group_names]
            norms = [normalize_name(p.name) for p in members]

            if len(set(norms)) != len(norms):
                raise ValueError(f"Duplicate player in combine group: {' + '.join(group_names)}")

            overlap = grouped_player_norms.intersection(norms)
            if overlap:
                raise ValueError(
                    f"Player used in more than one combine group: {', '.join(sorted(overlap))}"
                )

            grouped_player_norms.update(norms)
            items.append(GroupItem(members=members))

    for p in non_leaders:
        if normalize_name(p.name) in grouped_player_norms:
            continue
        items.append(GroupItem(members=[p]))

    items.sort(key=lambda item: (-item.power, len(item.members), item.members[0].name.lower()))

    for item in items:
        chosen_lane = min(
            LANES,
            key=lambda lane: (
                totals[lane],
                len(rows[lane]),
                LANES.index(lane),
            ),
        )

        rows[chosen_lane].extend(item.members)
        totals[chosen_lane] += item.power

    return rows, totals, sorted(working, key=lambda p: (-p.power, p.name.lower()))


class CanyonCog(commands.Cog):
    HELP_META = {
        "title": "Canyon",
        "summary": "Private WoS Canyon screenshot scanning, roster review, lane balancing, and public posting only when ready.",
        "details": (
            "Attach Canyon screenshots directly to `/canyon_scan`. The scan result and row drafts are ephemeral/private. "
            "Run `/canyon_scan` again with the next batch of screenshots and it appends to the saved roster. "
            "Use `reset:true` or `/canyon_clear` to start a fresh roster. "
            "Use `/canyon_rows` to build a private draft, then `/canyon_post` only when it is ready for the channel."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.oa = None
        self.model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    async def _get_openai_client(self):
        if self.oa is not None:
            return self.oa

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is missing.")

        from openai import AsyncOpenAI

        self.oa = AsyncOpenAI(api_key=api_key)
        return self.oa

    def _extract_response_text(self, response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        chunks: list[str] = []

        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                text_value = getattr(content, "text", None)
                if isinstance(text_value, str) and text_value.strip():
                    chunks.append(text_value)

        text = "\n".join(chunks).strip()
        if not text:
            raise RuntimeError("No text content returned from OpenAI response.")
        return text

    def _store_roster(
        self,
        guild_id: int,
        players: list[Player],
        *,
        reset: bool = False,
        image_count: int = 0,
        user_id: Optional[int] = None,
    ) -> dict[str, int]:
        sessions = load_sessions(self.bot, guild_id)

        existing_players: list[Player] = [] if reset else self._load_roster(guild_id)
        merged_players, added, updated, ignored = merge_players(existing_players, players)

        previous_total = len(existing_players)
        batch_record = {
            "ts": int(time.time()),
            "user_id": user_id,
            "images": image_count,
            "extracted": len(players),
            "added": added,
            "updated": updated,
            "ignored_duplicates": ignored,
            "saved_total": len(merged_players),
            "reset": bool(reset),
        }

        batches = sessions.get("scan_batches")
        if not isinstance(batches, list):
            batches = []
        batches.append(batch_record)
        sessions["scan_batches"] = batches[-MAX_SCAN_BATCH_HISTORY:]

        sessions["roster"] = {
            "updated_at": int(time.time()),
            "previous_total": previous_total,
            "last_batch": batch_record,
            "players": [{"name": p.name, "power": p.power} for p in merged_players],
        }
        save_sessions(guild_id, sessions)

        return {
            "previous_total": previous_total,
            "extracted": len(players),
            "added": added,
            "updated": updated,
            "ignored_duplicates": ignored,
            "saved_total": len(merged_players),
        }

    def _load_roster(self, guild_id: int) -> list[Player]:
        sessions = load_sessions(self.bot, guild_id)
        data = sessions.get("roster") or sessions.get(str(guild_id))
        if not data or not data.get("players"):
            return []
        return [Player(name=x["name"], power=int(x["power"])) for x in data["players"]]

    async def _collect_recent_images(
        self,
        interaction: discord.Interaction,
        history_messages: int,
    ) -> list[discord.Attachment]:
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError("This command must be used in a server text channel.")

        found: list[discord.Attachment] = []

        async for message in channel.history(limit=history_messages):
            if message.author.id != interaction.user.id:
                continue

            image_attachments = [a for a in message.attachments if is_image_attachment(a)]
            if not image_attachments:
                continue

            found.extend(image_attachments)

            if len(found) >= MAX_SCAN_IMAGES:
                break

        found = list(reversed(found))
        return found[-MAX_SCAN_IMAGES:]

    async def _extract_from_attachments(self, attachments: list[discord.Attachment]) -> list[Player]:
        client = await self._get_openai_client()

        if not attachments:
            raise ValueError("No images provided.")

        content_parts: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    "These are Whiteout Survival Canyon combat screenshots.\n"
                    "Extract ONLY players whose row clearly shows the button/status text 'Join'.\n"
                    "Ignore every row that does not say 'Join'.\n"
                    "Ignore headers, empty slots, totals, substitutes, and anything unclear.\n"
                    "Return ONLY valid JSON in this exact shape:\n"
                    "{\n"
                    '  "players": [\n'
                    '    {"name": "Player Name", "power_text": "616m"}\n'
                    "  ]\n"
                    "}\n"
                    "Do not wrap in markdown. Do not add commentary."
                ),
            }
        ]

        for attachment in attachments:
            if not is_image_attachment(attachment):
                raise ValueError(f"{attachment.filename} is not a supported image.")

            raw = await attachment.read()
            mime = get_attachment_mime(attachment)
            b64 = base64.b64encode(raw).decode("utf-8")
            data_url = f"data:{mime};base64,{b64}"

            content_parts.append(
                {
                    "type": "input_image",
                    "image_url": data_url,
                }
            )

        response = await client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise data extractor. "
                        "Return only the requested JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": content_parts,
                },
            ],
        )

        raw_text = self._extract_response_text(response)
        return parse_scan_payload(raw_text)

    @app_commands.command(name="canyon_scan", description="Privately scan Canyon screenshots attached to this command")
    @app_commands.describe(
        image_1="Canyon screenshot 1",
        image_2="Canyon screenshot 2",
        image_3="Canyon screenshot 3",
        image_4="Canyon screenshot 4",
        image_5="Canyon screenshot 5",
        image_6="Canyon screenshot 6",
        image_7="Canyon screenshot 7",
        image_8="Canyon screenshot 8",
        image_9="Canyon screenshot 9",
        image_10="Canyon screenshot 10",
        reset="Set true to start a fresh roster instead of adding to the saved one",
    )
    async def canyon_scan(
        self,
        interaction: discord.Interaction,
        image_1: discord.Attachment,
        image_2: Optional[discord.Attachment] = None,
        image_3: Optional[discord.Attachment] = None,
        image_4: Optional[discord.Attachment] = None,
        image_5: Optional[discord.Attachment] = None,
        image_6: Optional[discord.Attachment] = None,
        image_7: Optional[discord.Attachment] = None,
        image_8: Optional[discord.Attachment] = None,
        image_9: Optional[discord.Attachment] = None,
        image_10: Optional[discord.Attachment] = None,
        reset: bool = False,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            attachments = [
                a
                for a in (
                    image_1,
                    image_2,
                    image_3,
                    image_4,
                    image_5,
                    image_6,
                    image_7,
                    image_8,
                    image_9,
                    image_10,
                )
                if a is not None
            ]

            players = await self._extract_from_attachments(attachments)

            if not players:
                await interaction.followup.send(
                    "No joined players were extracted. Make sure the attached screenshots clearly show the Join rows.",
                    ephemeral=True,
                )
                return

            guild_id = interaction.guild_id or interaction.user.id
            stats = self._store_roster(
                guild_id,
                players,
                reset=reset,
                image_count=len(attachments),
                user_id=interaction.user.id,
            )

            saved_players = self._load_roster(guild_id)
            mode_text = "fresh roster / reset" if reset else "append to saved roster"
            body = (
                f"Mode: {mode_text}\n"
                f"Images scanned this batch: {len(attachments)}\n"
                f"Previous saved players: {stats['previous_total']}\n"
                f"Extracted this batch: {stats['extracted']}\n"
                f"Added: {stats['added']}\n"
                f"Updated higher-power duplicates: {stats['updated']}\n"
                f"Ignored duplicates: {stats['ignored_duplicates']}\n"
                f"Saved roster total: {stats['saved_total']}\n"
                "Source: command attachments only; no channel screenshots were scraped.\n"
                "Run `/canyon_scan` again with more screenshots to keep adding to this roster.\n"
                "Use `reset:true` or `/canyon_clear` to start again.\n\n"
                + roster_text(saved_players)
            )
            header = f"Saved canyon roster: {len(saved_players)} players"
            await send_long_message(interaction, header, body, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"Scan failed: {e}", ephemeral=True)

    @app_commands.command(name="canyon_list", description="Privately show the last scanned canyon roster")
    async def canyon_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        guild_id = interaction.guild_id or interaction.user.id
        players = self._load_roster(guild_id)

        if not players:
            await interaction.followup.send("No saved canyon roster. Run `/canyon_scan` first.", ephemeral=True)
            return

        await send_long_message(interaction, f"Saved roster ({len(players)})", roster_text(players), ephemeral=True)

    @app_commands.command(name="canyon_rows", description="Privately build balanced canyon rows and save them as a draft")
    @app_commands.describe(
        leaders="Exactly 4 leaders in lane order: Left, Left middle, Right middle, Right",
        combine="Optional. Example: AstraJ+Asteria; Name3+Name4",
        exclude="Optional. Example: Pigu, Name2",
    )
    async def canyon_rows(
        self,
        interaction: discord.Interaction,
        leaders: str,
        combine: Optional[str] = None,
        exclude: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            guild_id = interaction.guild_id or interaction.user.id
            players = self._load_roster(guild_id)

            if not players:
                await interaction.followup.send("No saved canyon roster. Run `/canyon_scan` first.", ephemeral=True)
                return

            rows, totals, working_players = build_balanced_rows(
                players=players,
                leaders_csv=leaders,
                combine_raw=combine,
                exclude_csv=exclude,
            )

            leader_names = parse_csv_names(leaders)
            summary = (
                f"Lane leaders: "
                f"Left={leader_names[0]} | "
                f"Left middle={leader_names[1]} | "
                f"Right middle={leader_names[2]} | "
                f"Right={leader_names[3]}\n"
                f"Players used: {sum(len(v) for v in rows.values())}\n"
                f"Source roster size: {len(working_players)}\n\n"
            )

            draft_body = summary + rows_text(rows, totals)

            sessions = load_sessions(self.bot, guild_id)
            sessions["last_rows"] = {
                "updated_at": int(time.time()),
                "created_by": interaction.user.id,
                "title": "Canyon rows",
                "body": draft_body,
            }
            save_sessions(guild_id, sessions)

            await send_long_message(
                interaction,
                "Private canyon row draft",
                draft_body + "\n\nRun `/canyon_post` when this is ready to publish.",
                ephemeral=True,
            )

        except Exception as e:
            await interaction.followup.send(f"Row build failed: {e}", ephemeral=True)

    @app_commands.command(name="canyon_post", description="Post the last private canyon row draft publicly")
    @app_commands.describe(title="Optional title for the public post")
    async def canyon_post(
        self,
        interaction: discord.Interaction,
        title: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        guild_id = interaction.guild_id or interaction.user.id
        sessions = load_sessions(self.bot, guild_id)
        draft = sessions.get("last_rows")

        if not draft or not draft.get("body"):
            await interaction.followup.send(
                "No canyon row draft is ready. Run `/canyon_rows` first.",
                ephemeral=True,
            )
            return

        post_title = title or draft.get("title") or "Canyon rows"
        body = str(draft.get("body", "")).strip()

        content = f"**{post_title}**\n```text\n{body}\n```"
        channel = interaction.channel

        if channel is None or not hasattr(channel, "send"):
            await interaction.followup.send("Could not post in this channel.", ephemeral=True)
            return

        if len(content) <= 1900:
            await channel.send(content)
        else:
            fp = io.BytesIO(body.encode("utf-8"))
            safe_title = re.sub(r"[^a-z0-9_ -]", "", post_title.lower()).strip().replace(" ", "_") or "canyon_rows"
            file = discord.File(fp, filename=f"{safe_title}.txt")
            await channel.send(f"**{post_title}**", file=file)

        await interaction.followup.send("Posted canyon rows publicly.", ephemeral=True)


    @app_commands.command(name="canyon_clear", description="Privately clear the saved canyon roster and row draft")
    async def canyon_clear(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        guild_id = interaction.guild_id or interaction.user.id
        sessions = load_sessions(self.bot, guild_id)
        sessions.pop("roster", None)
        sessions.pop(str(guild_id), None)
        sessions.pop("last_rows", None)
        sessions.pop("scan_batches", None)
        save_sessions(guild_id, sessions)

        await interaction.followup.send("Saved canyon roster and row draft cleared.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    from core.command_scope import bind_public_cog

    cog = CanyonCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
