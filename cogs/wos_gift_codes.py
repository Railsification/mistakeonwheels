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