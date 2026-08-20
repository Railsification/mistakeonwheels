# cogs/wos_furnace_calc.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
import re
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from core.logger import log_cmd
from core.settings import SettingsManager
from core.utils import DATA_DIR, ensure_deferred, load_json, save_json


FEATURE_KEY = "wos_furnace"
UPGRADES_PATH = DATA_DIR / "wos_furnace_upgrades.json"
REFINES_PATH = DATA_DIR / "wos_refine_rates.json"
PROFILES_PATH = DATA_DIR / "wos_furnace_profiles.json"


LEVEL_CHOICE_VALUES = [f"FC{i}" for i in range(1, 11)]
LEVEL_CHOICES = [app_commands.Choice(name=value, value=value) for value in LEVEL_CHOICE_VALUES]
PACKAGE_CHOICE_VALUES = ["minimum", "all_camps", "full_furnace"]
PACKAGE_CHOICES = [app_commands.Choice(name=value, value=value) for value in PACKAGE_CHOICE_VALUES]

BUILDING_CHOICES = LEVEL_CHOICES
BUILDING_KEY_TO_LABEL: Dict[str, str] = {
    "furnace": "Furnace",
    "embassy": "Embassy",
    "infantry_camp": "Infantry Camp",
    "marksman_camp": "Marksman Camp",
    "lancer_camp": "Lancer Camp",
    "command_center": "Command Center",
    "infirmary": "Infirmary",
    "war_academy": "War Academy",
}
BUILDING_LABEL_TO_KEY: Dict[str, str] = {
    label.casefold(): key for key, label in BUILDING_KEY_TO_LABEL.items()
}
BUILDING_KEYS: List[str] = list(BUILDING_KEY_TO_LABEL.keys())


DEFAULT_BUILDING_COSTS_BY_TARGET: Dict[str, Dict[str, Dict[str, int]]] = {
    "FC1": {
        "furnace": {"fc": 132, "rfc": 0},
        "embassy": {"fc": 33, "rfc": 0},
        "command_center": {"fc": 26, "rfc": 0},
        "infirmary": {"fc": 26, "rfc": 0},
        "infantry_camp": {"fc": 59, "rfc": 0},
        "marksman_camp": {"fc": 59, "rfc": 0},
        "lancer_camp": {"fc": 59, "rfc": 0},
        "war_academy": {"fc": 0, "rfc": 0},
    },
    "FC2": {
        "furnace": {"fc": 158, "rfc": 0},
        "embassy": {"fc": 39, "rfc": 0},
        "command_center": {"fc": 31, "rfc": 0},
        "infirmary": {"fc": 31, "rfc": 0},
        "infantry_camp": {"fc": 71, "rfc": 0},
        "marksman_camp": {"fc": 71, "rfc": 0},
        "lancer_camp": {"fc": 71, "rfc": 0},
        "war_academy": {"fc": 0, "rfc": 0},
    },
    "FC3": {
        "furnace": {"fc": 238, "rfc": 0},
        "embassy": {"fc": 59, "rfc": 0},
        "command_center": {"fc": 47, "rfc": 0},
        "infirmary": {"fc": 47, "rfc": 0},
        "infantry_camp": {"fc": 107, "rfc": 0},
        "marksman_camp": {"fc": 107, "rfc": 0},
        "lancer_camp": {"fc": 107, "rfc": 0},
        "war_academy": {"fc": 0, "rfc": 0},
    },
    "FC4": {
        "furnace": {"fc": 280, "rfc": 0},
        "embassy": {"fc": 70, "rfc": 0},
        "command_center": {"fc": 56, "rfc": 0},
        "infirmary": {"fc": 56, "rfc": 0},
        "infantry_camp": {"fc": 126, "rfc": 0},
        "marksman_camp": {"fc": 126, "rfc": 0},
        "lancer_camp": {"fc": 126, "rfc": 0},
        "war_academy": {"fc": 0, "rfc": 0},
    },
    "FC5": {
        "furnace": {"fc": 335, "rfc": 0},
        "embassy": {"fc": 83, "rfc": 0},
        "command_center": {"fc": 67, "rfc": 0},
        "infirmary": {"fc": 67, "rfc": 0},
        "infantry_camp": {"fc": 150, "rfc": 0},
        "marksman_camp": {"fc": 150, "rfc": 0},
        "lancer_camp": {"fc": 150, "rfc": 0},
        "war_academy": {"fc": 0, "rfc": 0},
    },
    "FC6": {
        "furnace": {"fc": 900, "rfc": 60},
        "embassy": {"fc": 225, "rfc": 13},
        "command_center": {"fc": 180, "rfc": 13},
        "infirmary": {"fc": 180, "rfc": 13},
        "infantry_camp": {"fc": 405, "rfc": 26},
        "marksman_camp": {"fc": 405, "rfc": 26},
        "lancer_camp": {"fc": 405, "rfc": 26},
        "war_academy": {"fc": 405, "rfc": 26},
    },
    "FC7": {
        "furnace": {"fc": 1080, "rfc": 90},
        "embassy": {"fc": 270, "rfc": 19},
        "command_center": {"fc": 216, "rfc": 19},
        "infirmary": {"fc": 216, "rfc": 19},
        "infantry_camp": {"fc": 486, "rfc": 37},
        "marksman_camp": {"fc": 486, "rfc": 37},
        "lancer_camp": {"fc": 486, "rfc": 37},
        "war_academy": {"fc": 486, "rfc": 37},
    },
    "FC8": {
        "furnace": {"fc": 1080, "rfc": 120},
        "embassy": {"fc": 270, "rfc": 30},
        "command_center": {"fc": 216, "rfc": 29},
        "infirmary": {"fc": 216, "rfc": 30},
        "infantry_camp": {"fc": 486, "rfc": 53},
        "marksman_camp": {"fc": 486, "rfc": 53},
        "lancer_camp": {"fc": 486, "rfc": 53},
        "war_academy": {"fc": 486, "rfc": 53},
    },
    "FC9": {
        "furnace": {"fc": 1260, "rfc": 180},
        "embassy": {"fc": 315, "rfc": 43},
        "command_center": {"fc": 252, "rfc": 36},
        "infirmary": {"fc": 252, "rfc": 36},
        "infantry_camp": {"fc": 567, "rfc": 79},
        "marksman_camp": {"fc": 567, "rfc": 79},
        "lancer_camp": {"fc": 567, "rfc": 79},
        "war_academy": {"fc": 567, "rfc": 79},
    },
    "FC10": {
        "furnace": {"fc": 1575, "rfc": 420},
        "embassy": {"fc": 391, "rfc": 103},
        "command_center": {"fc": 315, "rfc": 84},
        "infirmary": {"fc": 315, "rfc": 84},
        "infantry_camp": {"fc": 706, "rfc": 187},
        "marksman_camp": {"fc": 706, "rfc": 187},
        "lancer_camp": {"fc": 706, "rfc": 187},
        "war_academy": {"fc": 706, "rfc": 187},
    },
}

REQUIRED_CAMP_BY_CURRENT_LEVEL: Dict[str, str] = {
    "FC1": "lancer_camp",
    "FC2": "infantry_camp",
    "FC3": "marksman_camp",
    "FC4": "lancer_camp",
    "FC5": "infantry_camp",
    "FC6": "marksman_camp",
    "FC7": "lancer_camp",
    "FC8": "infantry_camp",
    "FC9": "marksman_camp",
}

DEFAULT_REFINES: Dict[str, Any] = {
    "timezone_note": "All WoS-related date maths should use UTC.",
    "first_refine_discount": 0.5,
    "attempts_above_max_use_last_tier": True,
    "max_search_attempts": 250000,
    "tiers": [
        {
            "name": "Tier 1",
            "min_attempt": 1,
            "max_attempt": 20,
            "fire_crystal_cost": 20,
            "outcomes": [
                {"refined_fire_crystals": 1, "probability": 65.0},
                {"refined_fire_crystals": 2, "probability": 25.0},
                {"refined_fire_crystals": 3, "probability": 10.0},
            ],
        },
        {
            "name": "Tier 2",
            "min_attempt": 21,
            "max_attempt": 40,
            "fire_crystal_cost": 50,
            "outcomes": [
                {"refined_fire_crystals": 2, "probability": 85.0},
                {"refined_fire_crystals": 3, "probability": 15.0},
            ],
        },
        {
            "name": "Tier 3",
            "min_attempt": 41,
            "max_attempt": 60,
            "fire_crystal_cost": 100,
            "outcomes": [
                {"refined_fire_crystals": 3, "probability": 85.0},
                {"refined_fire_crystals": 4, "probability": 12.5},
                {"refined_fire_crystals": 5, "probability": 2.0},
                {"refined_fire_crystals": 6, "probability": 0.5},
            ],
        },
        {
            "name": "Tier 4",
            "min_attempt": 61,
            "max_attempt": 80,
            "fire_crystal_cost": 130,
            "outcomes": [
                {"refined_fire_crystals": 3, "probability": 75.0},
                {"refined_fire_crystals": 4, "probability": 15.0},
                {"refined_fire_crystals": 5, "probability": 5.0},
                {"refined_fire_crystals": 6, "probability": 3.0},
                {"refined_fire_crystals": 7, "probability": 1.0},
                {"refined_fire_crystals": 8, "probability": 0.5},
                {"refined_fire_crystals": 9, "probability": 0.5},
            ],
        },
        {
            "name": "Tier 5",
            "min_attempt": 81,
            "max_attempt": 100,
            "fire_crystal_cost": 160,
            "outcomes": [
                {"refined_fire_crystals": 3, "probability": 70.0},
                {"refined_fire_crystals": 4, "probability": 12.0},
                {"refined_fire_crystals": 5, "probability": 9.0},
                {"refined_fire_crystals": 6, "probability": 4.0},
                {"refined_fire_crystals": 7, "probability": 1.5},
                {"refined_fire_crystals": 8, "probability": 1.0},
                {"refined_fire_crystals": 9, "probability": 1.0},
                {"refined_fire_crystals": 10, "probability": 0.5},
                {"refined_fire_crystals": 11, "probability": 0.5},
                {"refined_fire_crystals": 12, "probability": 0.5},
            ],
        },
    ],
}


def _building_req(label: str, fc: int, rfc: int, key: Optional[str] = None) -> Dict[str, Any]:
    building_key = key or BUILDING_LABEL_TO_KEY.get(label.casefold(), label.strip().lower().replace(" ", "_"))
    return {
        "building": label,
        "key": building_key,
        "fire_crystals": fc,
        "refined_fire_crystals": rfc,
    }


