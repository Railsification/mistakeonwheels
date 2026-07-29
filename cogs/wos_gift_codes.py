# cogs/wos_gift_codes.py
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog, public_guild_ids
from core.logger import err, info, log_cmd, warn
from core.settings import SettingsManager
from core.storage import load_guild_json, save_guild_json
from core.utils import ensure_deferred


FEATURE_NAME = "gift_codes"
DATA_FILENAME = "wos_gift_codes.json"
API_BASE_URL = "https://wos-giftcode-api.centurygame.com/api"
GIFT_CODE_ENDPOINT = f"{API_BASE_URL}/gift_code"
ENCRYPT_KEY = "tB87#kPtkxqOS2"
API_CONTRACT_DATE = "2026-07-22"

MAX_ACCOUNTS_PER_USER = 10
MAX_CODES_PER_MESSAGE = 3
REQUEST_TIMEOUT_SECONDS = 25
THROTTLE_RETRY_SECONDS = 5
MAX_API_ATTEMPTS = 4
INTER_ACCOUNT_DELAY_SECONDS = 0.35

FID_RE = re.compile(r"^[0-9]{5,20}$")
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,31}$")
PREFIXED_CODE_RE = re.compile(
    r"(?i)(?:gift\s*code|redeem\s*code|code)\s*[:=\-]\s*([A-Za-z0-9][A-Za-z0-9_-]{3,31})"
)

TERMINAL_ACCOUNT_STATUSES = {
    "success",
    "already_used",
    "usage_limit",
    "too_small",
    "same_type",
    "kid_mismatch",
}
RETRYABLE_ACCOUNT_STATUSES = {
    "failed",
    "throttled",
    "api_error",
    "network_error",
}
CODE_TERMINAL_STATUSES = {"invalid", "expired", "limit_reached", "api_changed"}

ERROR_STATUS_BY_CODE = {
    20000: "success",
    40008: "already_used",
    40005: "limit_reached",
    40007: "expired",
    40014: "invalid",
    40006: "usage_limit",
    40010: "too_small",
    40011: "same_type",
    40020: "kid_mismatch",
    40019: "throttled",
}

STATUS_LABELS = {
    "queued": "Queued",
    "processing": "Processing",
    "complete": "Complete",
    "complete_with_errors": "Complete with errors",
    "no_accounts": "No registered accounts",
    "invalid": "Invalid code",
    "expired": "Expired code",
    "limit_reached": "Global limit reached",
    "api_changed": "WoS API changed",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_blob() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "accounts": {},
        "codes": {},
    }


