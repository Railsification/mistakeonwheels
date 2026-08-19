# cogs/wos/chief_gear.py
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.storage import load_guild_json, save_guild_json
from core.utils import DATA_DIR, ensure_deferred, load_json

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover
    cv2 = None
    np = None

try:
    import pytesseract
except Exception:  # pragma: no cover
    pytesseract = None


FEATURE_KEY = "chief_gear"
__version__ = "1.0.0"
TABLE_PATH = DATA_DIR / "chief_gear_table.json"
PROFILES_FILENAME = "chief_gear_profiles.json"

EMBEDDED_CHIEF_GEAR_TABLE = {'schema_version': 1, 'notes': 'Materials are milestone totals from the Chief Gear table screenshots.', 'resources': {'alloy': 'Hardened Alloy', 'polish': 'Polishing Solution', 'plans': 'Design Plans', 'amber': 'Amber'}, 'levels': [{'key': 'green_0', 'display': 'Green (Uncommon) 0', 'tier': 'Green (Uncommon)', 'stars': 0, 'order': 0, 'materials': {'alloy': 1500, 'polish': 15, 'plans': 0, 'amber': 0}, 'stat_total_percent': 9.35, 'power_total': 224400, 'troop_deployment_capacity': None}, {'key': 'green_1', 'display': 'Green (Uncommon) 1', 'tier': 'Green (Uncommon)', 'stars': 1, 'order': 1, 'materials': {'alloy': 3800, 'polish': 40, 'plans': 0, 'amber': 0}, 'stat_total_percent': 12.75, 'power_total': 306000, 'troop_deployment_capacity': None}, {'key': 'blue_0', 'display': 'Blue (Rare) 0', 'tier': 'Blue (Rare)', 'stars': 0, 'order': 2, 'materials': {'alloy': 7000, 'polish': 70, 'plans': 0, 'amber': 0}, 'stat_total_percent': 17.0, 'power_total': 408000, 'troop_deployment_capacity': None}, {'key': 'blue_1', 'display': 'Blue (Rare) 1', 'tier': 'Blue (Rare)', 'stars': 1, 'order': 3, 'materials': {'alloy': 9700, 'polish': 95, 'plans': 0, 'amber': 0}, 'stat_total_percent': 21.25, 'power_total': 510000, 'troop_deployment_capacity': None}, {'key': 'blue_2', 'display': 'Blue (Rare) 2', 'tier': 'Blue (Rare)', 'stars': 2, 'order': 4, 'materials': {'alloy': 0, 'polish': 0, 'plans': 45, 'amber': 0}, 'stat_total_percent': 25.5, 'power_total': 612000, 'troop_deployment_capacity': None}, {'key': 'blue_3', 'display': 'Blue (Rare) 3', 'tier': 'Blue (Rare)', 'stars': 3, 'order': 5, 'materials': {'alloy': 0, 'polish': 0, 'plans': 50, 'amber': 0}, 'stat_total_percent': 29.75, 'power_total': 714000, 'troop_deployment_capacity': None}, {'key': 'purple_0', 'display': 'Purple (Epic) 0', 'tier': 'Purple (Epic)', 'stars': 0, 'order': 6, 'materials': {'alloy': 0, 'polish': 0, 'plans': 60, 'amber': 0}, 'stat_total_percent': 34.0, 'power_total': 816000, 'troop_deployment_capacity': None}, {'key': 'purple_1', 'display': 'Purple (Epic) 1', 'tier': 'Purple (Epic)', 'stars': 1, 'order': 7, 'materials': {'alloy': 0, 'polish': 0, 'plans': 70, 'amber': 0}, 'stat_total_percent': 36.89, 'power_total': 885360, 'troop_deployment_capacity': None}, {'key': 'purple_2', 'display': 'Purple (Epic) 2', 'tier': 'Purple (Epic)', 'stars': 2, 'order': 8, 'materials': {'alloy': 6500, 'polish': 65, 'plans': 40, 'amber': 0}, 'stat_total_percent': 39.78, 'power_total': 954720, 'troop_deployment_capacity': None}, {'key': 'purple_3', 'display': 'Purple (Epic) 3', 'tier': 'Purple (Epic)', 'stars': 3, 'order': 9, 'materials': {'alloy': 8000, 'polish': 80, 'plans': 50, 'amber': 0}, 'stat_total_percent': 42.67, 'power_total': 1024080, 'troop_deployment_capacity': None}, {'key': 'purple_t1_0', 'display': 'Purple (Epic) T1 0', 'tier': 'Purple (Epic) T1', 'stars': 0, 'order': 10, 'materials': {'alloy': 10000, 'polish': 95, 'plans': 60, 'amber': 0}, 'stat_total_percent': 45.56, 'power_total': 1093440, 'troop_deployment_capacity': None}, {'key': 'purple_t1_1', 'display': 'Purple (Epic) T1 1', 'tier': 'Purple (Epic) T1', 'stars': 1, 'order': 11, 'materials': {'alloy': 11000, 'polish': 110, 'plans': 70, 'amber': 0}, 'stat_total_percent': 48.45, 'power_total': 1162800, 'troop_deployment_capacity': None}, {'key': 'purple_t1_2', 'display': 'Purple (Epic) T1 2', 'tier': 'Purple (Epic) T1', 'stars': 2, 'order': 12, 'materials': {'alloy': 13000, 'polish': 130, 'plans': 85, 'amber': 0}, 'stat_total_percent': 51.34, 'power_total': 1232160, 'troop_deployment_capacity': None}, {'key': 'purple_t1_3', 'display': 'Purple (Epic) T1 3', 'tier': 'Purple (Epic) T1', 'stars': 3, 'order': 13, 'materials': {'alloy': 15000, 'polish': 160, 'plans': 100, 'amber': 0}, 'stat_total_percent': 54.23, 'power_total': 1301520, 'troop_deployment_capacity': None}, {'key': 'gold_0', 'display': 'Gold (Mythic) 0', 'tier': 'Gold (Mythic)', 'stars': 0, 'order': 14, 'materials': {'alloy': 22000, 'polish': 220, 'plans': 40, 'amber': 0}, 'stat_total_percent': 56.78, 'power_total': 1362720, 'troop_deployment_capacity': None}, {'key': 'gold_1', 'display': 'Gold (Mythic) 1', 'tier': 'Gold (Mythic)', 'stars': 1, 'order': 15, 'materials': {'alloy': 23000, 'polish': 230, 'plans': 40, 'amber': 0}, 'stat_total_percent': 59.33, 'power_total': 1423920, 'troop_deployment_capacity': None}, {'key': 'gold_2', 'display': 'Gold (Mythic) 2', 'tier': 'Gold (Mythic)', 'stars': 2, 'order': 16, 'materials': {'alloy': 25000, 'polish': 250, 'plans': 45, 'amber': 0}, 'stat_total_percent': 61.88, 'power_total': 1485120, 'troop_deployment_capacity': None}, {'key': 'gold_3', 'display': 'Gold (Mythic) 3', 'tier': 'Gold (Mythic)', 'stars': 3, 'order': 17, 'materials': {'alloy': 26000, 'polish': 260, 'plans': 45, 'amber': 0}, 'stat_total_percent': 64.43, 'power_total': 1546320, 'troop_deployment_capacity': None}, {'key': 'gold_t1_0', 'display': 'Gold (Mythic) T1 0', 'tier': 'Gold (Mythic) T1', 'stars': 0, 'order': 18, 'materials': {'alloy': 28000, 'polish': 280, 'plans': 45, 'amber': 0}, 'stat_total_percent': 66.98, 'power_total': 1607520, 'troop_deployment_capacity': None}, {'key': 'gold_t1_1', 'display': 'Gold (Mythic) T1 1', 'tier': 'Gold (Mythic) T1', 'stars': 1, 'order': 19, 'materials': {'alloy': 30000, 'polish': 300, 'plans': 55, 'amber': 0}, 'stat_total_percent': 69.53, 'power_total': 1668720, 'troop_deployment_capacity': None}, {'key': 'gold_t1_2', 'display': 'Gold (Mythic) T1 2', 'tier': 'Gold (Mythic) T1', 'stars': 2, 'order': 20, 'materials': {'alloy': 32000, 'polish': 320, 'plans': 55, 'amber': 0}, 'stat_total_percent': 72.08, 'power_total': 1729920, 'troop_deployment_capacity': None}, {'key': 'gold_t1_3', 'display': 'Gold (Mythic) T1 3', 'tier': 'Gold (Mythic) T1', 'stars': 3, 'order': 21, 'materials': {'alloy': 35000, 'polish': 340, 'plans': 55, 'amber': 0}, 'stat_total_percent': 74.63, 'power_total': 1791120, 'troop_deployment_capacity': None}, {'key': 'gold_t2_0', 'display': 'Gold (Mythic) T2 0', 'tier': 'Gold (Mythic) T2', 'stars': 0, 'order': 22, 'materials': {'alloy': 38000, 'polish': 360, 'plans': 55, 'amber': 0}, 'stat_total_percent': 77.18, 'power_total': 1852320, 'troop_deployment_capacity': None}, {'key': 'gold_t2_1', 'display': 'Gold (Mythic) T2 1', 'tier': 'Gold (Mythic) T2', 'stars': 1, 'order': 23, 'materials': {'alloy': 43000, 'polish': 430, 'plans': 75, 'amber': 0}, 'stat_total_percent': 79.73, 'power_total': 1913520, 'troop_deployment_capacity': None}, {'key': 'gold_t2_2', 'display': 'Gold (Mythic) T2 2', 'tier': 'Gold (Mythic) T2', 'stars': 2, 'order': 24, 'materials': {'alloy': 45000, 'polish': 460, 'plans': 80, 'amber': 0}, 'stat_total_percent': 82.28, 'power_total': 1974720, 'troop_deployment_capacity': None}, {'key': 'gold_t2_3', 'display': 'Gold (Mythic) T2 3', 'tier': 'Gold (Mythic) T2', 'stars': 3, 'order': 25, 'materials': {'alloy': 48000, 'polish': 500, 'plans': 85, 'amber': 0}, 'stat_total_percent': 85.0, 'power_total': 2040000, 'troop_deployment_capacity': None}, {'key': 'red_0', 'display': 'Red (Legendary) 0', 'tier': 'Red (Legendary)', 'stars': 0, 'order': 26, 'materials': {'alloy': 50000, 'polish': 530, 'plans': 85, 'amber': 10}, 'stat_total_percent': 89.25, 'power_total': 2142000, 'troop_deployment_capacity': 40}, {'key': 'red_1', 'display': 'Red (Legendary) 1', 'tier': 'Red (Legendary)', 'stars': 1, 'order': 27, 'materials': {'alloy': 52000, 'polish': 560, 'plans': 90, 'amber': 10}, 'stat_total_percent': 93.5, 'power_total': 2244000, 'troop_deployment_capacity': 80}, {'key': 'red_2', 'display': 'Red (Legendary) 2', 'tier': 'Red (Legendary)', 'stars': 2, 'order': 28, 'materials': {'alloy': 54000, 'polish': 590, 'plans': 95, 'amber': 10}, 'stat_total_percent': 97.75, 'power_total': 2346000, 'troop_deployment_capacity': 120}, {'key': 'red_3', 'display': 'Red (Legendary) 3', 'tier': 'Red (Legendary)', 'stars': 3, 'order': 29, 'materials': {'alloy': 56000, 'polish': 620, 'plans': 100, 'amber': 10}, 'stat_total_percent': 102.0, 'power_total': 2448000, 'troop_deployment_capacity': 160}, {'key': 'red_t1_0', 'display': 'Red (Legendary) T1 0', 'tier': 'Red (Legendary) T1', 'stars': 0, 'order': 30, 'materials': {'alloy': 59000, 'polish': 670, 'plans': 110, 'amber': 15}, 'stat_total_percent': 106.25, 'power_total': 2550000, 'troop_deployment_capacity': 290}, {'key': 'red_t1_1', 'display': 'Red (Legendary) T1 1', 'tier': 'Red (Legendary) T1', 'stars': 1, 'order': 31, 'materials': {'alloy': 61000, 'polish': 700, 'plans': 115, 'amber': 15}, 'stat_total_percent': 110.5, 'power_total': 2652000, 'troop_deployment_capacity': 330}, {'key': 'red_t1_2', 'display': 'Red (Legendary) T1 2', 'tier': 'Red (Legendary) T1', 'stars': 2, 'order': 32, 'materials': {'alloy': 63000, 'polish': 730, 'plans': 120, 'amber': 15}, 'stat_total_percent': 114.75, 'power_total': 2754000, 'troop_deployment_capacity': 370}, {'key': 'red_t1_3', 'display': 'Red (Legendary) T1 3', 'tier': 'Red (Legendary) T1', 'stars': 3, 'order': 33, 'materials': {'alloy': 65000, 'polish': 760, 'plans': 125, 'amber': 15}, 'stat_total_percent': 119.0, 'power_total': 2856000, 'troop_deployment_capacity': 410}, {'key': 'red_t2_0', 'display': 'Red (Legendary) T2 0', 'tier': 'Red (Legendary) T2', 'stars': 0, 'order': 34, 'materials': {'alloy': 68000, 'polish': 810, 'plans': 135, 'amber': 20}, 'stat_total_percent': 123.25, 'power_total': 2958000, 'troop_deployment_capacity': 540}, {'key': 'red_t2_1', 'display': 'Red (Legendary) T2 1', 'tier': 'Red (Legendary) T2', 'stars': 1, 'order': 35, 'materials': {'alloy': 70000, 'polish': 840, 'plans': 140, 'amber': 20}, 'stat_total_percent': 127.5, 'power_total': 3060000, 'troop_deployment_capacity': 580}, {'key': 'red_t2_2', 'display': 'Red (Legendary) T2 2', 'tier': 'Red (Legendary) T2', 'stars': 2, 'order': 36, 'materials': {'alloy': 72000, 'polish': 870, 'plans': 145, 'amber': 20}, 'stat_total_percent': 131.75, 'power_total': 3162000, 'troop_deployment_capacity': 620}, {'key': 'red_t2_3', 'display': 'Red (Legendary) T2 3', 'tier': 'Red (Legendary) T2', 'stars': 3, 'order': 37, 'materials': {'alloy': 74000, 'polish': 900, 'plans': 150, 'amber': 20}, 'stat_total_percent': 136.0, 'power_total': 3264000, 'troop_deployment_capacity': 660}, {'key': 'red_t3_0', 'display': 'Red (Legendary) T3 0', 'tier': 'Red (Legendary) T3', 'stars': 0, 'order': 38, 'materials': {'alloy': 77000, 'polish': 950, 'plans': 160, 'amber': 25}, 'stat_total_percent': 140.25, 'power_total': 3366000, 'troop_deployment_capacity': 790}, {'key': 'red_t3_1', 'display': 'Red (Legendary) T3 1', 'tier': 'Red (Legendary) T3', 'stars': 1, 'order': 39, 'materials': {'alloy': 80000, 'polish': 990, 'plans': 165, 'amber': 25}, 'stat_total_percent': 144.5, 'power_total': 3468000, 'troop_deployment_capacity': 830}, {'key': 'red_t3_2', 'display': 'Red (Legendary) T3 2', 'tier': 'Red (Legendary) T3', 'stars': 2, 'order': 40, 'materials': {'alloy': 83000, 'polish': 1030, 'plans': 170, 'amber': 25}, 'stat_total_percent': 148.75, 'power_total': 3570000, 'troop_deployment_capacity': 870}, {'key': 'red_t3_3', 'display': 'Red (Legendary) T3 3', 'tier': 'Red (Legendary) T3', 'stars': 3, 'order': 41, 'materials': {'alloy': 86000, 'polish': 1070, 'plans': 180, 'amber': 25}, 'stat_total_percent': 153.0, 'power_total': 3672000, 'troop_deployment_capacity': 910}, {'key': 'red_t4_0', 'display': 'Red (Legendary) T4 0', 'tier': 'Red (Legendary) T4', 'stars': 0, 'order': 42, 'materials': {'alloy': 120000, 'polish': 1500, 'plans': 250, 'amber': 40}, 'stat_total_percent': 161.5, 'power_total': 3876000, 'troop_deployment_capacity': 1050}, {'key': 'red_t4_1', 'display': 'Red (Legendary) T4 1', 'tier': 'Red (Legendary) T4', 'stars': 1, 'order': 43, 'materials': {'alloy': 140000, 'polish': 1650, 'plans': 275, 'amber': 40}, 'stat_total_percent': 170.0, 'power_total': 4080000, 'troop_deployment_capacity': 1100}, {'key': 'red_t4_2', 'display': 'Red (Legendary) T4 2', 'tier': 'Red (Legendary) T4', 'stars': 2, 'order': 44, 'materials': {'alloy': 160000, 'polish': 1800, 'plans': 300, 'amber': 40}, 'stat_total_percent': 178.5, 'power_total': 4284000, 'troop_deployment_capacity': 1150}, {'key': 'red_t4_3', 'display': 'Red (Legendary) T4 3', 'tier': 'Red (Legendary) T4', 'stars': 3, 'order': 45, 'materials': {'alloy': 180000, 'polish': 1950, 'plans': 325, 'amber': 40}, 'stat_total_percent': 187.0, 'power_total': 4488000, 'troop_deployment_capacity': 1200}]}