def build_default_upgrades() -> Dict[str, Any]:
    levels: List[Dict[str, Any]] = []
    ordered_levels = [f"FC{i}" for i in range(1, 11)]

    for idx, current_level in enumerate(ordered_levels):
        if current_level == "FC10":
            levels.append({
                "level": "FC10",
                "next_level": None,
                "packages": {},
            })
            break

        target_level = ordered_levels[idx + 1]
        costs = DEFAULT_BUILDING_COSTS_BY_TARGET[target_level]
        required_camp_key = REQUIRED_CAMP_BY_CURRENT_LEVEL[current_level]

        minimum_requirements = [
            _building_req("Furnace", costs["furnace"]["fc"], costs["furnace"]["rfc"], "furnace"),
            _building_req("Embassy", costs["embassy"]["fc"], costs["embassy"]["rfc"], "embassy"),
        ]
        camp_labels = {
            "infantry_camp": "Infantry Camp",
            "marksman_camp": "Marksman Camp",
            "lancer_camp": "Lancer Camp",
        }
        minimum_requirements.append(
            _building_req(
                camp_labels[required_camp_key],
                costs[required_camp_key]["fc"],
                costs[required_camp_key]["rfc"],
                required_camp_key,
            )
        )

        all_camps_requirements = [
            _building_req("Furnace", costs["furnace"]["fc"], costs["furnace"]["rfc"], "furnace"),
            _building_req("Embassy", costs["embassy"]["fc"], costs["embassy"]["rfc"], "embassy"),
            _building_req("Infantry Camp", costs["infantry_camp"]["fc"], costs["infantry_camp"]["rfc"], "infantry_camp"),
            _building_req("Marksman Camp", costs["marksman_camp"]["fc"], costs["marksman_camp"]["rfc"], "marksman_camp"),
            _building_req("Lancer Camp", costs["lancer_camp"]["fc"], costs["lancer_camp"]["rfc"], "lancer_camp"),
        ]

        full_furnace_requirements = list(all_camps_requirements)
        full_furnace_requirements.extend(
            [
                _building_req("Command Center", costs["command_center"]["fc"], costs["command_center"]["rfc"], "command_center"),
                _building_req("Infirmary", costs["infirmary"]["fc"], costs["infirmary"]["rfc"], "infirmary"),
            ]
        )
        if costs["war_academy"]["fc"] > 0 or costs["war_academy"]["rfc"] > 0:
            full_furnace_requirements.append(
                _building_req("War Academy", costs["war_academy"]["fc"], costs["war_academy"]["rfc"], "war_academy")
            )

        levels.append(
            {
                "level": current_level,
                "next_level": target_level,
                "packages": {
                    "minimum": {
                        "description": f"Furnace + Embassy + required troop camp for {current_level} → {target_level}",
                        "requirements": minimum_requirements,
                    },
                    "all_camps": {
                        "description": f"Furnace + Embassy + all troop camps for {current_level} → {target_level}",
                        "requirements": all_camps_requirements,
                    },
                    "full_furnace": {
                        "description": f"Full furnace package for {current_level} → {target_level}",
                        "requirements": full_furnace_requirements,
                    },
                },
            }
        )

    return {
        "timezone": "UTC",
        "feature_key": FEATURE_KEY,
        "levels": levels,
    }



def default_building_levels(base_level: str) -> Dict[str, str]:
    return {key: base_level for key in BUILDING_KEYS}

class ReferenceError(ValueError):
    pass


@dataclass
class RefineWindowProjection:
    weekly_refines: int
    total_attempts: int
    fire_crystal_spent: int
    minimum_rfc: int
    expected_rfc: float
    maximum_rfc: int