def _normalise_blob(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = _default_blob()

    accounts = raw.get("accounts")
    codes = raw.get("codes")

    if not isinstance(accounts, dict):
        accounts = {}
    if not isinstance(codes, dict):
        codes = {}

    return {
        "schema_version": 1,
        "accounts": accounts,
        "codes": codes,
    }


def _normalise_code(raw: str) -> str:
    return (raw or "").strip().strip("`'\".,;:!?()[]{}<>")


def _extract_codes_from_message(content: str) -> list[str]:
    text = (content or "").strip()
    if not text or len(text) > 300:
        return []

    found: list[str] = []

    for match in PREFIXED_CODE_RE.finditer(text):
        code = _normalise_code(match.group(1))
        if CODE_RE.fullmatch(code) and code not in found:
            found.append(code)

    if not found:
        candidate = _normalise_code(text)
        if CODE_RE.fullmatch(candidate):
            found.append(candidate)

    return found[:MAX_CODES_PER_MESSAGE]


def _extract_err_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None

    direct = payload.get("err_code")
    try:
        if direct is not None:
            return int(direct)
    except (TypeError, ValueError):
        pass

    data = payload.get("data")
    if isinstance(data, dict):
        nested = _extract_err_code(data)
        if nested is not None:
            return nested

    code = payload.get("code")
    try:
        numeric = int(code)
    except (TypeError, ValueError):
        numeric = None

    if numeric in ERROR_STATUS_BY_CODE:
        return numeric

    return None


def _extract_message(payload: Any, fallback: str = "") -> str:
    if not isinstance(payload, dict):
        return fallback[:300]

    for key in ("msg", "message", "error", "error_message"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)[:300]

    data = payload.get("data")
    if isinstance(data, dict):
        nested = _extract_message(data, "")
        if nested:
            return nested[:300]

    return fallback[:300]


def _result_counts(entry: dict[str, Any], total_accounts: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    results = entry.get("results")
    if not isinstance(results, dict):
        results = {}

    for result in results.values():
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "failed")
        counts[status] = counts.get(status, 0) + 1

    counts["pending"] = max(0, total_accounts - len(results))
    return counts


class WOSGiftCodesCog(commands.Cog):
    gift = app_commands.Group(
        name="gift",
        description="Whiteout Survival gift-code registration and redemption",
    )

    HELP_META = {
        "title": "WoS Gift Codes",
        "summary": "Registers WoS accounts and automatically redeems gift codes posted in configured channels.",
        "details": (
            "Configure a channel with `/council feature_channel_add` using the feature "
            "`gift_codes`. Members then register with `/gift register`. A code posted by "
            "itself, or as `Gift code: CODE`, is queued automatically."
        ),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._storage_locks: dict[int, asyncio.Lock] = {}
        self._queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self._queued_keys: set[tuple[int, str]] = set()
        self._worker_task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None

        # Register the dynamic feature key so it immediately appears in the
        # existing /council feature autocomplete without changing admin.py.
        for guild_id in public_guild_ids(bot, include_admin=True):
            self.settings.feature_channels(guild_id, FEATURE_NAME)

    async def cog_load(self) -> None:
        self._worker_task = asyncio.create_task(
            self._redemption_worker(),
            name="hotbot-wos-gift-redemption-worker",
        )
        await self._recover_queued_codes()
        info("WoS gift-code redeemer loaded")

    def cog_unload(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
        if self._session is not None and not self._session.closed:
            asyncio.create_task(self._session.close())

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        guild_id = int(guild_id)
        lock = self._storage_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._storage_locks[guild_id] = lock
        return lock

    def _load_blob(self, guild_id: int) -> dict[str, Any]:
        return _normalise_blob(
            load_guild_json(guild_id, DATA_FILENAME, _default_blob())
        )

    def _save_blob(self, guild_id: int, blob: dict[str, Any]) -> None:
        save_guild_json(guild_id, DATA_FILENAME, _normalise_blob(blob))

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-AU,en;q=0.9",
                    "Origin": "https://wos-giftcode.centurygame.com",
                    "Referer": "https://wos-giftcode.centurygame.com/",
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 "
                        "Chrome/138.0 Mobile Safari/537.36"
                    ),
                },
            )
        return self._session

    def _is_allowed_channel(self, guild_id: int | None, channel_id: int | None) -> bool:
        return self.settings.is_feature_allowed(
            guild_id,
            channel_id,
            FEATURE_NAME,
        )

    async def _require_feature_channel(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if self._is_allowed_channel(interaction.guild_id, interaction.channel_id):
            return True

        message = (
            "❌ WoS gift-code commands only work in a configured gift-code channel.\n"
            "An admin can add one with `/council feature_channel_add` and feature "
            "`gift_codes`."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    @staticmethod
    def _can_manage_guild(interaction: discord.Interaction) -> bool:
        member = interaction.user
        return isinstance(member, discord.Member) and (
            member.guild_permissions.administrator
            or member.guild_permissions.manage_guild
        )

    async def _recover_queued_codes(self) -> None:
        for guild_id in public_guild_ids(self.bot, include_admin=True):
            async with self._lock_for(guild_id):
                blob = self._load_blob(guild_id)
                changed = False
                codes = blob["codes"]

                for code, entry in codes.items():
                    if not isinstance(entry, dict):
                        continue
                    status = str(entry.get("status") or "")
                    if status == "processing":
                        entry["status"] = "queued"
                        changed = True
                    if entry.get("status") == "queued":
                        self._enqueue(guild_id, code)

                if changed:
                    self._save_blob(guild_id, blob)

    def _enqueue(self, guild_id: int, code: str) -> None:
        key = (int(guild_id), code)
        if key in self._queued_keys:
            return
        self._queued_keys.add(key)
        self._queue.put_nowait(key)

    async def _create_or_queue_code(
        self,
        *,
        guild_id: int,
        code: str,
        submitter_id: int,
        channel_id: int,
        source_message_id: int = 0,
        force: bool = False,
    ) -> tuple[bool, str, dict[str, Any]]:
        code = _normalise_code(code)
        if not CODE_RE.fullmatch(code):
            return False, "invalid_format", {}

        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            codes = blob["codes"]
            existing = codes.get(code)

            if isinstance(existing, dict) and not force:
                status = str(existing.get("status") or "unknown")
                return False, status, existing

            if not isinstance(existing, dict):
                existing = {
                    "code": code,
                    "submitted_at": _utc_now(),
                    "submitted_by": int(submitter_id),
                    "channel_id": int(channel_id),
                    "source_message_id": int(source_message_id or 0),
                    "status_message_id": 0,
                    "status": "queued",
                    "results": {},
                }
            else:
                existing["status"] = "queued"
                existing["last_requeued_at"] = _utc_now()
                existing["last_requeued_by"] = int(submitter_id)
                existing["channel_id"] = int(channel_id)

            codes[code] = existing
            self._save_blob(guild_id, blob)

        return True, "queued", existing

    async def _set_status_message(
        self,
        guild_id: int,
        code: str,
        channel_id: int,
        message_id: int,
    ) -> None:
        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            entry = blob["codes"].get(code)
            if not isinstance(entry, dict):
                return
            entry["channel_id"] = int(channel_id)
            entry["status_message_id"] = int(message_id)
            self._save_blob(guild_id, blob)

    async def _account_snapshot(self, guild_id: int) -> dict[str, dict[str, Any]]:
        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            output: dict[str, dict[str, Any]] = {}
            for fid, account in blob["accounts"].items():
                if not isinstance(account, dict) or not account.get("enabled", True):
                    continue
                output[str(fid)] = dict(account)
            return output

    async def _entry_snapshot(self, guild_id: int, code: str) -> dict[str, Any] | None:
        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            entry = blob["codes"].get(code)
            return dict(entry) if isinstance(entry, dict) else None

    async def _save_result(
        self,
        guild_id: int,
        code: str,
        fid: str,
        result: dict[str, Any],
    ) -> None:
        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            entry = blob["codes"].get(code)
            if not isinstance(entry, dict):
                return
            results = entry.setdefault("results", {})
            if not isinstance(results, dict):
                results = {}
                entry["results"] = results
            results[str(fid)] = result
            self._save_blob(guild_id, blob)

    async def _set_code_status(
        self,
        guild_id: int,
        code: str,
        status: str,
    ) -> None:
        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            entry = blob["codes"].get(code)
            if not isinstance(entry, dict):
                return
            entry["status"] = status
            entry["updated_at"] = _utc_now()
            if status not in {"queued", "processing"}:
                entry["completed_at"] = _utc_now()
            self._save_blob(guild_id, blob)

    async def _redemption_worker(self) -> None:
        while True:
            guild_id, code = await self._queue.get()
            self._queued_keys.discard((guild_id, code))
            try:
                await self._process_code(guild_id, code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err(f"WoS gift-code worker failed for guild {guild_id}, code {code}: {exc!r}")
                await self._set_code_status(guild_id, code, "complete_with_errors")
                await self._publish_progress(guild_id, code)
            finally:
                self._queue.task_done()

    async def _process_code(self, guild_id: int, code: str) -> None:
        await self._set_code_status(guild_id, code, "processing")
        accounts = await self._account_snapshot(guild_id)

        if not accounts:
            await self._set_code_status(guild_id, code, "no_accounts")
            await self._publish_progress(guild_id, code)
            return

        await self._publish_progress(guild_id, code)
        code_terminal_status: str | None = None
        processed_since_update = 0
        last_update = time.monotonic()

        for fid, account in accounts.items():
            current_entry = await self._entry_snapshot(guild_id, code)
            if current_entry is None:
                return

            existing_results = current_entry.get("results")
            if not isinstance(existing_results, dict):
                existing_results = {}
            previous = existing_results.get(fid)
            if isinstance(previous, dict):
                previous_status = str(previous.get("status") or "")
                if previous_status in TERMINAL_ACCOUNT_STATUSES:
                    continue

            try:
                kid = int(account.get("kid") or 0)
            except (TypeError, ValueError):
                kid = 0

            if kid <= 0:
                result = {
                    "status": "kid_mismatch",
                    "err_code": 40020,
                    "message": "Missing or invalid state number.",
                    "attempted_at": _utc_now(),
                    "attempts": 0,
                }
            else:
                result = await self._redeem_one(fid=fid, kid=kid, code=code)

            await self._save_result(guild_id, code, fid, result)
            processed_since_update += 1

            if result["status"] in CODE_TERMINAL_STATUSES:
                code_terminal_status = str(result["status"])
                break

            now = time.monotonic()
            if processed_since_update >= 10 or now - last_update >= 5:
                await self._publish_progress(guild_id, code)
                processed_since_update = 0
                last_update = now

            await asyncio.sleep(INTER_ACCOUNT_DELAY_SECONDS)

        if code_terminal_status is not None:
            final_status = code_terminal_status
        else:
            entry = await self._entry_snapshot(guild_id, code) or {}
            results = entry.get("results")
            if not isinstance(results, dict):
                results = {}
            has_retryable_error = any(
                isinstance(result, dict)
                and str(result.get("status") or "") in RETRYABLE_ACCOUNT_STATUSES
                for result in results.values()
            )
            final_status = "complete_with_errors" if has_retryable_error else "complete"

        await self._set_code_status(guild_id, code, final_status)
        await self._publish_progress(guild_id, code)

    async def _redeem_one(self, *, fid: str, kid: int, code: str) -> dict[str, Any]:
        session = await self._get_session()
        last_message = "No response from WoS."
        last_status = "network_error"
        last_err_code: int | None = None

        for attempt in range(1, MAX_API_ATTEMPTS + 1):
            unix_time = int(time.time())
            sign_source = (
                f"cdk={code}&fid={fid}&kid={kid}&time={unix_time}{ENCRYPT_KEY}"
            )
            signature = hashlib.md5(sign_source.encode("utf-8")).hexdigest()
            form = {
                "cdk": code,
                "fid": str(fid),
                "kid": str(kid),
                "time": str(unix_time),
                "sign": signature,
            }

            try:
                async with session.post(GIFT_CODE_ENDPOINT, data=form) as response:
                    raw_text = await response.text()
                    try:
                        payload = json.loads(raw_text)
                    except json.JSONDecodeError:
                        payload = {}

                    if response.status == 404:
                        return {
                            "status": "api_changed",
                            "err_code": None,
                            "http_status": response.status,
                            "message": (
                                "Century Games returned HTTP 404. The gift-code API contract changed."
                            ),
                            "attempted_at": _utc_now(),
                            "attempts": attempt,
                        }

                    if response.status == 429 or response.status >= 500:
                        last_status = "api_error"
                        last_message = f"WoS HTTP {response.status}."
                        if attempt < MAX_API_ATTEMPTS:
                            await asyncio.sleep(min(2 ** attempt, 10))
                            continue

                    err_code = _extract_err_code(payload)
                    message = _extract_message(payload, raw_text)
                    status = ERROR_STATUS_BY_CODE.get(err_code, "failed")

                    if err_code == 40019 and attempt < MAX_API_ATTEMPTS:
                        last_status = "throttled"
                        last_message = message or "WoS throttled this player."
                        last_err_code = err_code
                        await asyncio.sleep(THROTTLE_RETRY_SECONDS)
                        continue

                    if err_code is None:
                        wrapper_code = payload.get("code") if isinstance(payload, dict) else None
                        message_lower = (message or "").lower()
                        if wrapper_code in (0, "0") and "success" in message_lower:
                            status = "success"
                            err_code = 20000
                        elif response.status >= 400:
                            status = "api_error"

                    return {
                        "status": status,
                        "err_code": err_code,
                        "http_status": response.status,
                        "message": message or status.replace("_", " ").title(),
                        "attempted_at": _utc_now(),
                        "attempts": attempt,
                    }

            except asyncio.TimeoutError:
                last_status = "network_error"
                last_message = "WoS request timed out."
            except aiohttp.ClientError as exc:
                last_status = "network_error"
                last_message = f"Network error: {exc}"

            if attempt < MAX_API_ATTEMPTS:
                await asyncio.sleep(min(2 ** attempt, 10))

        return {
            "status": last_status,
            "err_code": last_err_code,
            "message": last_message,
            "attempted_at": _utc_now(),
            "attempts": MAX_API_ATTEMPTS,
        }

    def _progress_embed(
        self,
        code: str,
        entry: dict[str, Any],
        total_accounts: int,
    ) -> discord.Embed:
        status = str(entry.get("status") or "queued")
        counts = _result_counts(entry, total_accounts)

        if status == "processing":
            colour = discord.Colour.blurple()
        elif status in {"complete", "no_accounts"}:
            colour = discord.Colour.green()
        elif status == "queued":
            colour = discord.Colour.gold()
        else:
            colour = discord.Colour.red()

        embed = discord.Embed(
            title=f"🎁 WoS Gift Code — {code}",
            description=f"**Status:** {STATUS_LABELS.get(status, status.replace('_', ' ').title())}",
            colour=colour,
        )
        embed.add_field(name="Registered", value=str(total_accounts), inline=True)
        embed.add_field(name="✅ Redeemed", value=str(counts.get("success", 0)), inline=True)
        embed.add_field(name="📬 Already used", value=str(counts.get("already_used", 0)), inline=True)

        restricted = sum(
            counts.get(key, 0)
            for key in ("usage_limit", "too_small", "same_type")
        )
        problems = sum(
            counts.get(key, 0)
            for key in (
                "kid_mismatch",
                "failed",
                "throttled",
                "api_error",
                "network_error",
            )
        )

        embed.add_field(name="⚠️ Restricted", value=str(restricted), inline=True)
        embed.add_field(name="❌ Problems", value=str(problems), inline=True)
        embed.add_field(name="⏳ Pending", value=str(counts.get("pending", 0)), inline=True)

        if status == "invalid":
            embed.add_field(
                name="Result",
                value="WoS says this gift code does not exist.",
                inline=False,
            )
        elif status == "expired":
            embed.add_field(
                name="Result",
                value="WoS says this gift code has expired.",
                inline=False,
            )
        elif status == "limit_reached":
            embed.add_field(
                name="Result",
                value="The code's global redemption limit has been reached.",
                inline=False,
            )
        elif status == "api_changed":
            embed.add_field(
                name="Action required",
                value=(
                    "Century Games changed the redemption API. Update "
                    "`cogs/wos_gift_codes.py` before retrying."
                ),
                inline=False,
            )
        elif status == "no_accounts":
            embed.add_field(
                name="Nothing redeemed",
                value="No enabled WoS accounts are registered in this server.",
                inline=False,
            )
        elif status in {"complete", "complete_with_errors"}:
            embed.add_field(
                name="Finished",
                value="Successful rewards are delivered through in-game mail.",
                inline=False,
            )

        embed.set_footer(
            text=f"WoS signed gift-code API contract checked {API_CONTRACT_DATE}"
        )
        return embed

    async def _publish_progress(self, guild_id: int, code: str) -> None:
        entry = await self._entry_snapshot(guild_id, code)
        if entry is None:
            return
        accounts = await self._account_snapshot(guild_id)
        embed = self._progress_embed(code, entry, len(accounts))

        channel_id = int(entry.get("channel_id") or 0)
        message_id = int(entry.get("status_message_id") or 0)
        channel = self.bot.get_channel(channel_id)

        if channel is None or not isinstance(
            channel,
            (discord.TextChannel, discord.Thread),
        ):
            return

        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            message = await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            warn(f"Could not publish WoS gift-code progress in guild {guild_id}: {exc!r}")
            return

        await self._set_status_message(guild_id, code, channel.id, message.id)

    async def _queue_and_publish(
        self,
        *,
        guild_id: int,
        channel: discord.TextChannel | discord.Thread,
        code: str,
        submitter_id: int,
        source_message_id: int = 0,
        force: bool = False,
    ) -> tuple[bool, str]:
        accepted, reason, _entry = await self._create_or_queue_code(
            guild_id=guild_id,
            code=code,
            submitter_id=submitter_id,
            channel_id=channel.id,
            source_message_id=source_message_id,
            force=force,
        )
        if not accepted:
            return False, reason

        await self._publish_progress(guild_id, code)
        self._enqueue(guild_id, code)
        return True, "queued"

    # ---------- message auto-detection ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return
        if not self._is_allowed_channel(message.guild.id, message.channel.id):
            return

        codes = _extract_codes_from_message(message.content)
        if not codes:
            return

        queued_any = False
        duplicate_any = False

        for code in codes:
            accepted, _reason = await self._queue_and_publish(
                guild_id=message.guild.id,
                channel=message.channel,
                code=code,
                submitter_id=message.author.id,
                source_message_id=message.id,
            )
            queued_any = queued_any or accepted
            duplicate_any = duplicate_any or not accepted

        try:
            if queued_any:
                await message.add_reaction("✅")
            elif duplicate_any:
                await message.add_reaction("♻️")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ---------- slash commands ----------

    @gift.command(name="register", description="Register one of your WoS accounts for automatic gift codes.")
    @app_commands.describe(
        fid="Your numeric WoS FID / Player ID",
        state="Your current WoS state number",
        label="Optional name such as Main or Farm",
    )
    async def register(
        self,
        interaction: discord.Interaction,
        fid: str,
        state: app_commands.Range[int, 1, 999999],
        label: app_commands.Range[str, 1, 30] | None = None,
    ) -> None:
        log_cmd("gift register", interaction)
        if not await self._require_feature_channel(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        clean_fid = (fid or "").strip()
        if not FID_RE.fullmatch(clean_fid):
            await interaction.followup.send(
                "❌ FID must contain only numbers and be between 5 and 20 digits.",
                ephemeral=True,
            )
            return

        guild_id = int(interaction.guild_id or 0)
        user_id = int(interaction.user.id)

        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            accounts = blob["accounts"]
            existing = accounts.get(clean_fid)

            if isinstance(existing, dict):
                owner_id = int(existing.get("discord_user_id") or 0)
                if owner_id and owner_id != user_id:
                    await interaction.followup.send(
                        "❌ That FID is already registered to another Discord member in this server.",
                        ephemeral=True,
                    )
                    return

            owned_count = sum(
                1
                for account in accounts.values()
                if isinstance(account, dict)
                and int(account.get("discord_user_id") or 0) == user_id
                and str(account.get("fid") or "") != clean_fid
            )
            if owned_count >= MAX_ACCOUNTS_PER_USER:
                await interaction.followup.send(
                    f"❌ You can register up to {MAX_ACCOUNTS_PER_USER} WoS accounts.",
                    ephemeral=True,
                )
                return

            accounts[clean_fid] = {
                "fid": clean_fid,
                "kid": int(state),
                "label": (label or "").strip(),
                "discord_user_id": user_id,
                "enabled": True,
                "registered_at": (
                    existing.get("registered_at")
                    if isinstance(existing, dict) and existing.get("registered_at")
                    else _utc_now()
                ),
                "updated_at": _utc_now(),
            }
            self._save_blob(guild_id, blob)

        label_text = f" ({label.strip()})" if label else ""
        await interaction.followup.send(
            f"✅ Registered FID `{clean_fid}`{label_text} in **State {int(state)}**.\n"
            "Future gift codes posted in this channel will be redeemed automatically.",
            ephemeral=True,
        )

    @gift.command(name="accounts", description="Show the WoS accounts registered to you in this server.")
    async def accounts(self, interaction: discord.Interaction) -> None:
        log_cmd("gift accounts", interaction)
        if not await self._require_feature_channel(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        guild_id = int(interaction.guild_id or 0)
        user_id = int(interaction.user.id)
        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            owned = [
                account
                for account in blob["accounts"].values()
                if isinstance(account, dict)
                and int(account.get("discord_user_id") or 0) == user_id
            ]

        if not owned:
            await interaction.followup.send(
                "You have no registered WoS accounts. Use `/gift register`.",
                ephemeral=True,
            )
            return

        owned.sort(key=lambda item: (str(item.get("label") or ""), str(item.get("fid") or "")))
        lines = []
        for account in owned:
            fid = str(account.get("fid") or "unknown")
            kid = int(account.get("kid") or 0)
            label = str(account.get("label") or "").strip()
            enabled = "enabled" if account.get("enabled", True) else "disabled"
            suffix = f" — **{label}**" if label else ""
            lines.append(f"• `{fid}` — State **{kid}**{suffix} — {enabled}")

        await interaction.followup.send(
            "**Your registered WoS accounts**\n" + "\n".join(lines),
            ephemeral=True,
        )

    @gift.command(name="remove", description="Remove one of your registered WoS accounts.")
    @app_commands.describe(fid="The FID to remove")
    async def remove(self, interaction: discord.Interaction, fid: str) -> None:
        log_cmd("gift remove", interaction)
        if not await self._require_feature_channel(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        clean_fid = (fid or "").strip()
        guild_id = int(interaction.guild_id or 0)
        user_id = int(interaction.user.id)

        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            account = blob["accounts"].get(clean_fid)
            if not isinstance(account, dict):
                await interaction.followup.send(
                    "❌ That FID is not registered in this server.",
                    ephemeral=True,
                )
                return
            if int(account.get("discord_user_id") or 0) != user_id:
                await interaction.followup.send(
                    "❌ You can only remove accounts registered to your Discord user.",
                    ephemeral=True,
                )
                return

            del blob["accounts"][clean_fid]
            self._save_blob(guild_id, blob)

        await interaction.followup.send(
            f"✅ Removed FID `{clean_fid}` from automatic gift-code redemption.",
            ephemeral=True,
        )

    @gift.command(name="redeem", description="Queue a WoS gift code for every registered account.")
    @app_commands.describe(code="The exact WoS gift code")
    async def redeem(self, interaction: discord.Interaction, code: str) -> None:
        log_cmd("gift redeem", interaction)
        if not await self._require_feature_channel(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("❌ This command needs a server text channel.", ephemeral=True)
            return

        clean_code = _normalise_code(code)
        accepted, reason = await self._queue_and_publish(
            guild_id=int(interaction.guild_id or 0),
            channel=channel,
            code=clean_code,
            submitter_id=interaction.user.id,
        )

        if accepted:
            await interaction.followup.send(
                f"✅ `{clean_code}` was queued. Progress is shown in the channel.",
                ephemeral=True,
            )
        elif reason == "invalid_format":
            await interaction.followup.send(
                "❌ Gift codes must be 4–32 characters using letters, numbers, `_` or `-`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"♻️ `{clean_code}` is already stored with status **{reason.replace('_', ' ')}**.",
                ephemeral=True,
            )

    @gift.command(name="status", description="Show the saved result for a gift code.")
    @app_commands.describe(code="Gift code; leave blank to show the latest code")
    async def status(
        self,
        interaction: discord.Interaction,
        code: str | None = None,
    ) -> None:
        log_cmd("gift status", interaction)
        if not await self._require_feature_channel(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        guild_id = int(interaction.guild_id or 0)
        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            codes = blob["codes"]

            if code:
                clean_code = _normalise_code(code)
                entry = codes.get(clean_code)
                selected_code = clean_code
            else:
                entries = [
                    (stored_code, entry)
                    for stored_code, entry in codes.items()
                    if isinstance(entry, dict)
                ]
                entries.sort(
                    key=lambda item: str(item[1].get("submitted_at") or ""),
                    reverse=True,
                )
                if entries:
                    selected_code, entry = entries[0]
                else:
                    selected_code, entry = "", None

            total_accounts = sum(
                1
                for account in blob["accounts"].values()
                if isinstance(account, dict) and account.get("enabled", True)
            )

        if not isinstance(entry, dict):
            await interaction.followup.send(
                "No saved gift-code result was found.",
                ephemeral=True,
            )
            return

        embed = self._progress_embed(selected_code, entry, total_accounts)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @gift.command(name="retry", description="Retry failed accounts for a stored gift code (Manage Server).")
    @app_commands.describe(code="The saved gift code to retry")
    async def retry(self, interaction: discord.Interaction, code: str) -> None:
        log_cmd("gift retry", interaction)
        if not await self._require_feature_channel(interaction):
            return
        if not self._can_manage_guild(interaction):
            await interaction.response.send_message(
                "❌ Manage Server permission is required.",
                ephemeral=True,
            )
            return
        await ensure_deferred(interaction, ephemeral=True)

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("❌ This command needs a server text channel.", ephemeral=True)
            return

        clean_code = _normalise_code(code)
        guild_id = int(interaction.guild_id or 0)

        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            existing = blob["codes"].get(clean_code)
            if not isinstance(existing, dict):
                await interaction.followup.send(
                    "❌ That gift code is not stored.",
                    ephemeral=True,
                )
                return

            results = existing.get("results")
            if not isinstance(results, dict):
                results = {}
                existing["results"] = results

            for fid in list(results.keys()):
                result = results.get(fid)
                if isinstance(result, dict) and str(result.get("status") or "") in RETRYABLE_ACCOUNT_STATUSES:
                    del results[fid]

            existing["status"] = "queued"
            existing["channel_id"] = channel.id
            existing["last_requeued_at"] = _utc_now()
            existing["last_requeued_by"] = interaction.user.id
            self._save_blob(guild_id, blob)

        await self._publish_progress(guild_id, clean_code)
        self._enqueue(guild_id, clean_code)
        await interaction.followup.send(
            f"✅ Failed accounts for `{clean_code}` were queued again.",
            ephemeral=True,
        )

    @gift.command(name="server_accounts", description="List every registered FID in this server (Manage Server).")
    async def server_accounts(self, interaction: discord.Interaction) -> None:
        log_cmd("gift server_accounts", interaction)
        if not await self._require_feature_channel(interaction):
            return
        if not self._can_manage_guild(interaction):
            await interaction.response.send_message(
                "❌ Manage Server permission is required.",
                ephemeral=True,
            )
            return
        await ensure_deferred(interaction, ephemeral=True)

        guild_id = int(interaction.guild_id or 0)
        async with self._lock_for(guild_id):
            blob = self._load_blob(guild_id)
            accounts = [
                account
                for account in blob["accounts"].values()
                if isinstance(account, dict)
            ]

        if not accounts:
            await interaction.followup.send(
                "No WoS accounts are registered in this server.",
                ephemeral=True,
            )
            return

        accounts.sort(key=lambda item: (int(item.get("kid") or 0), str(item.get("fid") or "")))
        lines = []
        for account in accounts[:75]:
            fid = str(account.get("fid") or "unknown")
            kid = int(account.get("kid") or 0)
            owner_id = int(account.get("discord_user_id") or 0)
            label = str(account.get("label") or "").strip()
            suffix = f" — {label}" if label else ""
            lines.append(f"• `{fid}` — State {kid}{suffix} — <@{owner_id}>")

        if len(accounts) > 75:
            lines.append(f"…and {len(accounts) - 75} more.")

        chunks: list[str] = []
        current = ""
        for line in lines:
            addition = ("\n" if current else "") + line
            if len(current) + len(addition) > 1900:
                chunks.append(current)
                current = line
            else:
                current += addition
        if current:
            chunks.append(current)

        for index, chunk in enumerate(chunks):
            heading = f"**Registered WoS accounts: {len(accounts)}**\n" if index == 0 else ""
            await interaction.followup.send(
                heading + chunk,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    cog = WOSGiftCodesCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