RESOURCE_KEYS = ["alloy", "polish", "plans", "amber"]
RESOURCE_NAMES = {
    "alloy": "Alloy",
    "polish": "Polish",
    "plans": "Plans",
    "amber": "Amber",
}

SLOT_KEYS = ["goggles", "chest", "ring", "watch", "pants", "cane"]
SLOT_NAMES = {
    "goggles": "Goggles / Head",
    "chest": "Chest",
    "ring": "Ring",
    "watch": "Watch / Charm",
    "pants": "Pants",
    "cane": "Cane",
}
SLOT_CHOICES = [app_commands.Choice(name=name, value=key) for key, name in SLOT_NAMES.items()]

# Weekly Enhancement Material Exchange shop rates from WoS Chief Gear.
# `max_per_week` is the visible weekly remaining cap for that exchange row.
EXCHANGE_RULES = {
    "polish_to_plans": {"from": "polish", "from_qty": 10, "to": "plans", "to_qty": 1, "max_per_week": 50},
    "polish_to_alloy": {"from": "polish", "from_qty": 1, "to": "alloy", "to_qty": 50, "max_per_week": 1000},
    "alloy_to_plans": {"from": "alloy", "from_qty": 1000, "to": "plans", "to_qty": 1, "max_per_week": 50},
    "alloy_to_polish": {"from": "alloy", "from_qty": 200, "to": "polish", "to_qty": 1, "max_per_week": 500},
    "plans_to_amber": {"from": "plans", "from_qty": 10, "to": "amber", "to_qty": 1, "max_per_week": 500},
    "plans_to_polish": {"from": "plans", "from_qty": 1, "to": "polish", "to_qty": 3, "max_per_week": 500},
    "plans_to_alloy": {"from": "plans", "from_qty": 1, "to": "alloy", "to_qty": 300, "max_per_week": 500},
}