class WOSFurnaceCalculator(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self.upgrades: Dict[str, Any] = {}
        self.refines: Dict[str, Any] = {}
        self.profiles: Dict[str, Any] = {}
        self.level_map: Dict[str, Dict[str, Any]] = {}
        self.level_names: List[str] = []
        self.timezone_name: str = "UTC"
        self.load_reference_files()
        self.load_profiles()

    # -----------------------------
    # loading
    # -----------------------------
    def load_reference_files(self) -> None:
        self.upgrades = self._load_or_create_json(UPGRADES_PATH, build_default_upgrades())
        self.refines = self._load_or_create_json(REFINES_PATH, DEFAULT_REFINES)
        self._validate_upgrades(self.upgrades)
        self._validate_refines(self.refines)
        self.timezone_name = str(self.upgrades.get("timezone", "UTC"))
        self.level_map = {
            str(entry["level"]).strip().casefold(): entry
            for entry in self.upgrades["levels"]
        }
        self.level_names = [entry["level"] for entry in self.upgrades["levels"]]

    def load_profiles(self) -> None:
        data = self._load_or_create_json(PROFILES_PATH, {})
        if not isinstance(data, dict):
            raise ReferenceError("wos_furnace_profiles.json must be a JSON object.")
        self.profiles = {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def save_profiles(self) -> None:
        save_json(PROFILES_PATH, self.profiles)

    @staticmethod
    def _load_or_create_json(path, default_value: Any) -> Any:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            save_json(path, default_value)
            return load_json(path, default_value)
        return load_json(path, default_value)

    @staticmethod
    def _validate_upgrades(data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ReferenceError("wos_furnace_upgrades.json must be a JSON object.")
        levels = data.get("levels")
        if not isinstance(levels, list) or not levels:
            raise ReferenceError("wos_furnace_upgrades.json must contain a non-empty 'levels' list.")
        seen: set[str] = set()
        for idx, entry in enumerate(levels, start=1):
            if not isinstance(entry, dict):
                raise ReferenceError(f"levels[{idx}] must be an object.")
            level = entry.get("level")
            next_level = entry.get("next_level")
            packages = entry.get("packages", {})
            if not isinstance(level, str) or not level.strip():
                raise ReferenceError(f"levels[{idx}].level must be a non-empty string.")
            key = level.strip().casefold()
            if key in seen:
                raise ReferenceError(f"Duplicate level found in upgrades reference: {level}")
            seen.add(key)
            if next_level is not None and (not isinstance(next_level, str) or not next_level.strip()):
                raise ReferenceError(f"levels[{idx}].next_level must be null or a non-empty string.")
            if not isinstance(packages, dict):
                raise ReferenceError(f"levels[{idx}].packages must be an object.")

    @staticmethod
    def _validate_refines(data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ReferenceError("wos_refine_rates.json must be a JSON object.")
        tiers = data.get("tiers")
        if not isinstance(tiers, list) or not tiers:
            raise ReferenceError("wos_refine_rates.json must contain a non-empty 'tiers' list.")
        previous_max = 0
        for idx, tier in enumerate(tiers, start=1):
            min_attempt = tier.get("min_attempt")
            max_attempt = tier.get("max_attempt")
            if not isinstance(min_attempt, int) or not isinstance(max_attempt, int):
                raise ReferenceError(f"tiers[{idx}] needs integer min_attempt and max_attempt.")
            if min_attempt != previous_max + 1:
                raise ReferenceError(
                    f"Refine tiers must be contiguous. Tier {tier.get('name', idx)} starts at {min_attempt}, expected {previous_max + 1}."
                )
            previous_max = max_attempt
            outcomes = tier.get("outcomes")
            if not isinstance(outcomes, list) or not outcomes:
                raise ReferenceError(f"tiers[{idx}] must contain outcomes.")
            total = sum(float(outcome.get("probability", 0.0)) for outcome in outcomes)
            if not (math.isclose(total, 100.0, abs_tol=1e-9) or math.isclose(total, 1.0, abs_tol=1e-9)):
                raise ReferenceError(f"Tier {tier.get('name', idx)} probabilities must sum to 100 or 1.0.")

    # -----------------------------
    # access / restriction
    # -----------------------------
    async def _ensure_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None or interaction.channel_id is None:
            if interaction.response.is_done():
                await interaction.followup.send("❌ This command can only be used inside a server channel.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ This command can only be used inside a server channel.", ephemeral=True)
            return False
        if self.settings.is_feature_allowed(interaction.guild_id, interaction.channel_id, FEATURE_KEY):
            return True
        msg = (
            f"❌ This command is not allowed in this channel. "
            f"Use `/feature_channel_add` with feature `{FEATURE_KEY}` to allow it in a channel."
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    # -----------------------------
    # general helpers
    # -----------------------------
    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def _now_local_date(self) -> date:
        return datetime.now(self._tz()).date()

    def _parse_target_date(self, value: str) -> date:
        value = value.strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        raise ReferenceError("Date must be YYYY-MM-DD, DD/MM/YYYY, or DD-MM-YYYY.")

    @staticmethod
    def _normalize_level_name(level_name: str) -> str:
        return level_name.strip().casefold()

    def _get_level_entry(self, level_name: str) -> Dict[str, Any]:
        entry = self.level_map.get(self._normalize_level_name(level_name))
        if entry is None:
            raise ReferenceError(f"Unknown level '{level_name}'.")
        return entry

    def _get_profile(self, user_id: int) -> Dict[str, Any]:
        return self.profiles.get(str(user_id), {})

    @staticmethod
    def _require_non_negative(name: str, value: Optional[int]) -> int:
        if value is None:
            return 0
        if value < 0:
            raise ReferenceError(f"{name} cannot be negative.")
        return value

    @staticmethod
    def _fmt_int(value: int) -> str:
        return f"{value:,}"

    @staticmethod
    def _fmt_float(value: float) -> str:
        return f"{value:,.2f}"

    @staticmethod
    def _parse_int_text(name: str, value: str) -> int:
        cleaned = str(value).strip().replace(",", "").replace(" ", "")
        if not cleaned:
            raise ReferenceError(f"{name} is required.")
        try:
            parsed = int(cleaned)
        except ValueError as exc:
            raise ReferenceError(f"{name} must be a whole number.") from exc
        if parsed < 0:
            raise ReferenceError(f"{name} cannot be negative.")
        return parsed

    def _parse_number_pair(self, name: str, value: str) -> tuple[int, int]:
        raw = str(value).strip()
        parts: List[str] = []
        for separator in ("/", "|", ":"):
            if separator in raw:
                parts = [part.strip() for part in raw.split(separator, 1)]
                break
        if not parts:
            parts = raw.split()
        if len(parts) != 2:
            raise ReferenceError(f"{name} must be entered as `FC / RFC`, for example `2622 / 118`.")
        return (
            self._parse_int_text(f"{name} FC", parts[0]),
            self._parse_int_text(f"{name} RFC", parts[1]),
        )

    def _canonical_level(self, value: str) -> str:
        return str(self._get_level_entry(str(value).strip())["level"])

    @staticmethod
    def _canonical_package(value: str) -> str:
        cleaned = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
        aliases = {
            "min": "minimum",
            "minimum": "minimum",
            "all": "all_camps",
            "all_camp": "all_camps",
            "all_camps": "all_camps",
            "full": "full_furnace",
            "full_furnace": "full_furnace",
        }
        package = aliases.get(cleaned)
        if package is None:
            raise ReferenceError("Package must be `minimum`, `all_camps`, or `full_furnace`.")
        return package

    def _parse_level_pair(self, value: str) -> tuple[str, str]:
        raw = str(value).strip()
        parts: List[str] = []
        for separator in ("->", "→", "/", "|"):
            if separator in raw:
                parts = [part.strip() for part in raw.split(separator, 1)]
                break
        if not parts:
            parts = raw.split()
        if len(parts) != 2:
            raise ReferenceError("Levels must be entered as `CURRENT -> TARGET`, for example `FC5 -> FC10`.")
        return self._canonical_level(parts[0]), self._canonical_level(parts[1])

    def _parse_level_package(self, value: str) -> tuple[str, str]:
        raw = str(value).strip()
        parts: List[str] = []
        for separator in ("/", "|"):
            if separator in raw:
                parts = [part.strip() for part in raw.split(separator, 1)]
                break
        if not parts:
            parts = raw.split(maxsplit=1)
        if len(parts) != 2:
            raise ReferenceError("Enter `CURRENT LEVEL / PACKAGE`, for example `FC5 / minimum`.")
        return self._canonical_level(parts[0]), self._canonical_package(parts[1])


    def _level_value(self, level_name: str) -> int:
        entry = self._get_level_entry(level_name)
        match = re.search(r"(\d+)", entry["level"])
        if not match:
            raise ReferenceError(f"Invalid level name '{level_name}'.")
        return int(match.group(1))

    def _canonical_building_key(self, raw_key: str) -> str:
        key = raw_key.strip().casefold()
        if key in BUILDING_KEY_TO_LABEL:
            return key
        mapped = BUILDING_LABEL_TO_KEY.get(key)
        if mapped:
            return mapped
        raise ReferenceError(f"Unknown building '{raw_key}'.")

    def _normalize_building_levels(self, base_level: str, raw_levels: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        self._get_level_entry(base_level)
        levels = default_building_levels(base_level)
        if isinstance(raw_levels, dict):
            for raw_key, raw_value in raw_levels.items():
                if raw_value is None:
                    continue
                key = self._canonical_building_key(str(raw_key))
                level_name = str(raw_value).strip()
                if not level_name:
                    continue
                self._get_level_entry(level_name)
                levels[key] = level_name
        return levels

    def _serialize_building_levels(self, levels: Dict[str, str]) -> Dict[str, str]:
        return {key: levels[key] for key in BUILDING_KEYS}

    def _buildings_field_text(self, levels: Dict[str, str]) -> str:
        return "\n".join(f"{BUILDING_KEY_TO_LABEL[key]}: **{levels[key]}**" for key in BUILDING_KEYS)

    def _base_embed(self, title: str, description: str = "") -> discord.Embed:
        embed = discord.Embed(title=title, description=description, colour=discord.Colour.orange())
        embed.set_footer(text=f"TZ: {self.timezone_name}")
        return embed

    def _project_weekly_amount(self, amount_per_week: int, start_date: date, target_date: date) -> int:
        if amount_per_week <= 0 or target_date < start_date:
            return 0
        total_days = (target_date - start_date).days + 1
        full_weeks = total_days // 7
        partial_days = total_days % 7
        return (full_weeks * amount_per_week) + math.floor((amount_per_week * partial_days) / 7)


    def _merge_profile_defaults(
        self,
        user_id: int,
        use_saved: bool,
        current_level: Optional[str],
        current_fire_crystals: Optional[int],
        current_refined_fire_crystals: Optional[int],
        package: Optional[str],
        weekly_fire_crystals_income: Optional[int],
        weekly_rfc_income: Optional[int],
    ) -> Dict[str, Any]:
        profile = self._get_profile(user_id) if use_saved else {}
        profile_buildings = profile.get("current_buildings") if isinstance(profile.get("current_buildings"), dict) else {}

        derived_level = current_level if current_level is not None else profile.get("current_level")
        if current_level is None and profile_buildings.get("furnace"):
            derived_level = str(profile_buildings["furnace"])

        if not derived_level:
            raise ReferenceError("current_level is required. Set a profile or pass it in the command.")

        if current_level is not None:
            self._get_level_entry(current_level)
            if profile_buildings:
                profile_buildings = dict(profile_buildings)
                profile_buildings["furnace"] = current_level

        current_buildings = self._normalize_building_levels(str(derived_level), profile_buildings)

        merged = {
            "current_level": current_buildings["furnace"],
            "current_buildings": current_buildings,
            "current_fire_crystals": (
                current_fire_crystals if current_fire_crystals is not None else profile.get("fire_crystals", 0)
            ),
            "current_refined_fire_crystals": (
                current_refined_fire_crystals
                if current_refined_fire_crystals is not None
                else profile.get("refined_fire_crystals", 0)
            ),
            "package": package if package is not None else profile.get("preferred_package", "minimum"),
            "weekly_fire_crystals_income": (
                weekly_fire_crystals_income
                if weekly_fire_crystals_income is not None
                else profile.get("weekly_fire_crystals_income", 0)
            ),
            "weekly_rfc_income": (
                weekly_rfc_income
                if weekly_rfc_income is not None
                else profile.get("weekly_refined_fire_crystals_income", 0)
            ),
        }
        return merged

    # -----------------------------
    # refine helpers
    # -----------------------------
    def _tier_probability_scale(self, tier: Dict[str, Any]) -> float:
        total = sum(float(outcome["probability"]) for outcome in tier["outcomes"])
        return 100.0 if math.isclose(total, 100.0, abs_tol=1e-9) else 1.0

    def _tier_min_rfc(self, tier: Dict[str, Any]) -> int:
        return min(outcome["refined_fire_crystals"] for outcome in tier["outcomes"])

    def _tier_expected_rfc(self, tier: Dict[str, Any]) -> float:
        scale = self._tier_probability_scale(tier)
        return sum(
            outcome["refined_fire_crystals"] * (float(outcome["probability"]) / scale)
            for outcome in tier["outcomes"]
        )

    def _tier_max_rfc(self, tier: Dict[str, Any]) -> int:
        return max(outcome["refined_fire_crystals"] for outcome in tier["outcomes"])

    def _tier_for_attempt(self, attempt_number: int) -> Dict[str, Any]:
        tiers: List[Dict[str, Any]] = self.refines["tiers"]
        for tier in tiers:
            if tier["min_attempt"] <= attempt_number <= tier["max_attempt"]:
                return tier
        if self.refines.get("attempts_above_max_use_last_tier", True):
            return tiers[-1]
        raise ReferenceError("Refine attempt exceeds the configured tiers.")

    @staticmethod
    def _weekly_day_counts(total_attempts: int, days: int = 7) -> List[int]:
        if total_attempts <= 0 or days <= 0:
            return [0] * days
        if total_attempts <= days:
            return [1 if i < total_attempts else 0 for i in range(days)]
        counts = [1] * days
        counts[0] += total_attempts - days
        return counts

    def _format_schedule_counts(self, start_day: date, counts: List[int]) -> str:
        return " | ".join(
            f"{(start_day + timedelta(days=i)).strftime('%a')} {counts[i]}"
            for i in range(len(counts))
        )

    def _full_week_schedule(self, weekly_refines: int) -> str:
        return self._format_schedule_counts(date(2024, 1, 1), self._weekly_day_counts(weekly_refines, 7))

    def _window_segments(self, start_date: date, target_date: date) -> List[tuple[date, date, int]]:
        if target_date < start_date:
            return []
        segments: List[tuple[date, date, int]] = []
        cursor = start_date
        while cursor <= target_date:
            days_until_sunday = 6 - cursor.weekday()
            segment_end = min(target_date, cursor + timedelta(days=days_until_sunday))
            segment_days = (segment_end - cursor).days + 1
            segments.append((cursor, segment_end, segment_days))
            cursor = segment_end + timedelta(days=1)
        return segments

    def _window_schedule_segments(self, weekly_refines: int, start_date: date, target_date: date) -> List[Dict[str, Any]]:
        segments: List[Dict[str, Any]] = []
        for segment_start, segment_end, segment_days in self._window_segments(start_date, target_date):
            segment_attempts = math.floor((weekly_refines * segment_days) / 7)
            counts = self._weekly_day_counts(segment_attempts, segment_days)
            segments.append(
                {
                    "start": segment_start,
                    "end": segment_end,
                    "days": segment_days,
                    "attempts": segment_attempts,
                    "counts": counts,
                    "text": self._format_schedule_counts(segment_start, counts),
                }
            )
        return segments

    def _schedule_text_for_window(self, weekly_refines: int, start_date: date, target_date: date) -> str:
        segments = self._window_schedule_segments(weekly_refines, start_date, target_date)
        if not segments:
            return "No refines in window."
        if len(segments) == 1:
            segment = segments[0]
            if segment["days"] == 7 and segment["start"].weekday() == 0:
                return f"This week: {segment['text']}"
            return f"Current window ({segment['start'].strftime('%a')}-{segment['end'].strftime('%a')}): {segment['text']}"

        lines: List[str] = []
        first = segments[0]
        lines.append(
            f"Current week ({first['start'].strftime('%a')}-{first['end'].strftime('%a')}): {first['text']}"
        )

        middle_full = [segment for segment in segments[1:-1] if segment["days"] == 7]
        if middle_full:
            lines.append(f"Each full week after Monday reset: {middle_full[0]['text']}")
        elif len(segments) >= 2 and segments[1]["days"] == 7:
            lines.append(f"Next full week from Monday reset: {segments[1]['text']}")

        last = segments[-1]
        if last is not first and last["days"] < 7:
            lines.append(
                f"Final partial week ({last['start'].strftime('%a')}-{last['end'].strftime('%a')}): {last['text']}"
            )

        return "\n".join(lines)

    def simulate_window_refines(self, weekly_refines: int, start_date: date, target_date: date) -> RefineWindowProjection:
        if weekly_refines < 0:
            raise ReferenceError("weekly_refines cannot be negative.")
        if target_date < start_date:
            return RefineWindowProjection(weekly_refines, 0, 0, 0, 0.0, 0)

        total_attempts = 0
        total_fc_spent = 0.0
        total_min_rfc = 0
        total_expected_rfc = 0.0
        total_max_rfc = 0
        first_refine_discount = float(self.refines.get("first_refine_discount", 0.5))

        for _segment_start, _segment_end, segment_days in self._window_segments(start_date, target_date):
            segment_attempts = math.floor((weekly_refines * segment_days) / 7)
            day_counts = self._weekly_day_counts(segment_attempts, segment_days)
            weekly_attempt_number = 0
            for attempts_today in day_counts:
                for attempt_in_day in range(attempts_today):
                    weekly_attempt_number += 1
                    tier = self._tier_for_attempt(weekly_attempt_number)
                    fc_cost = float(tier["fire_crystal_cost"])
                    if attempt_in_day == 0:
                        fc_cost *= (1.0 - first_refine_discount)
                    total_fc_spent += fc_cost
                    total_min_rfc += self._tier_min_rfc(tier)
                    total_expected_rfc += self._tier_expected_rfc(tier)
                    total_max_rfc += self._tier_max_rfc(tier)
                    total_attempts += 1

        return RefineWindowProjection(
            weekly_refines=weekly_refines,
            total_attempts=total_attempts,
            fire_crystal_spent=int(round(total_fc_spent)),
            minimum_rfc=total_min_rfc,
            expected_rfc=total_expected_rfc,
            maximum_rfc=total_max_rfc,
        )

    def find_min_weekly_refines_for_rfc(self, required_rfc: int, start_date: date, target_date: date, mode: str) -> RefineWindowProjection:
        if required_rfc <= 0:
            return self.simulate_window_refines(0, start_date, target_date)
        if mode not in {"minimum", "expected"}:
            raise ReferenceError("mode must be minimum or expected.")

        def produced(weekly_refines: int) -> float:
            projection = self.simulate_window_refines(weekly_refines, start_date, target_date)
            return projection.minimum_rfc if mode == "minimum" else projection.expected_rfc

        high = 1
        max_weekly_refines = int(self.refines.get("max_search_attempts", 250000))
        while high <= max_weekly_refines and produced(high) < required_rfc:
            high *= 2
        if high > max_weekly_refines:
            high = max_weekly_refines
        if produced(high) < required_rfc:
            raise ReferenceError("Could not satisfy required RFC inside the configured search range.")

        low = 0
        while low < high:
            mid = (low + high) // 2
            if produced(mid) >= required_rfc:
                high = mid
            else:
                low = mid + 1
        return self.simulate_window_refines(low, start_date, target_date)

    def find_max_weekly_refines_for_fc_budget(self, fc_budget: int, start_date: date, target_date: date) -> RefineWindowProjection:
        if fc_budget <= 0:
            return self.simulate_window_refines(0, start_date, target_date)

        def spent(weekly_refines: int) -> int:
            return self.simulate_window_refines(weekly_refines, start_date, target_date).fire_crystal_spent

        low = 0
        high = 1
        max_weekly_refines = int(self.refines.get("max_search_attempts", 250000))
        while high <= max_weekly_refines and spent(high) <= fc_budget:
            high *= 2
        if high > max_weekly_refines:
            high = max_weekly_refines
        if spent(high) <= fc_budget:
            return self.simulate_window_refines(high, start_date, target_date)

        while low < high:
            mid = (low + high + 1) // 2
            if spent(mid) <= fc_budget:
                low = mid
            else:
                high = mid - 1
        return self.simulate_window_refines(low, start_date, target_date)

    # -----------------------------
    # upgrade helpers
    # -----------------------------
    def _get_package_name(self, level_entry: Dict[str, Any], package_name: str) -> str:
        wanted = package_name.strip().casefold()
        for actual_name in level_entry.get("packages", {}).keys():
            if actual_name.casefold() == wanted:
                return actual_name
        available = ", ".join(level_entry.get("packages", {}).keys())
        raise ReferenceError(f"Unknown package '{package_name}' for level {level_entry['level']}. Available: {available}")

    def _building_level_at_or_above(self, current_level: str, required_level: str) -> bool:
        return self._level_value(current_level) >= self._level_value(required_level)

    def resolve_package(self, level_entry: Dict[str, Any], package_name: str) -> Dict[str, Any]:
        if level_entry.get("next_level") is None:
            return {
                "package_name": package_name,
                "description": "Terminal level.",
                "fire_crystals": 0,
                "refined_fire_crystals": 0,
                "selected_buildings": [],
            }
        actual_package_name = self._get_package_name(level_entry, package_name)
        package = level_entry["packages"][actual_package_name]
        total_fc = 0
        total_rfc = 0
        selected_buildings: List[Dict[str, Any]] = []
        for req in package.get("requirements", []):
            building_name = req.get("building", "Unknown")
            building_key = req.get("key") or BUILDING_LABEL_TO_KEY.get(str(building_name).casefold(), str(building_name))
            selected_buildings.append(
                {
                    "key": building_key,
                    "building": building_name,
                    "fire_crystals": int(req.get("fire_crystals", 0)),
                    "refined_fire_crystals": int(req.get("refined_fire_crystals", 0)),
                }
            )
            total_fc += int(req.get("fire_crystals", 0))
            total_rfc += int(req.get("refined_fire_crystals", 0))
        return {
            "package_name": actual_package_name,
            "description": package.get("description", ""),
            "fire_crystals": total_fc,
            "refined_fire_crystals": total_rfc,
            "selected_buildings": selected_buildings,
        }

    def _costed_step_from_buildings(
        self,
        level_entry: Dict[str, Any],
        package_name: str,
        building_levels: Dict[str, str],
    ) -> Dict[str, Any]:
        resolved = self.resolve_package(level_entry, package_name)
        next_level = level_entry.get("next_level")
        if not next_level:
            return {
                **resolved,
                "fire_crystals": 0,
                "refined_fire_crystals": 0,
                "selected_buildings": [],
            }

        selected_buildings: List[Dict[str, Any]] = []
        total_fc = 0
        total_rfc = 0
        for building in resolved["selected_buildings"]:
            key = self._canonical_building_key(building["key"])
            current_building_level = building_levels.get(key, level_entry["level"])
            if self._building_level_at_or_above(current_building_level, next_level):
                continue
            selected_buildings.append(building)
            total_fc += building["fire_crystals"]
            total_rfc += building["refined_fire_crystals"]

        return {
            **resolved,
            "fire_crystals": total_fc,
            "refined_fire_crystals": total_rfc,
            "selected_buildings": selected_buildings,
        }

    def _apply_step_to_buildings(
        self,
        building_levels: Dict[str, str],
        level_entry: Dict[str, Any],
        costed_step: Dict[str, Any],
    ) -> Dict[str, str]:
        updated = dict(building_levels)
        next_level = level_entry.get("next_level")
        if not next_level:
            return updated
        updated["furnace"] = next_level
        for building in costed_step.get("selected_buildings", []):
            updated[self._canonical_building_key(building["key"])] = next_level
        return updated

    def build_upgrade_steps(
        self,
        current_level: str,
        target_level: str,
        package_name: str,
        current_buildings: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        current_entry = self._get_level_entry(current_level)
        target_key = self._normalize_level_name(target_level)
        building_levels = self._normalize_building_levels(current_entry["level"], current_buildings)
        if self._normalize_level_name(current_entry["level"]) == target_key:
            return []
        steps: List[Dict[str, Any]] = []
        visited: set[str] = set()
        level_name = current_entry["level"]
        while self._normalize_level_name(level_name) != target_key:
            level_entry = self._get_level_entry(level_name)
            key = self._normalize_level_name(level_entry["level"])
            if key in visited:
                raise ReferenceError("Upgrade path loop detected in reference file.")
            visited.add(key)
            next_level = level_entry.get("next_level")
            if not next_level:
                raise ReferenceError(f"Cannot continue from level {level_entry['level']}.")
            costed_step = self._costed_step_from_buildings(level_entry, package_name, building_levels)
            steps.append({
                "from_level": level_entry["level"],
                "to_level": next_level,
                **costed_step,
            })
            building_levels = self._apply_step_to_buildings(building_levels, level_entry, costed_step)
            level_name = next_level
        return steps

    def summarize_steps(self, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "fire_crystals": sum(step["fire_crystals"] for step in steps),
            "refined_fire_crystals": sum(step["refined_fire_crystals"] for step in steps),
            "steps": steps,
        }

    def forecast_reachable_level(
        self,
        current_level: str,
        available_fc: int,
        available_rfc: int,
        package_name: str,
        current_buildings: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        steps_taken: List[Dict[str, Any]] = []
        cursor_level = current_level
        remaining_fc = available_fc
        remaining_rfc = available_rfc
        building_levels = self._normalize_building_levels(current_level, current_buildings)
        while True:
            level_entry = self._get_level_entry(cursor_level)
            next_level = level_entry.get("next_level")
            if not next_level:
                break
            resolved = self._costed_step_from_buildings(level_entry, package_name, building_levels)
            if remaining_fc < resolved["fire_crystals"] or remaining_rfc < resolved["refined_fire_crystals"]:
                break
            remaining_fc -= resolved["fire_crystals"]
            remaining_rfc -= resolved["refined_fire_crystals"]
            steps_taken.append({
                "from_level": level_entry["level"],
                "to_level": next_level,
                **resolved,
            })
            building_levels = self._apply_step_to_buildings(building_levels, level_entry, resolved)
            cursor_level = next_level
        next_step = None
        level_entry = self._get_level_entry(cursor_level)
        if level_entry.get("next_level"):
            next_step = {
                "from_level": level_entry["level"],
                "to_level": level_entry["next_level"],
                **self._costed_step_from_buildings(level_entry, package_name, building_levels),
            }
        return {
            "reached_level": cursor_level,
            "remaining_fc": remaining_fc,
            "remaining_rfc": remaining_rfc,
            "steps_taken": steps_taken,
            "next_step": next_step,
            "building_levels": building_levels,
        }

    def _step_building_summary(self, step: Dict[str, Any]) -> str:
        if not step.get("selected_buildings"):
            return "No buildings"
        return ", ".join(building["building"] for building in step["selected_buildings"])

    # -----------------------------
    # help embeds
    # -----------------------------
    def _build_help_embeds(self) -> List[discord.Embed]:
        embed = self._base_embed(
            "WoS Furnace Calculator",
            "The furnace tools now use popups. Run a command, fill in the boxes, then press **Submit**.",
        )
        embed.add_field(
            name="Save your details",
            value=(
                "`/furnace_profile_set` opens the profile popup. Your saved values are prefilled.\n"
                "`/furnace_profile_view` shows your profile and gives you edit buttons."
            ),
            inline=False,
        )
        embed.add_field(
            name="Run a calculator",
            value=(
                "`/furnace_refines_needed` — works out the refines needed by a target date.\n"
                "`/furnace_upgrade_forecast` — shows the level your weekly refine plan can reach."
            ),
            inline=False,
        )
        embed.add_field(
            name="Popup format",
            value=(
                "Levels: `FC5 -> FC10`\n"
                "Resources: `2622 / 118` means FC / RFC\n"
                "Weekly income: `500 / 20` means FC / RFC"
            ),
            inline=False,
        )
        embed.add_field(
            name="Packages",
            value=(
                "`minimum` — Furnace + Embassy + required troop camp\n"
                "`all_camps` — Furnace + Embassy + all three troop camps\n"
                "`full_furnace` — full package including support buildings"
            ),
            inline=False,
        )
        embed.add_field(
            name="Refine maths",
            value=(
                "Tiers reset Monday UTC. The first refine each day is half price. "
                "Results show guaranteed and expected RFC."
            ),
            inline=False,
        )
        return [embed]

    # -----------------------------

    # -----------------------------
    # profile commands
    # -----------------------------
    def _create_profile_values(
        self,
        user_id: int,
        current_level: str,
        current_fire_crystals: int = 0,
        current_refined_fire_crystals: int = 0,
        weekly_refines: int = 0,
        preferred_package: str = "minimum",
        weekly_fire_crystals_income: int = 0,
        weekly_rfc_income: int = 0,
    ) -> Dict[str, Any]:
        current_level = self._canonical_level(current_level)
        preferred_package = self._canonical_package(preferred_package)
        self._require_non_negative("current_fire_crystals", current_fire_crystals)
        self._require_non_negative("current_refined_fire_crystals", current_refined_fire_crystals)
        self._require_non_negative("weekly_refines", weekly_refines)
        self._require_non_negative("weekly_fire_crystals_income", weekly_fire_crystals_income)
        self._require_non_negative("weekly_rfc_income", weekly_rfc_income)
        profile = {
            "current_level": current_level,
            "current_buildings": self._serialize_building_levels(default_building_levels(current_level)),
            "fire_crystals": int(current_fire_crystals),
            "refined_fire_crystals": int(current_refined_fire_crystals),
            "weekly_refines": int(weekly_refines),
            "preferred_package": preferred_package,
            "weekly_fire_crystals_income": int(weekly_fire_crystals_income),
            "weekly_refined_fire_crystals_income": int(weekly_rfc_income),
            "updated_at": datetime.now(self._tz()).isoformat(timespec="seconds"),
        }
        self.profiles[str(user_id)] = profile
        self.save_profiles()
        return profile

    def _update_profile_values(
        self,
        user_id: int,
        current_level: Optional[str] = None,
        current_fire_crystals: Optional[int] = None,
        current_refined_fire_crystals: Optional[int] = None,
        weekly_refines: Optional[int] = None,
        preferred_package: Optional[str] = None,
        weekly_fire_crystals_income: Optional[int] = None,
        weekly_rfc_income: Optional[int] = None,
    ) -> Dict[str, Any]:
        profile = self._get_profile(user_id)
        if not profile:
            raise ReferenceError("No saved profile found. Use /furnace_profile_set first.")
        current_buildings = self._normalize_building_levels(
            str(profile.get("current_level", "FC1")),
            profile.get("current_buildings") if isinstance(profile.get("current_buildings"), dict) else None,
        )
        if current_level is not None:
            current_level = self._canonical_level(current_level)
            profile["current_level"] = current_level
            current_buildings["furnace"] = current_level
        if current_fire_crystals is not None:
            self._require_non_negative("current_fire_crystals", current_fire_crystals)
            profile["fire_crystals"] = int(current_fire_crystals)
        if current_refined_fire_crystals is not None:
            self._require_non_negative("current_refined_fire_crystals", current_refined_fire_crystals)
            profile["refined_fire_crystals"] = int(current_refined_fire_crystals)
        if weekly_refines is not None:
            self._require_non_negative("weekly_refines", weekly_refines)
            profile["weekly_refines"] = int(weekly_refines)
        if preferred_package is not None:
            profile["preferred_package"] = self._canonical_package(preferred_package)
        if weekly_fire_crystals_income is not None:
            self._require_non_negative("weekly_fire_crystals_income", weekly_fire_crystals_income)
            profile["weekly_fire_crystals_income"] = int(weekly_fire_crystals_income)
        if weekly_rfc_income is not None:
            self._require_non_negative("weekly_rfc_income", weekly_rfc_income)
            profile["weekly_refined_fire_crystals_income"] = int(weekly_rfc_income)
        profile["current_buildings"] = self._serialize_building_levels(current_buildings)
        profile["updated_at"] = datetime.now(self._tz()).isoformat(timespec="seconds")
        self.save_profiles()
        return profile

    def _update_profile_building_values(
        self,
        user_id: int,
        furnace: Optional[str] = None,
        embassy: Optional[str] = None,
        infantry_camp: Optional[str] = None,
        marksman_camp: Optional[str] = None,
        lancer_camp: Optional[str] = None,
        command_center: Optional[str] = None,
        infirmary: Optional[str] = None,
        war_academy: Optional[str] = None,
    ) -> Dict[str, Any]:
        profile = self._get_profile(user_id)
        if not profile:
            raise ReferenceError("No saved profile found. Use /furnace_profile_set first.")
        base_level = str(profile.get("current_level", "FC1"))
        building_levels = self._normalize_building_levels(
            base_level,
            profile.get("current_buildings") if isinstance(profile.get("current_buildings"), dict) else None,
        )
        updates = {
            "furnace": furnace,
            "embassy": embassy,
            "infantry_camp": infantry_camp,
            "marksman_camp": marksman_camp,
            "lancer_camp": lancer_camp,
            "command_center": command_center,
            "infirmary": infirmary,
            "war_academy": war_academy,
        }
        changed = False
        for key, value in updates.items():
            if value is None:
                continue
            building_levels[key] = self._canonical_level(value)
            changed = True
        if not changed:
            raise ReferenceError("Pass at least one building level to update.")
        profile["current_level"] = building_levels["furnace"]
        profile["current_buildings"] = self._serialize_building_levels(building_levels)
        profile["updated_at"] = datetime.now(self._tz()).isoformat(timespec="seconds")
        self.save_profiles()
        return profile

    def _build_profile_embed(self, profile: Dict[str, Any]) -> discord.Embed:
        embed = self._base_embed(
            "Saved Furnace Profile",
            "Use the buttons below to edit one section without re-entering everything.",
        )
        embed.add_field(name="Current level", value=str(profile.get("current_level", "-")), inline=True)
        embed.add_field(name="Fire Crystals", value=self._fmt_int(int(profile.get("fire_crystals", 0))), inline=True)
        embed.add_field(
            name="Refined Fire Crystals",
            value=self._fmt_int(int(profile.get("refined_fire_crystals", 0))),
            inline=True,
        )
        embed.add_field(name="Weekly refines", value=self._fmt_int(int(profile.get("weekly_refines", 0))), inline=True)
        embed.add_field(name="Preferred package", value=str(profile.get("preferred_package", "minimum")), inline=True)
        embed.add_field(
            name="Weekly income",
            value=(
                f"FC **{self._fmt_int(int(profile.get('weekly_fire_crystals_income', 0)))}** | "
                f"RFC **{self._fmt_int(int(profile.get('weekly_refined_fire_crystals_income', 0)))}**"
            ),
            inline=False,
        )
        building_levels = self._normalize_building_levels(
            str(profile.get("current_level", "FC1")),
            profile.get("current_buildings") if isinstance(profile.get("current_buildings"), dict) else None,
        )
        embed.add_field(name="Current building levels", value=self._buildings_field_text(building_levels), inline=False)
        embed.add_field(name="Updated", value=str(profile.get("updated_at", "-")), inline=False)
        return embed

    @app_commands.command(name="furnace_profile_set", description="Open the furnace profile popup.")
    async def _furnace_profile_set_command(self, interaction: discord.Interaction) -> None:
        log_cmd("furnace_profile_set", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await interaction.response.send_modal(
            FurnaceResourcesModal(self, interaction.user.id, self._get_profile(interaction.user.id))
        )

    async def furnace_profile_set(
        self,
        interaction: discord.Interaction,
        current_level: str,
        current_fire_crystals: int = 0,
        current_refined_fire_crystals: int = 0,
        weekly_refines: int = 0,
        preferred_package: str = "minimum",
        weekly_fire_crystals_income: int = 0,
        weekly_rfc_income: int = 0,
    ) -> None:
        """Backward-compatible callable retained for other scripts."""
        log_cmd("furnace_profile_set", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        try:
            self._create_profile_values(
                interaction.user.id,
                current_level,
                current_fire_crystals,
                current_refined_fire_crystals,
                weekly_refines,
                preferred_package,
                weekly_fire_crystals_income,
                weekly_rfc_income,
            )
            await interaction.followup.send("✅ Furnace profile saved.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(name="furnace_profile_view", description="View your saved furnace profile.")
    async def furnace_profile_view(self, interaction: discord.Interaction) -> None:
        log_cmd("furnace_profile_view", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        profile = self._get_profile(interaction.user.id)
        if not profile:
            await interaction.followup.send(
                "No saved furnace profile found. Press **Resources** to create one.",
                view=FurnaceProfileEditorView(self, interaction.user.id),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=self._build_profile_embed(profile),
            view=FurnaceProfileEditorView(self, interaction.user.id),
            ephemeral=True,
        )

    @app_commands.command(name="furnace_profile_update", description="Open the furnace profile editor popup.")
    async def _furnace_profile_update_command(self, interaction: discord.Interaction) -> None:
        log_cmd("furnace_profile_update", interaction)
        if not await self._ensure_allowed(interaction):
            return
        profile = self._get_profile(interaction.user.id)
        if not profile:
            await interaction.response.send_message(
                "No saved profile found. Use `/furnace_profile_set` first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(FurnaceResourcesModal(self, interaction.user.id, profile))

    async def furnace_profile_update(
        self,
        interaction: discord.Interaction,
        current_level: Optional[str] = None,
        current_fire_crystals: Optional[int] = None,
        current_refined_fire_crystals: Optional[int] = None,
        weekly_refines: Optional[int] = None,
        preferred_package: Optional[str] = None,
        weekly_fire_crystals_income: Optional[int] = None,
        weekly_rfc_income: Optional[int] = None,
    ) -> None:
        """Backward-compatible callable retained for other scripts."""
        log_cmd("furnace_profile_update", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        try:
            self._update_profile_values(
                interaction.user.id,
                current_level=current_level,
                current_fire_crystals=current_fire_crystals,
                current_refined_fire_crystals=current_refined_fire_crystals,
                weekly_refines=weekly_refines,
                preferred_package=preferred_package,
                weekly_fire_crystals_income=weekly_fire_crystals_income,
                weekly_rfc_income=weekly_rfc_income,
            )
            await interaction.followup.send("✅ Furnace profile updated.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(name="furnace_profile_buildings_update", description="Open the building-level popup.")
    async def _furnace_profile_buildings_update_command(self, interaction: discord.Interaction) -> None:
        log_cmd("furnace_profile_buildings_update", interaction)
        if not await self._ensure_allowed(interaction):
            return
        profile = self._get_profile(interaction.user.id)
        if not profile:
            await interaction.response.send_message(
                "No saved profile found. Use `/furnace_profile_set` first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(FurnaceCoreBuildingsModal(self, interaction.user.id, profile))

    async def furnace_profile_buildings_update(
        self,
        interaction: discord.Interaction,
        furnace: Optional[str] = None,
        embassy: Optional[str] = None,
        infantry_camp: Optional[str] = None,
        marksman_camp: Optional[str] = None,
        lancer_camp: Optional[str] = None,
        command_center: Optional[str] = None,
        infirmary: Optional[str] = None,
        war_academy: Optional[str] = None,
    ) -> None:
        """Backward-compatible callable retained for other scripts."""
        log_cmd("furnace_profile_buildings_update", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        try:
            self._update_profile_building_values(
                interaction.user.id,
                furnace=furnace,
                embassy=embassy,
                infantry_camp=infantry_camp,
                marksman_camp=marksman_camp,
                lancer_camp=lancer_camp,
                command_center=command_center,
                infirmary=infirmary,
                war_academy=war_academy,
            )
            await interaction.followup.send("✅ Furnace building levels updated.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(name="furnace_profile_clear", description="Delete your saved furnace profile.")
    async def furnace_profile_clear(self, interaction: discord.Interaction) -> None:
        log_cmd("furnace_profile_clear", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        if str(interaction.user.id) in self.profiles:
            self.profiles.pop(str(interaction.user.id), None)
            self.save_profiles()
            await interaction.followup.send("✅ Furnace profile cleared.", ephemeral=True)
        else:
            await interaction.followup.send("No saved furnace profile found.", ephemeral=True)

    # -----------------------------
    # help commands
    # -----------------------------
    @app_commands.command(name="furnace_help", description="Show the furnace calculator help sheet.")
    async def furnace_help(self, interaction: discord.Interaction) -> None:
        log_cmd("furnace_help", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        await interaction.followup.send(
            embeds=self._build_help_embeds(),
            view=FurnaceHelpView(self, interaction.user.id),
            ephemeral=True,
        )

    @app_commands.command(name="furnace_post_help", description="Post the furnace help sheet into a channel.")
    @app_commands.describe(channel="Channel to post the help sheet into")
    async def furnace_post_help(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        log_cmd("furnace_post_help", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        try:
            embeds = self._build_help_embeds()
            await channel.send(embeds=embeds)
            await interaction.followup.send(f"✅ Posted furnace help in {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ I do not have permission to post in that channel.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    # -----------------------------
    # main commands
    # -----------------------------
    @app_commands.command(name="furnace_refines_needed", description="Open the refines-needed calculator popup.")
    async def _furnace_refines_needed_command(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_allowed(interaction):
            return
        await interaction.response.send_modal(
            FurnaceRefinesNeededModal(self, interaction.user.id, self._get_profile(interaction.user.id))
        )

    async def furnace_refines_needed(
        self,
        interaction: discord.Interaction,
        target_level: str,
        target_date: str,
        current_level: Optional[str] = None,
        current_fire_crystals: Optional[int] = None,
        current_refined_fire_crystals: Optional[int] = None,
        package: Optional[str] = None,
        use_saved: bool = True,
        weekly_fire_crystals_income: Optional[int] = None,
        weekly_rfc_income: Optional[int] = None,
        *,
        _result_view: Optional[discord.ui.View] = None,
    ) -> None:
        log_cmd("furnace_refines_needed", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        try:
            parsed_date = self._parse_target_date(target_date)
            start_date = self._now_local_date()
            if parsed_date < start_date:
                raise ReferenceError("target_date cannot be before today in the configured timezone.")

            merged = self._merge_profile_defaults(
                user_id=interaction.user.id,
                use_saved=use_saved,
                current_level=current_level,
                current_fire_crystals=current_fire_crystals,
                current_refined_fire_crystals=current_refined_fire_crystals,
                package=package,
                weekly_fire_crystals_income=weekly_fire_crystals_income,
                weekly_rfc_income=weekly_rfc_income,
            )
            package_name = merged["package"]
            current_level_name = str(merged["current_level"])
            current_buildings = merged["current_buildings"]
            current_fc = self._require_non_negative("current_fire_crystals", int(merged["current_fire_crystals"]))
            current_rfc = self._require_non_negative(
                "current_refined_fire_crystals", int(merged["current_refined_fire_crystals"])
            )
            weekly_fc_income = self._require_non_negative(
                "weekly_fire_crystals_income", int(merged["weekly_fire_crystals_income"])
            )
            weekly_rfc_income_val = self._require_non_negative("weekly_rfc_income", int(merged["weekly_rfc_income"]))

            steps = self.build_upgrade_steps(current_level_name, target_level, package_name, current_buildings)
            summary = self.summarize_steps(steps)
            projected_fc_income = self._project_weekly_amount(weekly_fc_income, start_date, parsed_date)
            projected_rfc_income = self._project_weekly_amount(weekly_rfc_income_val, start_date, parsed_date)
            fc_budget_for_refines = current_fc + projected_fc_income - summary["fire_crystals"]
            current_plus_accrued_rfc = current_rfc + projected_rfc_income
            rfc_shortfall_before_refines = max(0, summary["refined_fire_crystals"] - current_plus_accrued_rfc)

            min_projection = self.find_min_weekly_refines_for_rfc(rfc_shortfall_before_refines, start_date, parsed_date, mode="minimum")
            exp_projection = self.find_min_weekly_refines_for_rfc(rfc_shortfall_before_refines, start_date, parsed_date, mode="expected")
            min_viable = min_projection.fire_crystal_spent <= fc_budget_for_refines
            exp_viable = exp_projection.fire_crystal_spent <= fc_budget_for_refines

            days_available = (parsed_date - start_date).days + 1
            weeks_left = days_available / 7.0
            weekly_rfc_needed = (rfc_shortfall_before_refines / weeks_left) if weeks_left > 0 else 0.0

            embed = self._base_embed(
                title="WoS Furnace Refines Needed",
                description=(
                    f"**Start:** {current_level_name}\n"
                    f"**Target:** {target_level}\n"
                    f"**Package:** {package_name}\n"
                    f"**Window:** {start_date.isoformat()} → {parsed_date.isoformat()} ({days_available} days, inclusive)"
                ),
            )
            embed.add_field(
                name="Upgrade Cost",
                value=(
                    f"FC required: **{self._fmt_int(summary['fire_crystals'])}**\n"
                    f"RFC required: **{self._fmt_int(summary['refined_fire_crystals'])}**"
                ),
                inline=True,
            )
            embed.add_field(
                name="Current + Accrued Before Refines",
                value=(
                    f"FC now: **{self._fmt_int(current_fc)}**\n"
                    f"RFC now: **{self._fmt_int(current_rfc)}**\n"
                    f"FC accrued: **{self._fmt_int(projected_fc_income)}**\n"
                    f"RFC accrued: **{self._fmt_int(projected_rfc_income)}**"
                ),
                inline=True,
            )
            embed.add_field(
                name="Before-Refine Position",
                value=(
                    f"FC left for refines: **{self._fmt_int(fc_budget_for_refines)}**\n"
                    f"RFC still needed: **{self._fmt_int(rfc_shortfall_before_refines)}**\n"
                    f"Weeks left: **{self._fmt_float(weeks_left)}**\n"
                    f"Weekly RFC needed: **{self._fmt_float(weekly_rfc_needed)}**"
                ),
                inline=True,
            )
            differing_buildings = [
                f"{BUILDING_KEY_TO_LABEL[key]} {current_buildings[key]}"
                for key in BUILDING_KEYS
                if current_buildings[key] != current_level_name
            ]
            if differing_buildings:
                embed.add_field(
                    name="Current building overrides",
                    value="\n".join(differing_buildings),
                    inline=False,
                )

            def build_mode_block(projection: RefineWindowProjection, viable: bool, theoretical: bool) -> str:
                produced = projection.expected_rfc if theoretical else float(projection.minimum_rfc)
                remaining_fc_after_refines = fc_budget_for_refines - projection.fire_crystal_spent
                status = "✅ Works" if viable else "❌ Not enough FC budget"
                delta_rfc = produced - rfc_shortfall_before_refines
                return (
                    f"{status}\n"
                    f"Weekly refines needed: **{self._fmt_int(projection.weekly_refines)}**\n"
                    f"Attempts in window: **{self._fmt_int(projection.total_attempts)}**\n"
                    f"FC spent on refines: **{self._fmt_int(projection.fire_crystal_spent)}**\n"
                    f"RFC from refines: **{self._fmt_float(produced) if theoretical else self._fmt_int(int(produced))}**\n"
                    f"RFC delta vs target: **{self._fmt_float(delta_rfc) if theoretical else self._fmt_int(int(delta_rfc))}**\n"
                    f"FC left after refines: **{self._fmt_int(remaining_fc_after_refines)}**\n"
                    f"Plan:\n{self._schedule_text_for_window(projection.weekly_refines, start_date, parsed_date)}"
                )

            embed.add_field(name="Guaranteed / Minimum RFC Plan", value=build_mode_block(min_projection, min_viable, theoretical=False), inline=False)
            embed.add_field(name="Expected / Theoretical RFC Plan", value=build_mode_block(exp_projection, exp_viable, theoretical=True), inline=False)

            if not min_viable or not exp_viable:
                affordable_projection = self.find_max_weekly_refines_for_fc_budget(max(0, fc_budget_for_refines), start_date, parsed_date)
                guaranteed_budget_result = self.forecast_reachable_level(
                    current_level=current_level_name,
                    available_fc=current_fc + projected_fc_income - affordable_projection.fire_crystal_spent,
                    available_rfc=current_plus_accrued_rfc + affordable_projection.minimum_rfc,
                    package_name=package_name,
                    current_buildings=current_buildings,
                )
                expected_budget_result = self.forecast_reachable_level(
                    current_level=current_level_name,
                    available_fc=current_fc + projected_fc_income - affordable_projection.fire_crystal_spent,
                    available_rfc=current_plus_accrued_rfc + math.floor(affordable_projection.expected_rfc),
                    package_name=package_name,
                    current_buildings=current_buildings,
                )
                embed.add_field(
                    name="Budget-Limited Best You Can Do",
                    value=(
                        f"Max weekly refines affordable: **{self._fmt_int(affordable_projection.weekly_refines)}**\n"
                        f"Plan:\n{self._schedule_text_for_window(affordable_projection.weekly_refines, start_date, parsed_date)}\n"
                        f"FC spent on refines: **{self._fmt_int(affordable_projection.fire_crystal_spent)}**\n"
                        f"Guaranteed RFC from refines: **{self._fmt_int(affordable_projection.minimum_rfc)}**\n"
                        f"Expected RFC from refines: **{self._fmt_float(affordable_projection.expected_rfc)}**\n"
                        f"Guaranteed reachable level: **{guaranteed_budget_result['reached_level']}**\n"
                        f"Expected reachable level: **{expected_budget_result['reached_level']}**\n"
                        f"Extra FC still needed for guaranteed target: **{self._fmt_int(max(0, min_projection.fire_crystal_spent - max(0, fc_budget_for_refines)))}** total / **{self._fmt_int(math.ceil(max(0, min_projection.fire_crystal_spent - max(0, fc_budget_for_refines)) / weeks_left) if weeks_left > 0 else 0)}** per week\n"
                        f"Extra FC still needed for expected target: **{self._fmt_int(max(0, exp_projection.fire_crystal_spent - max(0, fc_budget_for_refines)))}** total / **{self._fmt_int(math.ceil(max(0, exp_projection.fire_crystal_spent - max(0, fc_budget_for_refines)) / weeks_left) if weeks_left > 0 else 0)}** per week"
                    ),
                    inline=False,
                )

            if steps:
                lines = [
                    f"`{step['from_level']} → {step['to_level']}` — {self._step_building_summary(step)}"
                    for step in steps[:10]
                ]
                if len(steps) > 10:
                    lines.append(f"… and {len(steps) - 10} more step(s)")
                embed.add_field(name="Upgrade Path", value="\n".join(lines), inline=False)

            await interaction.followup.send(embed=embed, view=_result_view, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(name="furnace_upgrade_forecast", description="Open the upgrade forecast calculator popup.")
    async def _furnace_upgrade_forecast_command(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_allowed(interaction):
            return
        await interaction.response.send_modal(
            FurnaceUpgradeForecastModal(self, interaction.user.id, self._get_profile(interaction.user.id))
        )

    async def furnace_upgrade_forecast(
        self,
        interaction: discord.Interaction,
        target_date: str,
        weekly_refines: Optional[int] = None,
        current_level: Optional[str] = None,
        current_fire_crystals: Optional[int] = None,
        current_refined_fire_crystals: Optional[int] = None,
        package: Optional[str] = None,
        use_saved: bool = True,
        weekly_fire_crystals_income: Optional[int] = None,
        weekly_rfc_income: Optional[int] = None,
        *,
        _result_view: Optional[discord.ui.View] = None,
    ) -> None:
        log_cmd("furnace_upgrade_forecast", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        try:
            parsed_date = self._parse_target_date(target_date)
            start_date = self._now_local_date()
            if parsed_date < start_date:
                raise ReferenceError("target_date cannot be before today in the configured timezone.")

            merged = self._merge_profile_defaults(
                user_id=interaction.user.id,
                use_saved=use_saved,
                current_level=current_level,
                current_fire_crystals=current_fire_crystals,
                current_refined_fire_crystals=current_refined_fire_crystals,
                package=package,
                weekly_fire_crystals_income=weekly_fire_crystals_income,
                weekly_rfc_income=weekly_rfc_income,
            )
            profile = self._get_profile(interaction.user.id) if use_saved else {}
            if weekly_refines is None:
                weekly_refines = int(profile.get("weekly_refines", 0))
            weekly_refines = self._require_non_negative("weekly_refines", int(weekly_refines))

            package_name = merged["package"]
            current_level_name = str(merged["current_level"])
            current_buildings = merged["current_buildings"]
            current_fc = self._require_non_negative("current_fire_crystals", int(merged["current_fire_crystals"]))
            current_rfc = self._require_non_negative(
                "current_refined_fire_crystals", int(merged["current_refined_fire_crystals"])
            )
            weekly_fc_income = self._require_non_negative(
                "weekly_fire_crystals_income", int(merged["weekly_fire_crystals_income"])
            )
            weekly_rfc_income_val = self._require_non_negative("weekly_rfc_income", int(merged["weekly_rfc_income"]))

            projected_fc_income = self._project_weekly_amount(weekly_fc_income, start_date, parsed_date)
            projected_rfc_income = self._project_weekly_amount(weekly_rfc_income_val, start_date, parsed_date)
            refine_projection = self.simulate_window_refines(weekly_refines, start_date, parsed_date)

            total_fc_pool = current_fc + projected_fc_income - refine_projection.fire_crystal_spent
            guaranteed_rfc_pool = current_rfc + projected_rfc_income + refine_projection.minimum_rfc
            expected_rfc_pool = current_rfc + projected_rfc_income + math.floor(refine_projection.expected_rfc)

            guaranteed_result = self.forecast_reachable_level(
                current_level_name,
                total_fc_pool,
                guaranteed_rfc_pool,
                package_name,
                current_buildings=current_buildings,
            )
            expected_result = self.forecast_reachable_level(
                current_level_name,
                total_fc_pool,
                expected_rfc_pool,
                package_name,
                current_buildings=current_buildings,
            )

            days_available = (parsed_date - start_date).days + 1
            embed = self._base_embed(
                title="WoS Furnace Upgrade Forecast",
                description=(
                    f"**Start:** {current_level_name}\n"
                    f"**Package:** {package_name}\n"
                    f"**Window:** {start_date.isoformat()} → {parsed_date.isoformat()} ({days_available} days, inclusive)"
                ),
            )
            embed.add_field(
                name="Weekly Refine Plan",
                value=(
                    f"Weekly refines: **{self._fmt_int(weekly_refines)}**\n"
                    f"Attempts in window: **{self._fmt_int(refine_projection.total_attempts)}**\n"
                    f"Plan:\n{self._schedule_text_for_window(weekly_refines, start_date, parsed_date)}"
                ),
                inline=False,
            )
            embed.add_field(
                name="Refine Output",
                value=(
                    f"FC spent on refines: **{self._fmt_int(refine_projection.fire_crystal_spent)}**\n"
                    f"Guaranteed RFC: **{self._fmt_int(refine_projection.minimum_rfc)}**\n"
                    f"Expected / theoretical RFC: **{self._fmt_float(refine_projection.expected_rfc)}**"
                ),
                inline=False,
            )
            embed.add_field(
                name="Resources Before Upgrades",
                value=(
                    f"FC now: **{self._fmt_int(current_fc)}**\n"
                    f"RFC now: **{self._fmt_int(current_rfc)}**\n"
                    f"FC accrued: **{self._fmt_int(projected_fc_income)}**\n"
                    f"RFC accrued: **{self._fmt_int(projected_rfc_income)}**\n"
                    f"FC after refines: **{self._fmt_int(total_fc_pool)}**"
                ),
                inline=False,
            )
            differing_buildings = [
                f"{BUILDING_KEY_TO_LABEL[key]} {current_buildings[key]}"
                for key in BUILDING_KEYS
                if current_buildings[key] != current_level_name
            ]
            if differing_buildings:
                embed.add_field(name="Current building overrides", value="\n".join(differing_buildings), inline=False)
            embed.add_field(
                name="Guaranteed / Minimum Result",
                value=(
                    f"Reachable level: **{guaranteed_result['reached_level']}**\n"
                    f"Remaining FC: **{self._fmt_int(guaranteed_result['remaining_fc'])}**\n"
                    f"Remaining RFC: **{self._fmt_int(guaranteed_result['remaining_rfc'])}**"
                ),
                inline=True,
            )
            embed.add_field(
                name="Expected / Theoretical Result",
                value=(
                    f"Reachable level: **{expected_result['reached_level']}**\n"
                    f"Remaining FC: **{self._fmt_int(expected_result['remaining_fc'])}**\n"
                    f"Remaining RFC: **{self._fmt_int(expected_result['remaining_rfc'])}**"
                ),
                inline=True,
            )
            if guaranteed_result.get("next_step"):
                next_step = guaranteed_result["next_step"]
                missing_fc = max(0, next_step["fire_crystals"] - guaranteed_result["remaining_fc"])
                missing_rfc = max(0, next_step["refined_fire_crystals"] - guaranteed_result["remaining_rfc"])
                embed.add_field(
                    name="Next Guaranteed Blocker",
                    value=(
                        f"`{next_step['from_level']} → {next_step['to_level']}`\n"
                        f"Buildings: {self._step_building_summary(next_step)}\n"
                        f"Missing FC: **{self._fmt_int(missing_fc)}**\n"
                        f"Missing RFC: **{self._fmt_int(missing_rfc)}**"
                    ),
                    inline=False,
                )
            if guaranteed_result["steps_taken"]:
                lines = [
                    f"`{step['from_level']} → {step['to_level']}` — {self._step_building_summary(step)}"
                    for step in guaranteed_result["steps_taken"][:10]
                ]
                if len(guaranteed_result["steps_taken"]) > 10:
                    lines.append(f"… and {len(guaranteed_result['steps_taken']) - 10} more step(s)")
                embed.add_field(name="Guaranteed Path", value="\n".join(lines), inline=False)
            if expected_result["steps_taken"] and expected_result["steps_taken"] != guaranteed_result["steps_taken"]:
                lines = [
                    f"`{step['from_level']} → {step['to_level']}` — {self._step_building_summary(step)}"
                    for step in expected_result["steps_taken"][:10]
                ]
                if len(expected_result["steps_taken"]) > 10:
                    lines.append(f"… and {len(expected_result['steps_taken']) - 10} more step(s)")
                embed.add_field(name="Expected Path", value="\n".join(lines), inline=False)
            await interaction.followup.send(embed=embed, view=_result_view, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(name="furnace_reference_check", description="Show loaded furnace reference metadata.")
    async def furnace_reference_check(self, interaction: discord.Interaction) -> None:
        log_cmd("furnace_reference_check", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        try:
            package_names: List[str] = []
            for entry in self.upgrades["levels"]:
                if entry.get("packages"):
                    package_names = list(entry["packages"].keys())
                    break
            tier_lines = [
                f"{tier['name']}: attempts {tier['min_attempt']}-{tier['max_attempt']} | FC/refine {tier['fire_crystal_cost']}"
                for tier in self.refines["tiers"][:10]
            ]
            embed = self._base_embed(title="WoS Furnace Reference Check")
            embed.add_field(name="Levels loaded", value=str(len(self.upgrades["levels"])), inline=True)
            embed.add_field(name="Packages", value=", ".join(package_names) if package_names else "None", inline=True)
            embed.add_field(name="Refine tiers", value=str(len(self.refines["tiers"])), inline=True)
            embed.add_field(
                name="Level range",
                value=f"{self.upgrades['levels'][0]['level']} → {self.upgrades['levels'][-1]['level']}",
                inline=False,
            )
            embed.add_field(name="Refine tiers detail", value="\n".join(tier_lines), inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(name="furnace_reference_reload", description="Reload the furnace JSON references.")
    async def furnace_reference_reload(self, interaction: discord.Interaction) -> None:
        log_cmd("furnace_reference_reload", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        try:
            self.load_reference_files()
            self.load_profiles()
            await interaction.followup.send(
                f"✅ Reloaded `{UPGRADES_PATH.name}`, `{REFINES_PATH.name}`, and `{PROFILES_PATH.name}` successfully.",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Reload failed: {exc}", ephemeral=True)

    # -----------------------------


class FurnaceOwnedView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: float = 900.0):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "❌ Open your own furnace calculator so your saved details are used.",
            ephemeral=True,
        )
        return False


class FurnaceProfileEditorView(FurnaceOwnedView):
    def __init__(self, cog: WOSFurnaceCalculator, owner_id: int):
        super().__init__(owner_id)
        self.cog = cog

    @discord.ui.button(label="Resources", style=discord.ButtonStyle.primary, emoji="💎")
    async def resources(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            FurnaceResourcesModal(self.cog, self.owner_id, self.cog._get_profile(self.owner_id))
        )

    @discord.ui.button(label="Weekly plan", style=discord.ButtonStyle.secondary, emoji="📅")
    async def weekly_plan(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            FurnacePlanModal(self.cog, self.owner_id, self.cog._get_profile(self.owner_id))
        )

    @discord.ui.button(label="Core buildings", style=discord.ButtonStyle.secondary, emoji="🏛️")
    async def core_buildings(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        profile = self.cog._get_profile(self.owner_id)
        if not profile:
            await interaction.response.send_message("Save Resources first.", ephemeral=True)
            return
        await interaction.response.send_modal(FurnaceCoreBuildingsModal(self.cog, self.owner_id, profile))

    @discord.ui.button(label="Camps / academy", style=discord.ButtonStyle.secondary, emoji="🏕️")
    async def camps(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        profile = self.cog._get_profile(self.owner_id)
        if not profile:
            await interaction.response.send_message("Save Resources first.", ephemeral=True)
            return
        await interaction.response.send_modal(FurnaceCampBuildingsModal(self.cog, self.owner_id, profile))


class FurnaceHelpView(FurnaceOwnedView):
    def __init__(self, cog: WOSFurnaceCalculator, owner_id: int):
        super().__init__(owner_id)
        self.cog = cog

    @discord.ui.button(label="Edit profile", style=discord.ButtonStyle.secondary, emoji="⚙️")
    async def edit_profile(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            FurnaceResourcesModal(self.cog, self.owner_id, self.cog._get_profile(self.owner_id))
        )

    @discord.ui.button(label="Refines needed", style=discord.ButtonStyle.primary, emoji="🎯")
    async def refines_needed(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            FurnaceRefinesNeededModal(self.cog, self.owner_id, self.cog._get_profile(self.owner_id))
        )

    @discord.ui.button(label="Upgrade forecast", style=discord.ButtonStyle.success, emoji="📈")
    async def upgrade_forecast(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            FurnaceUpgradeForecastModal(self.cog, self.owner_id, self.cog._get_profile(self.owner_id))
        )


class FurnaceResourcesModal(discord.ui.Modal):
    def __init__(self, cog: WOSFurnaceCalculator, owner_id: int, profile: Optional[Dict[str, Any]] = None):
        super().__init__(title="Furnace profile — resources")
        self.cog = cog
        self.owner_id = owner_id
        profile = profile or {}
        self.current_level = discord.ui.TextInput(
            label="Current furnace level",
            placeholder="FC5",
            default=str(profile.get("current_level", "")).strip() or None,
            max_length=10,
        )
        self.current_fc = discord.ui.TextInput(
            label="Current Fire Crystals",
            placeholder="2622",
            default=str(int(profile.get("fire_crystals", 0))),
            max_length=20,
        )
        self.current_rfc = discord.ui.TextInput(
            label="Current Refined Fire Crystals",
            placeholder="118",
            default=str(int(profile.get("refined_fire_crystals", 0))),
            max_length=20,
        )
        self.weekly_fc = discord.ui.TextInput(
            label="Fire Crystals gained per week",
            placeholder="0",
            default=str(int(profile.get("weekly_fire_crystals_income", 0))),
            max_length=20,
        )
        self.weekly_rfc = discord.ui.TextInput(
            label="Refined Crystals gained per week",
            placeholder="0",
            default=str(int(profile.get("weekly_refined_fire_crystals_income", 0))),
            max_length=20,
        )
        for item in (self.current_level, self.current_fc, self.current_rfc, self.weekly_fc, self.weekly_rfc):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            current_level = self.cog._canonical_level(str(self.current_level.value))
            current_fc = self.cog._parse_int_text("Current Fire Crystals", str(self.current_fc.value))
            current_rfc = self.cog._parse_int_text("Current Refined Fire Crystals", str(self.current_rfc.value))
            weekly_fc = self.cog._parse_int_text("Weekly Fire Crystal income", str(self.weekly_fc.value))
            weekly_rfc = self.cog._parse_int_text("Weekly Refined Fire Crystal income", str(self.weekly_rfc.value))
            existing = self.cog._get_profile(self.owner_id)
            if existing:
                profile = self.cog._update_profile_values(
                    self.owner_id,
                    current_level=current_level,
                    current_fire_crystals=current_fc,
                    current_refined_fire_crystals=current_rfc,
                    weekly_fire_crystals_income=weekly_fc,
                    weekly_rfc_income=weekly_rfc,
                )
            else:
                profile = self.cog._create_profile_values(
                    self.owner_id,
                    current_level=current_level,
                    current_fire_crystals=current_fc,
                    current_refined_fire_crystals=current_rfc,
                    weekly_fire_crystals_income=weekly_fc,
                    weekly_rfc_income=weekly_rfc,
                )
            await interaction.response.send_message(
                embed=self.cog._build_profile_embed(profile),
                view=FurnaceProfileEditorView(self.cog, self.owner_id),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


class FurnacePlanModal(discord.ui.Modal):
    def __init__(self, cog: WOSFurnaceCalculator, owner_id: int, profile: Optional[Dict[str, Any]] = None):
        super().__init__(title="Furnace profile — weekly plan")
        self.cog = cog
        self.owner_id = owner_id
        profile = profile or {}
        self.weekly_refines = discord.ui.TextInput(
            label="Planned refines per week",
            placeholder="60",
            default=str(int(profile.get("weekly_refines", 0))),
            max_length=20,
        )
        self.package = discord.ui.TextInput(
            label="Default upgrade package",
            placeholder="minimum / all_camps / full_furnace",
            default=str(profile.get("preferred_package", "minimum")),
            max_length=30,
        )
        self.add_item(self.weekly_refines)
        self.add_item(self.package)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            if not self.cog._get_profile(self.owner_id):
                raise ReferenceError("Save the Resources section first.")
            profile = self.cog._update_profile_values(
                self.owner_id,
                weekly_refines=self.cog._parse_int_text("Weekly refines", str(self.weekly_refines.value)),
                preferred_package=self.cog._canonical_package(str(self.package.value)),
            )
            await interaction.response.send_message(
                embed=self.cog._build_profile_embed(profile),
                view=FurnaceProfileEditorView(self.cog, self.owner_id),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


class FurnaceCoreBuildingsModal(discord.ui.Modal):
    def __init__(self, cog: WOSFurnaceCalculator, owner_id: int, profile: Dict[str, Any]):
        super().__init__(title="Furnace profile — core buildings")
        self.cog = cog
        self.owner_id = owner_id
        base = str(profile.get("current_level", "FC1"))
        buildings = self.cog._normalize_building_levels(
            base,
            profile.get("current_buildings") if isinstance(profile.get("current_buildings"), dict) else None,
        )
        self.furnace = discord.ui.TextInput(label="Furnace", default=buildings["furnace"], max_length=10)
        self.embassy = discord.ui.TextInput(label="Embassy", default=buildings["embassy"], max_length=10)
        self.command_center = discord.ui.TextInput(label="Command Center", default=buildings["command_center"], max_length=10)
        self.infirmary = discord.ui.TextInput(label="Infirmary", default=buildings["infirmary"], max_length=10)
        for item in (self.furnace, self.embassy, self.command_center, self.infirmary):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            profile = self.cog._update_profile_building_values(
                self.owner_id,
                furnace=str(self.furnace.value),
                embassy=str(self.embassy.value),
                command_center=str(self.command_center.value),
                infirmary=str(self.infirmary.value),
            )
            await interaction.response.send_message(
                embed=self.cog._build_profile_embed(profile),
                view=FurnaceProfileEditorView(self.cog, self.owner_id),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


class FurnaceCampBuildingsModal(discord.ui.Modal):
    def __init__(self, cog: WOSFurnaceCalculator, owner_id: int, profile: Dict[str, Any]):
        super().__init__(title="Furnace profile — camps / academy")
        self.cog = cog
        self.owner_id = owner_id
        base = str(profile.get("current_level", "FC1"))
        buildings = self.cog._normalize_building_levels(
            base,
            profile.get("current_buildings") if isinstance(profile.get("current_buildings"), dict) else None,
        )
        self.infantry = discord.ui.TextInput(label="Infantry Camp", default=buildings["infantry_camp"], max_length=10)
        self.marksman = discord.ui.TextInput(label="Marksman Camp", default=buildings["marksman_camp"], max_length=10)
        self.lancer = discord.ui.TextInput(label="Lancer Camp", default=buildings["lancer_camp"], max_length=10)
        self.war_academy = discord.ui.TextInput(label="War Academy", default=buildings["war_academy"], max_length=10)
        for item in (self.infantry, self.marksman, self.lancer, self.war_academy):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            profile = self.cog._update_profile_building_values(
                self.owner_id,
                infantry_camp=str(self.infantry.value),
                marksman_camp=str(self.marksman.value),
                lancer_camp=str(self.lancer.value),
                war_academy=str(self.war_academy.value),
            )
            await interaction.response.send_message(
                embed=self.cog._build_profile_embed(profile),
                view=FurnaceProfileEditorView(self.cog, self.owner_id),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


class FurnaceCalculationResultView(FurnaceOwnedView):
    def __init__(self, cog: WOSFurnaceCalculator, owner_id: int, mode: str, inputs: Dict[str, Any]):
        super().__init__(owner_id)
        self.cog = cog
        self.mode = mode
        self.inputs = dict(inputs)

    @discord.ui.button(label="Edit & recalculate", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        profile = self.cog._get_profile(self.owner_id)
        if self.mode == "refines":
            modal: discord.ui.Modal = FurnaceRefinesNeededModal(
                self.cog, self.owner_id, profile, initial=self.inputs
            )
        else:
            modal = FurnaceUpgradeForecastModal(
                self.cog, self.owner_id, profile, initial=self.inputs
            )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Save inputs", style=discord.ButtonStyle.success, emoji="💾")
    async def save_inputs(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            existing = self.cog._get_profile(self.owner_id)
            weekly_refines = self.inputs.get("weekly_refines")
            if existing:
                profile = self.cog._update_profile_values(
                    self.owner_id,
                    current_level=str(self.inputs["current_level"]),
                    current_fire_crystals=int(self.inputs["current_fire_crystals"]),
                    current_refined_fire_crystals=int(self.inputs["current_refined_fire_crystals"]),
                    weekly_refines=int(weekly_refines) if weekly_refines is not None else None,
                    preferred_package=str(self.inputs["package"]),
                    weekly_fire_crystals_income=int(self.inputs["weekly_fire_crystals_income"]),
                    weekly_rfc_income=int(self.inputs["weekly_rfc_income"]),
                )
            else:
                profile = self.cog._create_profile_values(
                    self.owner_id,
                    current_level=str(self.inputs["current_level"]),
                    current_fire_crystals=int(self.inputs["current_fire_crystals"]),
                    current_refined_fire_crystals=int(self.inputs["current_refined_fire_crystals"]),
                    weekly_refines=int(weekly_refines or 0),
                    preferred_package=str(self.inputs["package"]),
                    weekly_fire_crystals_income=int(self.inputs["weekly_fire_crystals_income"]),
                    weekly_rfc_income=int(self.inputs["weekly_rfc_income"]),
                )
            await interaction.response.send_message(
                "✅ These calculator inputs are now your saved defaults.",
                embed=self.cog._build_profile_embed(profile),
                view=FurnaceProfileEditorView(self.cog, self.owner_id),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @discord.ui.button(label="Profile editor", style=discord.ButtonStyle.secondary, emoji="⚙️")
    async def profile_editor(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            FurnaceResourcesModal(self.cog, self.owner_id, self.cog._get_profile(self.owner_id))
        )


class FurnaceRefinesNeededModal(discord.ui.Modal):
    def __init__(
        self,
        cog: WOSFurnaceCalculator,
        owner_id: int,
        profile: Optional[Dict[str, Any]] = None,
        *,
        initial: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(title="Furnace refines needed")
        self.cog = cog
        self.owner_id = owner_id
        profile = profile or {}
        initial = initial or {}
        current_level = str(initial.get("current_level") or profile.get("current_level") or "")
        target_level = str(initial.get("target_level") or "")
        levels_default = f"{current_level} -> {target_level}" if current_level and target_level else None
        target_date_default = str(initial.get("target_date") or (self.cog._now_local_date() + timedelta(days=30)).isoformat())
        current_fc = int(initial.get("current_fire_crystals", profile.get("fire_crystals", 0)))
        current_rfc = int(initial.get("current_refined_fire_crystals", profile.get("refined_fire_crystals", 0)))
        weekly_fc = int(initial.get("weekly_fire_crystals_income", profile.get("weekly_fire_crystals_income", 0)))
        weekly_rfc = int(initial.get("weekly_rfc_income", profile.get("weekly_refined_fire_crystals_income", 0)))
        package = str(initial.get("package") or profile.get("preferred_package", "minimum"))
        self.levels = discord.ui.TextInput(
            label="Current -> target level", placeholder="FC5 -> FC10", default=levels_default, max_length=30
        )
        self.target_date = discord.ui.TextInput(
            label="Target date", placeholder="YYYY-MM-DD", default=target_date_default, max_length=20
        )
        self.resources = discord.ui.TextInput(
            label="Resources now — FC / RFC", placeholder="2622 / 118", default=f"{current_fc} / {current_rfc}", max_length=50
        )
        self.weekly_income = discord.ui.TextInput(
            label="Weekly income — FC / RFC", placeholder="500 / 20", default=f"{weekly_fc} / {weekly_rfc}", max_length=50
        )
        self.package = discord.ui.TextInput(
            label="Upgrade package", placeholder="minimum / all_camps / full_furnace", default=package, max_length=30
        )
        for item in (self.levels, self.target_date, self.resources, self.weekly_income, self.package):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            current_level, target_level = self.cog._parse_level_pair(str(self.levels.value))
            current_fc, current_rfc = self.cog._parse_number_pair("Resources now", str(self.resources.value))
            weekly_fc, weekly_rfc = self.cog._parse_number_pair("Weekly income", str(self.weekly_income.value))
            package = self.cog._canonical_package(str(self.package.value))
            parsed_date = self.cog._parse_target_date(str(self.target_date.value))
            inputs = {
                "current_level": current_level,
                "target_level": target_level,
                "target_date": parsed_date.isoformat(),
                "current_fire_crystals": current_fc,
                "current_refined_fire_crystals": current_rfc,
                "package": package,
                "weekly_fire_crystals_income": weekly_fc,
                "weekly_rfc_income": weekly_rfc,
            }
            await self.cog.furnace_refines_needed(
                interaction,
                target_level=target_level,
                target_date=parsed_date.isoformat(),
                current_level=current_level,
                current_fire_crystals=current_fc,
                current_refined_fire_crystals=current_rfc,
                package=package,
                use_saved=True,
                weekly_fire_crystals_income=weekly_fc,
                weekly_rfc_income=weekly_rfc,
                _result_view=FurnaceCalculationResultView(self.cog, self.owner_id, "refines", inputs),
            )
        except Exception as exc:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


class FurnaceUpgradeForecastModal(discord.ui.Modal):
    def __init__(
        self,
        cog: WOSFurnaceCalculator,
        owner_id: int,
        profile: Optional[Dict[str, Any]] = None,
        *,
        initial: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(title="Furnace upgrade forecast")
        self.cog = cog
        self.owner_id = owner_id
        profile = profile or {}
        initial = initial or {}
        current_level = str(initial.get("current_level") or profile.get("current_level") or "")
        package = str(initial.get("package") or profile.get("preferred_package", "minimum"))
        level_package_default = f"{current_level} / {package}" if current_level else None
        target_date_default = str(initial.get("target_date") or (self.cog._now_local_date() + timedelta(days=30)).isoformat())
        weekly_refines = int(initial.get("weekly_refines", profile.get("weekly_refines", 0)))
        current_fc = int(initial.get("current_fire_crystals", profile.get("fire_crystals", 0)))
        current_rfc = int(initial.get("current_refined_fire_crystals", profile.get("refined_fire_crystals", 0)))
        weekly_fc = int(initial.get("weekly_fire_crystals_income", profile.get("weekly_fire_crystals_income", 0)))
        weekly_rfc = int(initial.get("weekly_rfc_income", profile.get("weekly_refined_fire_crystals_income", 0)))
        self.level_package = discord.ui.TextInput(
            label="Current level / package", placeholder="FC5 / minimum", default=level_package_default, max_length=40
        )
        self.target_date = discord.ui.TextInput(
            label="Target date", placeholder="YYYY-MM-DD", default=target_date_default, max_length=20
        )
        self.weekly_refines = discord.ui.TextInput(
            label="Refines per week", placeholder="60", default=str(weekly_refines), max_length=20
        )
        self.resources = discord.ui.TextInput(
            label="Resources now — FC / RFC", placeholder="2622 / 118", default=f"{current_fc} / {current_rfc}", max_length=50
        )
        self.weekly_income = discord.ui.TextInput(
            label="Weekly income — FC / RFC", placeholder="500 / 20", default=f"{weekly_fc} / {weekly_rfc}", max_length=50
        )
        for item in (self.level_package, self.target_date, self.weekly_refines, self.resources, self.weekly_income):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            current_level, package = self.cog._parse_level_package(str(self.level_package.value))
            weekly_refines = self.cog._parse_int_text("Weekly refines", str(self.weekly_refines.value))
            current_fc, current_rfc = self.cog._parse_number_pair("Resources now", str(self.resources.value))
            weekly_fc, weekly_rfc = self.cog._parse_number_pair("Weekly income", str(self.weekly_income.value))
            parsed_date = self.cog._parse_target_date(str(self.target_date.value))
            inputs = {
                "current_level": current_level,
                "target_date": parsed_date.isoformat(),
                "weekly_refines": weekly_refines,
                "current_fire_crystals": current_fc,
                "current_refined_fire_crystals": current_rfc,
                "package": package,
                "weekly_fire_crystals_income": weekly_fc,
                "weekly_rfc_income": weekly_rfc,
            }
            await self.cog.furnace_upgrade_forecast(
                interaction,
                target_date=parsed_date.isoformat(),
                weekly_refines=weekly_refines,
                current_level=current_level,
                current_fire_crystals=current_fc,
                current_refined_fire_crystals=current_rfc,
                package=package,
                use_saved=True,
                weekly_fire_crystals_income=weekly_fc,
                weekly_rfc_income=weekly_rfc,
                _result_view=FurnaceCalculationResultView(self.cog, self.owner_id, "forecast", inputs),
            )
        except Exception as exc:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)
    cog = WOSFurnaceCalculator(bot)
    guild_obj = discord.Object(id=bot.hot_config["guild_id"])
    for cmd in cog.get_app_commands():
        cmd._guild_ids = {bot.hot_config["guild_id"]}
        cmd.guilds = (guild_obj,)
    await bot.add_cog(cog)
