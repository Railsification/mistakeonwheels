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
BUILD_VERSION = "2026-07-29-api-validation-v8"

MAX_ACCOUNTS_PER_USER = 10
MAX_CODES_PER_MESSAGE = 3
REQUEST_TIMEOUT_SECONDS = 25
THROTTLE_RETRY_SECONDS = 5
MAX_API_ATTEMPTS = 4
INTER_ACCOUNT_DELAY_SECONDS = 3.0
AUTO_VALIDATION_COOLDOWN_SECONDS = 60
AUTO_VALIDATION_USER_COOLDOWN_SECONDS = 15
AUTO_VALIDATION_RETRY_DELAYS_SECONDS = (60, 300, 900)
FID_RE = re.compile(r"^[0-9]{5,20}$")
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,31}$")
PREFIXED_CODE_RE = re.compile(
    r"(?i)(?:gift\s*code|redeem\s*code|code)\s*[:=\-]\s*([A-Za-z0-9][A-Za-z0-9_-]{3,31})"
)

# Bare all-letter codes may be uppercase (OFFICIALSTORE) or deliberately
# mixed-case (gogowOS). Ordinary lowercase chat must not be treated as a code.
COMMON_CHAT_WORDS = {
    "AFTERNOON",
    "ANYONE",
    "EVERYONE",
    "GOODNIGHT",
    "HELLO",
    "LATER",
    "LOL",
    "MORNING",
    "NIGHT",
    "PLEASE",
    "THANKS",
    "THANKYOU",
    "WELCOME",
}

TERMINAL_ACCOUNT_STATUSES = {
    "success",
    "already_used",
    "usage_limit",
    "too_small",
    "same_type",
}

# These account-specific responses prove the submitted text is a genuine WoS
# gift code. A restricted validation account must not block the other accounts.
VALIDATION_PROVES_CODE_STATUSES = {
    "success",
    "already_used",
    "usage_limit",
    "too_small",
    "same_type",
}
VALIDATION_REJECTS_CODE_STATUSES = {"invalid", "expired", "limit_reached"}
VALIDATION_RETRY_STATUSES = {
    "failed",
    "kid_mismatch",
    "throttled",
    "api_error",
    "network_error",
    "api_changed",
}

RETRYABLE_ACCOUNT_STATUSES = {
    "failed",
    "kid_mismatch",
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