DEFAULT_PROFILE = {
    "inventory": {"alloy": 0, "polish": 0, "plans": 0, "amber": 0},
    "slots": {slot: None for slot in SLOT_KEYS},
    "last_scan": {},
}


@dataclass
class ParsedScan:
    level_key: Optional[str]
    power_total: Optional[int]
    inventory: dict[str, int]
    next_cost: dict[str, int]
    raw_text: str
    confidence_notes: list[str]


@dataclass
class ParsedSlotScan:
    slot: str
    tier: Optional[int]
    stars: Optional[int]
    level_key: Optional[str]
    raw_tier_text: str
    notes: list[str]


class ChiefGearCog(commands.Cog):
    HELP_META = {
        "title": "Chief Gear Calculator",
        "summary": "WoS Chief Gear material planning with per-server saved profiles and screenshot inventory scanning.",
        "details": (
            "Feature key: `chief_gear`. Use `/chief_gear scan image:<screenshot>` to privately scan all six visible gear slots "
            "plus inventory from the Chief Gear screen, then `/chief_gear plan target:<level>` or `/chief_gear recommend`. "
            "Uses local Python OCR/CV, not OpenAI. Data is stored per server."
        ),
    }

    chief_gear = app_commands.Group(name="chief_gear", description="WoS Chief Gear calculator")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self.table = self._load_table()
        self.levels = self.table["levels"]
        self.level_by_key = {row["key"]: row for row in self.levels}
        self.level_by_power = {int(row["power_total"]): row for row in self.levels}

    SLOT_CARD_REGIONS = {
        # Normalised card boxes for the standard Chief Gear screen.
        # These include the gear tile and visible star strip, but avoid the charm icons underneath.
        "goggles": (0.122, 0.149, 0.286, 0.222),
        "chest": (0.074, 0.259, 0.233, 0.332),
        "ring": (0.122, 0.362, 0.286, 0.437),
        "watch": (0.714, 0.149, 0.878, 0.222),
        "pants": (0.767, 0.259, 0.931, 0.332),
        "cane": (0.719, 0.362, 0.884, 0.437),
    }

    # ---------- setup / data ----------

    def _load_table(self) -> dict[str, Any]:
        # The Chief Gear table is application data, so keep it inside the cog.
        # Railway's persistent data volume can mask repository files under data/.
        data = json.loads(json.dumps(EMBEDDED_CHIEF_GEAR_TABLE))
        for index, row in enumerate(data["levels"]):
            row.setdefault("order", index)
            row.setdefault("materials", {})
            for key in RESOURCE_KEYS:
                row["materials"].setdefault(key, 0)
        return data

    def _blank_profile(self) -> dict[str, Any]:
        return json.loads(json.dumps(DEFAULT_PROFILE))

    def _load_profiles(self, guild_id: int) -> dict[str, Any]:
        raw = load_guild_json(guild_id, PROFILES_FILENAME, {})
        return raw if isinstance(raw, dict) else {}

    def _save_profiles(self, guild_id: int, data: dict[str, Any]) -> None:
        save_guild_json(guild_id, PROFILES_FILENAME, data)

    def _profile_key(self, user_id: int) -> str:
        return str(int(user_id))

    def _get_profile(self, guild_id: int, user_id: int) -> dict[str, Any]:
        data = self._load_profiles(guild_id)
        key = self._profile_key(user_id)
        if key not in data or not isinstance(data[key], dict):
            data[key] = self._blank_profile()
            self._save_profiles(guild_id, data)
        profile = data[key]
        profile.setdefault("inventory", {"alloy": 0, "polish": 0, "plans": 0, "amber": 0})
        profile.setdefault("slots", {slot: None for slot in SLOT_KEYS})
        profile.setdefault("last_scan", {})
        for res in RESOURCE_KEYS:
            profile["inventory"].setdefault(res, 0)
        for slot in SLOT_KEYS:
            profile["slots"].setdefault(slot, None)
        return profile

    def _set_profile(self, guild_id: int, user_id: int, profile: dict[str, Any]) -> None:
        data = self._load_profiles(guild_id)
        data[self._profile_key(user_id)] = profile
        self._save_profiles(guild_id, data)

    # ---------- access ----------

    async def _ensure_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None or interaction.channel_id is None:
            if interaction.response.is_done():
                await interaction.followup.send("Use this inside a server channel.", ephemeral=True)
            else:
                await interaction.response.send_message("Use this inside a server channel.", ephemeral=True)
            return False

        admin_guild_id = int((getattr(self.bot, "hot_config", {}) or {}).get("admin_guild_id", 0) or 0)
        if admin_guild_id and interaction.guild_id == admin_guild_id:
            return True

        if self.settings.is_feature_allowed(interaction.guild_id, interaction.channel_id, FEATURE_KEY):
            return True

        msg = (
            f"This command is not enabled in this channel. "
            f"Use `/council feature_channel_add` from the admin server with feature `{FEATURE_KEY}`."
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    # ---------- level helpers ----------

    def _level_label(self, key: Optional[str]) -> str:
        if not key:
            return "not set"
        row = self.level_by_key.get(key)
        return row["display"] if row else key

    def _level_order(self, key: Optional[str]) -> int:
        row = self.level_by_key.get(key or "")
        return int(row["order"]) if row else -1

    def _find_nearest_power_level(self, power: int, tolerance: int = 3500) -> tuple[Optional[str], Optional[int]]:
        best_key = None
        best_diff = None
        for row in self.levels:
            diff = abs(int(row["power_total"]) - int(power))
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_key = row["key"]
        if best_diff is not None and best_diff <= tolerance:
            return best_key, best_diff
        return None, best_diff

    def _cost_between(self, current_key: Optional[str], target_key: str) -> dict[str, int]:
        current_order = self._level_order(current_key)
        target_order = self._level_order(target_key)
        totals = {res: 0 for res in RESOURCE_KEYS}
        if target_order <= current_order:
            return totals
        for row in self.levels:
            order = int(row["order"])
            if current_order < order <= target_order:
                for res in RESOURCE_KEYS:
                    totals[res] += int(row["materials"].get(res, 0) or 0)
        return totals

    def _next_level_key(self, current_key: Optional[str]) -> Optional[str]:
        order = self._level_order(current_key)
        next_order = order + 1
        if next_order < 0 or next_order >= len(self.levels):
            return None
        return self.levels[next_order]["key"]

    def _visible_click_cost_for_level(self, current_key: Optional[str]) -> dict[str, int]:
        """Return the visible one-click cost from the selected gear panel.

        WoS shows four enhancement clicks inside each table row/star. The table material
        row is the full row cost, so the visible click cost is usually one quarter of
        the next row's material cost. Used only to split OCR strings like `855` into
        `85/5`; it is not shown as a planning result.
        """
        next_key = self._next_level_key(current_key)
        if not next_key:
            return {res: 0 for res in RESOURCE_KEYS}
        row = self.level_by_key.get(next_key, {})
        mats = row.get("materials", {}) if isinstance(row, dict) else {}
        costs: dict[str, int] = {}
        for res in RESOURCE_KEYS:
            value = int(mats.get(res, 0) or 0)
            costs[res] = int(round(value / 4)) if value else 0
        return costs

    def _normalise_inventory_value(self, res: str, raw: str, expected_cost: Optional[int] = None) -> tuple[int, int]:
        """Parse one HAVE/COST OCR region.

        Tesseract often drops the slash in `85/5` and returns `855`. If we know the
        selected gear's visible click cost, split the OCR number by that suffix.
        """
        raw = raw or ""
        match = re.search(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)", raw)
        if match:
            return self._parse_int(match.group(1)), self._parse_int(match.group(2))

        nums = re.findall(r"\d[\d,]*", raw)
        if len(nums) >= 2:
            return self._parse_int(nums[0]), self._parse_int(nums[1])
        if not nums:
            return 0, 0

        digits = re.sub(r"\D", "", nums[0])
        if expected_cost and expected_cost > 0:
            suffix = str(int(expected_cost))
            if digits.endswith(suffix) and len(digits) > len(suffix):
                have = int(digits[:-len(suffix)] or "0")
                return have, int(expected_cost)

        # Amber is commonly tiny and `85/5` is often OCR'd as `855`.
        if res == "amber" and len(digits) >= 3 and digits.endswith(("5", "10", "15", "20", "25", "40")):
            for suffix in ("40", "25", "20", "15", "10", "5"):
                if digits.endswith(suffix) and len(digits) > len(suffix):
                    return int(digits[:-len(suffix)]), int(suffix)

        return int(digits or 0), 0

    def _format_costs(self, costs: dict[str, int]) -> str:
        return " | ".join(f"{RESOURCE_NAMES[k]}: **{int(costs.get(k, 0)):,}**" for k in RESOURCE_KEYS)

    def _missing_costs(self, inventory: dict[str, int], costs: dict[str, int]) -> dict[str, int]:
        return {res: max(0, int(costs.get(res, 0)) - int(inventory.get(res, 0))) for res in RESOURCE_KEYS}

    # ---------- OCR helpers ----------

    async def _attachment_to_image(self, attachment: discord.Attachment):
        if Image is None:
            raise RuntimeError("Pillow is not installed.")
        if not (attachment.content_type or "").startswith("image/"):
            name = (attachment.filename or "").lower()
            if not name.endswith((".png", ".jpg", ".jpeg", ".webp")):
                raise RuntimeError("Attach an image screenshot.")
        raw = await attachment.read()
        return Image.open(io.BytesIO(raw)).convert("RGB")

    def _ocr_text(self, pil_img, *, numeric_only: bool = False, psm: int = 6) -> str:
        if pytesseract is None:
            raise RuntimeError("pytesseract is not installed.")
        if cv2 is None or np is None:
            raise RuntimeError("opencv/numpy is not installed.")
        arr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        scale = 3 if min(gray.shape[:2]) < 500 else 2
        big = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        config = f"--psm {psm}"
        if numeric_only:
            config += " -c tessedit_char_whitelist=0123456789,/.%+"
        return pytesseract.image_to_string(big, config=config) or ""

    @staticmethod
    def _parse_int(value: str) -> int:
        return int(re.sub(r"[^0-9]", "", value or "") or 0)

    def _parse_inventory_line(self, text: str) -> tuple[dict[str, int], dict[str, int]]:
        pairs = re.findall(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)", text)
        inventory = {res: 0 for res in RESOURCE_KEYS}
        next_cost = {res: 0 for res in RESOURCE_KEYS}
        for res, pair in zip(RESOURCE_KEYS, pairs[:4]):
            inventory[res] = self._parse_int(pair[0])
            next_cost[res] = self._parse_int(pair[1])
        return inventory, next_cost

    def _parse_power(self, text: str) -> Optional[int]:
        candidates = []
        for match in re.finditer(r"\d{1,3}(?:,\d{3})+", text):
            value = self._parse_int(match.group(0))
            if 200000 <= value <= 6000000:
                candidates.append(value)
        if candidates:
            return candidates[0]
        for match in re.finditer(r"\b\d{6,7}\b", text):
            value = int(match.group(0))
            if 200000 <= value <= 6000000:
                return value
        return None


    def _parse_power_from_region(self, pil_img) -> tuple[Optional[int], str]:
        # Current selected gear power sits below the T/progress bar and above Stat Bonuses.
        # Crop it directly so inventory values do not get mistaken as power.
        w, h = pil_img.size
        crops = [
            (0.24, 0.535, 0.84, 0.595),
            (0.20, 0.520, 0.86, 0.610),
        ]
        texts: list[str] = []
        for x1, y1, x2, y2 in crops:
            try:
                crop = pil_img.crop((int(w * x1), int(h * y1), int(w * x2), int(h * y2)))
                text = self._ocr_text(crop, numeric_only=True, psm=7)
                if text.strip():
                    texts.append(text.strip())
                    power = self._parse_power(text)
                    if power:
                        return power, "\n".join(texts)
            except Exception:
                pass
        return None, "\n".join(texts)

    def _parse_resource_regions(self, pil_img, expected_cost: Optional[dict[str, int]] = None) -> tuple[dict[str, int], dict[str, int], str]:
        # Values are always shown as HAVE/COST under the four resource icons.
        # Fixed columns are much more reliable than OCRing the whole line at once.
        w, h = pil_img.size
        regions = {
            "alloy": (0.03, 0.815, 0.27, 0.855),
            "polish": (0.27, 0.815, 0.50, 0.855),
            "plans": (0.52, 0.815, 0.75, 0.855),
            "amber": (0.75, 0.815, 0.98, 0.855),
        }
        inventory = {res: 0 for res in RESOURCE_KEYS}
        selected_cost = {res: 0 for res in RESOURCE_KEYS}
        raw_parts: list[str] = []
        for res in RESOURCE_KEYS:
            x1, y1, x2, y2 = regions[res]
            crop = pil_img.crop((int(w * x1), int(h * y1), int(w * x2), int(h * y2)))
            text = self._ocr_text(crop, numeric_only=True, psm=7)
            raw_parts.append(f"{res}:{text.strip()}")
            have, cost = self._normalise_inventory_value(res, text, (expected_cost or {}).get(res))
            inventory[res] = have
            selected_cost[res] = cost
        return inventory, selected_cost, " | ".join(raw_parts)

    def _crop_slot_card(self, pil_img, slot: str):
        w, h = pil_img.size
        x1, y1, x2, y2 = self.SLOT_CARD_REGIONS[slot]
        return pil_img.crop((int(w * x1), int(h * y1), int(w * x2), int(h * y2)))

    def _detect_slot_cards(self, pil_img) -> dict[str, Any]:
        """Detect the six visible gear tiles by their red/pink card background."""
        if cv2 is None or np is None:
            return {}

        arr = np.array(pil_img.convert("RGB"))
        h, w = arr.shape[:2]
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        mask1 = cv2.inRange(hsv, np.array([0, 45, 80]), np.array([14, 255, 255]))
        mask2 = cv2.inRange(hsv, np.array([158, 45, 80]), np.array([179, 255, 255]))
        mask = mask1 | mask2
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        boxes: list[tuple[int, int, int, int]] = []
        for contour in contours:
            x, y, bw, bh = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            cx = x + bw / 2
            if not (0.08 * w <= bw <= 0.20 * w):
                continue
            if not (0.055 * h <= bh <= 0.105 * h):
                continue
            if not (0.10 * h <= y <= 0.46 * h):
                continue
            if 0.34 * w <= cx <= 0.66 * w:
                # central selected gear icon, not one of the six equipment tiles
                continue
            if area < 0.0035 * w * h:
                continue
            boxes.append((x, y, bw, bh))

        left = sorted([b for b in boxes if b[0] + b[2] / 2 < w / 2], key=lambda b: b[1])[:3]
        right = sorted([b for b in boxes if b[0] + b[2] / 2 >= w / 2], key=lambda b: b[1])[:3]
        if len(left) < 3 or len(right) < 3:
            return {}

        mapping = {
            "goggles": left[0],
            "chest": left[1],
            "ring": left[2],
            "watch": right[0],
            "pants": right[1],
            "cane": right[2],
        }
        cards = {}
        pad_x = int(w * 0.006)
        pad_y = int(h * 0.004)
        for slot, (x, y, bw, bh) in mapping.items():
            # Bottom tiles can include the charm gems below the gear card. Keep the red card square only.
            card_h = min(bh, int(bw * 1.07))
            cards[slot] = pil_img.crop((
                max(0, x - pad_x),
                max(0, y - pad_y),
                min(w, x + bw + pad_x),
                min(h, y + card_h + pad_y),
            ))
        return cards

    def _yellow_mask(self, rgb_arr):
        if cv2 is None or np is None:
            raise RuntimeError("opencv/numpy is not installed.")
        hsv = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2HSV)
        return cv2.inRange(hsv, np.array([12, 60, 95]), np.array([50, 255, 255]))

    def _tier_candidate_from_text(self, text: str) -> Optional[int]:
        cleaned = (text or "").upper()
        cleaned = cleaned.replace("I", "1").replace("L", "1").replace("|", "1")
        cleaned = re.sub(r"[^T1234]", "", cleaned)
        if not cleaned:
            return None
        idx = cleaned.find("T")
        if idx >= 0:
            for ch in cleaned[idx + 1:]:
                if ch in "1234":
                    return int(ch)
            return None
        digits = [int(ch) for ch in cleaned if ch in "1234"]
        if digits:
            return digits[0]
        return None

    def _ocr_tier_from_card(self, card_img) -> tuple[Optional[int], str]:
        if pytesseract is None or cv2 is None or np is None:
            return None, ""

        arr = np.array(card_img.convert("RGB"))
        ch, cw = arr.shape[:2]
        crop_defs = [
            (0.00, 0.00, 0.55, 0.33),
            (0.00, 0.00, 0.48, 0.28),
            (0.00, 0.00, 0.62, 0.36),
        ]
        raw_texts: list[str] = []
        votes: list[int] = []
        for x1, y1, x2, y2 in crop_defs:
            crop = arr[int(ch * y1):int(ch * y2), int(cw * x1):int(cw * x2)]
            if crop.size == 0:
                continue
            for scale in (6, 8):
                big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                inputs = [("rgb", big)]
                try:
                    mask = self._yellow_mask(big)
                    mask = cv2.dilate(mask, np.ones((2, 2), np.uint8), iterations=1)
                    inputs.append(("mask", 255 - mask))
                except Exception:
                    pass
                for _, ocr_img in inputs:
                    for psm in (7, 8, 13):
                        try:
                            text = pytesseract.image_to_string(
                                ocr_img,
                                config=f"--psm {psm} -c tessedit_char_whitelist=Tt1234Il|",
                            ) or ""
                        except Exception:
                            continue
                        text = text.strip()
                        if not text:
                            continue
                        raw_texts.append(text)
                        candidate = self._tier_candidate_from_text(text)
                        if candidate is not None:
                            votes.append(candidate)
        raw = " ".join(raw_texts)
        if votes:
            # Most common wins. If there is a tie, lower tier wins because OCR often reads stars as extra 3s.
            counts = {tier: votes.count(tier) for tier in set(votes)}
            tier = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
            return int(tier), raw
        return None, raw

    def _count_gear_stars(self, card_img) -> int:
        if cv2 is None or np is None:
            return 0
        arr = np.array(card_img.convert("RGB"))
        ch, cw = arr.shape[:2]
        mask = self._yellow_mask(arr)

        # Yellow gear stars sit down the far-left edge inside the card.
        # Ignore the T label, charm gems under the card, and yellow gear artwork.
        mask[:int(ch * 0.29), :] = 0
        mask[:, int(cw * 0.37):] = 0
        mask[int(ch * 0.88):, :] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        centers: list[int] = []
        for contour in contours:
            x, y, bw, bh = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            ratio = bw / max(1, bh)
            if not (12 <= bw <= 45 and 12 <= bh <= 45):
                continue
            if not (0.55 <= ratio <= 1.65):
                continue
            if area < 150:
                continue
            if x > int(cw * 0.14):
                continue
            centers.append(y + bh // 2)

        merged: list[int] = []
        for cy in sorted(centers):
            if not merged or abs(cy - merged[-1]) > int(ch * 0.15):
                merged.append(cy)
        return max(0, min(3, len(merged)))

    def _level_key_from_visible_card(self, tier: Optional[int], stars: int) -> Optional[str]:
        if tier is None:
            return None
        key = f"red_t{tier}_{stars}"
        return key if key in self.level_by_key else None

    def _parse_card_for_slot(self, slot: str, card) -> ParsedSlotScan:
        tier, raw_tier = self._ocr_tier_from_card(card)
        stars = self._count_gear_stars(card)
        level_key = self._level_key_from_visible_card(tier, stars)
        notes: list[str] = []
        if level_key is None:
            notes.append("Could not confidently read tier/stars from the top gear tile.")
        return ParsedSlotScan(slot=slot, tier=tier, stars=stars, level_key=level_key, raw_tier_text=raw_tier, notes=notes)

    def _parse_all_visible_slots(self, pil_img) -> dict[str, ParsedSlotScan]:
        parsed: dict[str, ParsedSlotScan] = {}
        detected_cards = self._detect_slot_cards(pil_img)
        for slot in SLOT_KEYS:
            candidates: list[ParsedSlotScan] = []
            try:
                if detected_cards and detected_cards.get(slot) is not None:
                    candidates.append(self._parse_card_for_slot(slot, detected_cards[slot]))
            except Exception:
                pass
            try:
                candidates.append(self._parse_card_for_slot(slot, self._crop_slot_card(pil_img, slot)))
            except Exception:
                pass

            good = [c for c in candidates if c.level_key]
            if good:
                # Prefer the read with the highest visible star count; fixed crops often keep star strips cleaner.
                parsed[slot] = sorted(good, key=lambda c: (int(c.stars or 0), len(c.raw_tier_text or "")), reverse=True)[0]
                parsed[slot].notes = []
            elif candidates:
                best = sorted(candidates, key=lambda c: (c.tier is not None, int(c.stars or 0)), reverse=True)[0]
                parsed[slot] = best
            else:
                parsed[slot] = ParsedSlotScan(
                    slot=slot, tier=None, stars=None, level_key=None, raw_tier_text="",
                    notes=["Slot scan failed."],
                )
        return parsed

    def _parse_screenshot(self, pil_img) -> ParsedScan:
        w, h = pil_img.size
        notes: list[str] = []

        power, power_text = self._parse_power_from_region(pil_img)
        level_key = None
        if power is not None:
            level_key, diff = self._find_nearest_power_level(power)
            if level_key is None:
                notes.append(f"Power `{power:,}` did not match a known table row. Closest diff: {diff:,}.")
        else:
            notes.append("Could not OCR the selected gear power total.")

        expected_click_cost = self._visible_click_cost_for_level(level_key) if level_key else None
        inventory, selected_cost, resource_text = self._parse_resource_regions(pil_img, expected_click_cost)
        if not any(inventory.values()):
            # Last resort fallback.
            full_text = self._ocr_text(pil_img, psm=6)
            inv2, cost2 = self._parse_inventory_line(full_text)
            if any(inv2.values()):
                inventory, selected_cost = inv2, cost2
            else:
                notes.append("Could not OCR inventory counts from the screenshot.")
                full_text = ""
        else:
            full_text = ""

        return ParsedScan(
            level_key=level_key,
            power_total=power,
            inventory=inventory,
            next_cost=selected_cost,
            raw_text=(power_text + "\n" + resource_text + "\n" + full_text).strip(),
            confidence_notes=notes,
        )

    # ---------- embeds ----------

    def _base_embed(self, title: str, description: Optional[str] = None) -> discord.Embed:
        return discord.Embed(title=title, description=description or "", colour=discord.Colour.gold())

    def _profile_embed(self, profile: dict[str, Any], owner: discord.abc.User) -> discord.Embed:
        embed = self._base_embed("Chief Gear Profile", f"Saved profile for **{owner.display_name}**")
        inv = profile.get("inventory", {})
        embed.add_field(name="Inventory", value=self._format_costs(inv), inline=False)
        slot_lines = []
        for slot in SLOT_KEYS:
            key = profile.get("slots", {}).get(slot)
            slot_lines.append(f"**{SLOT_NAMES[slot]}** — {self._level_label(key)}")
        embed.add_field(name="Slots", value="\n".join(slot_lines), inline=False)
        return embed

    # ---------- commands ----------

    @chief_gear.command(name="help", description="Show Chief Gear calculator help.")
    async def help_cmd(self, interaction: discord.Interaction):
        log_cmd("chief_gear help", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        embed = self._base_embed("Chief Gear Calculator Help")
        embed.add_field(
            name="Main flow",
            value=(
                "`/chief_gear scan image:<screenshot>` — scans all six visible gear slots + inventory\n"
                "`/chief_gear view` — shows saved profile\n"
                "`/chief_gear plan target:<level>` — materials needed to target\n"
                "`/chief_gear recommend` — spends saved inventory until no more upgrades fit\n"
                "`/chief_gear exchange_plan target:<level> weeks:2 convert_to_amber:false` — weekly exchange/shop conversion plan"
            ),
            inline=False,
        )
        embed.add_field(
            name="Screenshot note",
            value="The six top gear tiles are read from their visible tier/star labels. The lower panel is only used for inventory and power sanity-checking.",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @chief_gear.command(name="scan", description="Scan a Chief Gear screenshot and save all visible gear levels/inventory.")
    @app_commands.describe(image="Chief Gear screenshot", selected_slot="Optional selected gear slot for power sanity-check override")
    @app_commands.choices(selected_slot=SLOT_CHOICES)
    async def scan_cmd(self, interaction: discord.Interaction, image: discord.Attachment, selected_slot: Optional[str] = None):
        log_cmd("chief_gear scan", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        if pytesseract is None:
            await interaction.followup.send(
                "OCR is not installed. Add `pytesseract` to requirements and `tesseract-ocr` to the Dockerfile.",
                ephemeral=True,
            )
            return

        try:
            pil_img = await self._attachment_to_image(image)
            parsed = self._parse_screenshot(pil_img)
            slot_scans = self._parse_all_visible_slots(pil_img)
        except Exception as exc:
            await interaction.followup.send(f"Scan failed: `{exc}`", ephemeral=True)
            return

        # If the user provides the selected slot, the lower-panel power total can override that slot.
        if selected_slot and parsed.level_key:
            slot_scans[selected_slot].level_key = parsed.level_key
            slot_scans[selected_slot].notes.append("Selected-slot power total used as exact override.")

        profile = self._get_profile(int(interaction.guild_id), int(interaction.user.id))
        if any(parsed.inventory.values()):
            profile["inventory"] = parsed.inventory

        saved_count = 0
        for slot_key, slot_scan in slot_scans.items():
            if slot_scan.level_key:
                profile["slots"][slot_key] = slot_scan.level_key
                saved_count += 1

        profile["last_scan"] = {
            "mode": "all_visible_slots",
            "selected_slot": selected_slot,
            "power_level_key": parsed.level_key,
            "power_total": parsed.power_total,
            "inventory": parsed.inventory,
            "next_cost": parsed.next_cost,
            "slot_scans": {
                key: {
                    "tier": scan.tier,
                    "stars": scan.stars,
                    "level_key": scan.level_key,
                    "raw_tier_text": scan.raw_tier_text,
                    "notes": scan.notes,
                }
                for key, scan in slot_scans.items()
            },
            "notes": parsed.confidence_notes,
        }
        self._set_profile(int(interaction.guild_id), int(interaction.user.id), profile)

        embed = self._base_embed("Chief Gear Scan Saved")
        slot_lines = []
        note_lines = []
        for slot_key in SLOT_KEYS:
            scan = slot_scans[slot_key]
            if scan.level_key:
                slot_lines.append(f"**{SLOT_NAMES[slot_key]}** — {self._level_label(scan.level_key)}")
            else:
                slot_lines.append(f"**{SLOT_NAMES[slot_key]}** — not read")
            for note in scan.notes:
                note_lines.append(f"{SLOT_NAMES[slot_key]}: {note}")

        embed.add_field(name="Slots saved", value=f"**{saved_count}/6**", inline=True)
        embed.add_field(name="Detected power", value=f"{parsed.power_total:,}" if parsed.power_total else "not read", inline=True)
        embed.add_field(name="Visible gear levels", value="\n".join(slot_lines)[:1000], inline=False)
        embed.add_field(name="Inventory", value=self._format_costs(parsed.inventory), inline=False)
        all_notes = list(parsed.confidence_notes) + note_lines
        if all_notes:
            embed.add_field(name="Scan notes", value="\n".join(all_notes)[:1000], inline=False)
        embed.set_footer(text="Private scan. Nothing posted publicly.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @chief_gear.command(name="set_inventory", description="Manually save current Chief Gear materials.")
    @app_commands.describe(alloy="Hardened Alloy", polish="Polishing Solution", plans="Design Plans", amber="Amber/orange stone")
    async def set_inventory_cmd(self, interaction: discord.Interaction, alloy: int, polish: int, plans: int, amber: int):
        log_cmd("chief_gear set_inventory", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        profile = self._get_profile(int(interaction.guild_id), int(interaction.user.id))
        profile["inventory"] = {"alloy": max(0, alloy), "polish": max(0, polish), "plans": max(0, plans), "amber": max(0, amber)}
        self._set_profile(int(interaction.guild_id), int(interaction.user.id), profile)
        await interaction.followup.send("Saved inventory: " + self._format_costs(profile["inventory"]), ephemeral=True)

    @chief_gear.command(name="set_slot", description="Manually save one gear slot level.")
    @app_commands.describe(slot="Gear slot", level="Level, e.g. red_t2_0 or Red T2 0")
    @app_commands.choices(slot=SLOT_CHOICES)
    async def set_slot_cmd(self, interaction: discord.Interaction, slot: str, level: str):
        log_cmd("chief_gear set_slot", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        key = self._resolve_level_key(level)
        if not key:
            await interaction.followup.send(f"Unknown level `{level}`. Try `/chief_gear levels`.", ephemeral=True)
            return
        profile = self._get_profile(int(interaction.guild_id), int(interaction.user.id))
        profile["slots"][slot] = key
        self._set_profile(int(interaction.guild_id), int(interaction.user.id), profile)
        await interaction.followup.send(f"Saved **{SLOT_NAMES[slot]}** as **{self._level_label(key)}**.", ephemeral=True)

    @chief_gear.command(name="view", description="Show your saved Chief Gear profile.")
    async def view_cmd(self, interaction: discord.Interaction):
        log_cmd("chief_gear view", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        profile = self._get_profile(int(interaction.guild_id), int(interaction.user.id))
        await interaction.followup.send(embed=self._profile_embed(profile, interaction.user), ephemeral=True)

    @chief_gear.command(name="levels", description="Show known Chief Gear levels.")
    async def levels_cmd(self, interaction: discord.Interaction):
        log_cmd("chief_gear levels", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        lines = [f"`{row['key']}` — {row['display']}" for row in self.levels]
        embed = self._base_embed("Chief Gear Levels")
        for idx in range(0, len(lines), 15):
            embed.add_field(name="Levels" if idx == 0 else "Levels continued", value="\n".join(lines[idx:idx+15]), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    def _target_totals(self, profile: dict[str, Any], target_key: str, slot: Optional[str] = None):
        slots = [slot] if slot else list(SLOT_KEYS)
        totals = {res: 0 for res in RESOURCE_KEYS}
        detail_lines: list[str] = []
        skipped: list[str] = []
        for slot_key in slots:
            current_key = profile.get("slots", {}).get(slot_key)
            if not current_key:
                skipped.append(SLOT_NAMES[slot_key])
                continue
            costs = self._cost_between(current_key, target_key)
            for res in RESOURCE_KEYS:
                totals[res] += costs[res]
            detail_lines.append(f"**{SLOT_NAMES[slot_key]}**: {self._level_label(current_key)} → {self._level_label(target_key)}")
        return totals, detail_lines, skipped

    def _apply_exchange(self, inv: dict[str, int], rule_key: str, count: int) -> str:
        rule = EXCHANGE_RULES[rule_key]
        src = rule["from"]
        dst = rule["to"]
        from_qty = int(rule["from_qty"])
        to_qty = int(rule["to_qty"])
        count = max(0, int(count))
        inv[src] -= from_qty * count
        inv[dst] += to_qty * count
        return f"{count}x {from_qty:,} {RESOURCE_NAMES[src]} → {to_qty:,} {RESOURCE_NAMES[dst]}"


    def _weekly_exchange_plan(self, inventory: dict[str, int], needed: dict[str, int], weeks: int, convert_to_amber: bool):
        working = {res: int(inventory.get(res, 0) or 0) for res in RESOURCE_KEYS}
        weeks = max(1, int(weeks))
        week_lines: list[list[str]] = [[] for _ in range(weeks)]

        def missing_now() -> dict[str, int]:
            return {res: max(0, int(needed.get(res, 0)) - int(working.get(res, 0))) for res in RESOURCE_KEYS}

        def surplus_now(res: str) -> int:
            return max(0, int(working.get(res, 0)) - int(needed.get(res, 0)))

        # Optional amber conversion. If disabled, amber is left for buying/events.
        for week in range(weeks):
            if not convert_to_amber:
                break
            miss = missing_now()
            if miss["amber"] <= 0:
                break
            rule = EXCHANGE_RULES["plans_to_amber"]
            max_by_cap = int(rule["max_per_week"])
            max_by_need = miss["amber"]
            max_by_surplus_plans = surplus_now("plans") // int(rule["from_qty"])
            count = min(max_by_cap, max_by_need, max_by_surplus_plans)
            if count > 0:
                week_lines[week].append(self._apply_exchange(working, "plans_to_amber", count))

        # Use true surplus resources only; do not convert material still needed for the target.
        for week in range(weeks):
            miss = missing_now()
            # If polish is short and alloy is surplus, turn alloy into polish.
            if miss["polish"] > 0 and surplus_now("alloy") >= EXCHANGE_RULES["alloy_to_polish"]["from_qty"]:
                rule = EXCHANGE_RULES["alloy_to_polish"]
                count = min(int(rule["max_per_week"]), miss["polish"], surplus_now("alloy") // int(rule["from_qty"]))
                if count > 0:
                    week_lines[week].append(self._apply_exchange(working, "alloy_to_polish", count))

            miss = missing_now()
            # If alloy is short and polish is surplus, turn polish into alloy.
            if miss["alloy"] > 0 and surplus_now("polish") >= EXCHANGE_RULES["polish_to_alloy"]["from_qty"]:
                rule = EXCHANGE_RULES["polish_to_alloy"]
                count = min(int(rule["max_per_week"]), (miss["alloy"] + 49) // 50, surplus_now("polish") // int(rule["from_qty"]))
                if count > 0:
                    week_lines[week].append(self._apply_exchange(working, "polish_to_alloy", count))

            miss = missing_now()
            # Spend surplus design plans on the bigger remaining shortage.
            for rule_key in ("plans_to_polish", "plans_to_alloy"):
                if surplus_now("plans") <= 0:
                    break
                miss = missing_now()
                if rule_key == "plans_to_polish" and miss["polish"] <= 0:
                    continue
                if rule_key == "plans_to_alloy" and miss["alloy"] <= 0:
                    continue
                rule = EXCHANGE_RULES[rule_key]
                dst = rule["to"]
                per = int(rule["to_qty"])
                count = min(int(rule["max_per_week"]), surplus_now("plans"), (miss[dst] + per - 1) // per)
                if count > 0:
                    week_lines[week].append(self._apply_exchange(working, rule_key, count))

        return working, week_lines, missing_now()

    @chief_gear.command(name="plan", description="Calculate materials needed from saved level(s) to a target.")
    @app_commands.describe(target="Target level", slot="Optional: calculate one slot only")
    @app_commands.choices(slot=SLOT_CHOICES)
    async def plan_cmd(self, interaction: discord.Interaction, target: str, slot: Optional[str] = None):
        log_cmd("chief_gear plan", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        target_key = self._resolve_level_key(target)
        if not target_key:
            await interaction.followup.send(f"Unknown target `{target}`. Try `/chief_gear levels`.", ephemeral=True)
            return

        profile = self._get_profile(int(interaction.guild_id), int(interaction.user.id))
        slots = [slot] if slot else list(SLOT_KEYS)
        totals = {res: 0 for res in RESOURCE_KEYS}
        detail_lines = []
        skipped = []

        for slot_key in slots:
            current_key = profile.get("slots", {}).get(slot_key)
            if not current_key:
                skipped.append(SLOT_NAMES[slot_key])
                continue
            costs = self._cost_between(current_key, target_key)
            for res in RESOURCE_KEYS:
                totals[res] += costs[res]
            detail_lines.append(
                f"**{SLOT_NAMES[slot_key]}**: {self._level_label(current_key)} → {self._level_label(target_key)}"
            )

        inv = profile.get("inventory", {})
        missing = self._missing_costs(inv, totals)
        embed = self._base_embed("Chief Gear Plan")
        embed.add_field(name="Target", value=self._level_label(target_key), inline=True)
        embed.add_field(name="Slots", value=str(len(detail_lines)), inline=True)
        if detail_lines:
            embed.add_field(name="Upgrade path", value="\n".join(detail_lines)[:1000], inline=False)
        if skipped:
            embed.add_field(name="Skipped / not set", value=", ".join(skipped), inline=False)
        embed.add_field(name="Needed", value=self._format_costs(totals), inline=False)
        embed.add_field(name="You have", value=self._format_costs(inv), inline=False)
        embed.add_field(name="Still missing", value=self._format_costs(missing), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @chief_gear.command(name="recommend", description="Recommend cheapest next upgrades using your saved inventory.")
    @app_commands.describe(max_steps="Safety cap only. Default tries to spend until no more upgrades fit.")
    async def recommend_cmd(self, interaction: discord.Interaction, max_steps: app_commands.Range[int, 1, 100] = 100):
        log_cmd("chief_gear recommend", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        profile = self._get_profile(int(interaction.guild_id), int(interaction.user.id))
        working_slots = dict(profile.get("slots", {}))
        inventory = {res: int(profile.get("inventory", {}).get(res, 0) or 0) for res in RESOURCE_KEYS}
        steps = []

        for _ in range(max_steps):
            candidates = []
            for slot_key in SLOT_KEYS:
                current_key = working_slots.get(slot_key)
                if not current_key:
                    continue
                next_key = self._next_level_key(current_key)
                if not next_key:
                    continue
                cost = self._cost_between(current_key, next_key)
                affordable = all(inventory[res] >= cost[res] for res in RESOURCE_KEYS)
                if not affordable:
                    continue
                # Balance first: lower current level first. Then cheaper material footprint.
                material_score = sum(cost.values())
                candidates.append((self._level_order(current_key), material_score, slot_key, next_key, cost))
            if not candidates:
                break
            _, _, slot_key, next_key, cost = sorted(candidates)[0]
            for res in RESOURCE_KEYS:
                inventory[res] -= cost[res]
            old_key = working_slots[slot_key]
            working_slots[slot_key] = next_key
            steps.append((slot_key, old_key, next_key, cost))

        embed = self._base_embed("Chief Gear Recommendations")
        if not steps:
            embed.description = "No affordable next upgrades from the saved profile/inventory."
        else:
            lines = []
            for idx, (slot_key, old_key, next_key, cost) in enumerate(steps[:20], start=1):
                lines.append(f"**{idx}. {SLOT_NAMES[slot_key]}**: {self._level_label(old_key)} → {self._level_label(next_key)}")
            if len(steps) > 20:
                lines.append(f"...and {len(steps) - 20} more affordable upgrades.")
            embed.add_field(name="Upgrade order", value="\n".join(lines)[:1000], inline=False)
            embed.add_field(name="Inventory left after plan", value=self._format_costs(inventory), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @chief_gear.command(name="exchange_plan", description="Plan weekly material exchanges toward a Chief Gear target.")
    @app_commands.describe(
        target="Target level",
        weeks="How many weekly exchange resets before you spend materials",
        convert_to_amber="True = convert surplus plans to amber. False = leave amber to buy/earn.",
    )
    async def exchange_plan_cmd(
        self,
        interaction: discord.Interaction,
        target: str,
        weeks: app_commands.Range[int, 1, 8] = 2,
        convert_to_amber: bool = False,
    ):
        log_cmd("chief_gear exchange_plan", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)

        target_key = self._resolve_level_key(target)
        if not target_key:
            await interaction.followup.send(f"Unknown target `{target}`. Try `/chief_gear levels`.", ephemeral=True)
            return

        profile = self._get_profile(int(interaction.guild_id), int(interaction.user.id))
        needed, detail_lines, skipped = self._target_totals(profile, target_key)
        inv = {res: int(profile.get("inventory", {}).get(res, 0) or 0) for res in RESOURCE_KEYS}
        before_missing = self._missing_costs(inv, needed)
        after_inv, week_lines, after_missing = self._weekly_exchange_plan(inv, needed, int(weeks), bool(convert_to_amber))

        embed = self._base_embed("Chief Gear Weekly Exchange Plan")
        embed.add_field(name="Target", value=f"{self._level_label(target_key)} across **{len(detail_lines)}** saved slots", inline=False)
        if skipped:
            embed.add_field(name="Skipped / not set", value=", ".join(skipped), inline=False)
        embed.add_field(name="Needed", value=self._format_costs(needed), inline=False)
        embed.add_field(name="You have", value=self._format_costs(inv), inline=False)
        embed.add_field(name="Missing before exchange", value=self._format_costs(before_missing), inline=False)
        embed.add_field(
            name="Amber mode",
            value=(
                "Convert surplus Design Plans into Amber where possible."
                if convert_to_amber
                else "Do **not** convert Design Plans into Amber. Amber shortfall stays as buy/earn."
            ),
            inline=False,
        )

        for idx, lines in enumerate(week_lines, start=1):
            embed.add_field(
                name=f"Week {idx} exchange",
                value="\n".join(lines) if lines else "No safe exchange using only surplus materials.",
                inline=False,
            )

        embed.add_field(name="After planned exchanges", value=self._format_costs(after_inv), inline=False)
        embed.add_field(name="Still missing after exchange", value=self._format_costs(after_missing), inline=False)
        embed.set_footer(text="Exchange plan only spends surplus resources so it will not break the target plan.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @exchange_plan_cmd.autocomplete("target")
    async def exchange_plan_target_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._level_autocomplete(interaction, current)

    # ---------- autocomplete / resolve ----------

    def _resolve_level_key(self, value: str) -> Optional[str]:
        raw = (value or "").strip().lower()
        if raw in self.level_by_key:
            return raw
        norm = re.sub(r"[^a-z0-9]+", "", raw)
        for row in self.levels:
            candidates = {
                row["key"],
                row["display"].lower(),
                row["display"].lower().replace("(legendary)", "").replace("(mythic)", ""),
            }
            for candidate in candidates:
                if re.sub(r"[^a-z0-9]+", "", candidate) == norm:
                    return row["key"]
        return None

    async def _level_autocomplete(self, interaction: discord.Interaction, current: str):
        cur = (current or "").lower()
        choices = []
        for row in self.levels:
            name = f"{row['display']} ({row['key']})"
            if not cur or cur in name.lower() or cur in row["key"]:
                choices.append(app_commands.Choice(name=name[:100], value=row["key"]))
            if len(choices) >= 25:
                break
        return choices

    @set_slot_cmd.autocomplete("level")
    async def set_slot_level_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._level_autocomplete(interaction, current)

    @plan_cmd.autocomplete("target")
    async def plan_target_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._level_autocomplete(interaction, current)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)
    cog = ChiefGearCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
