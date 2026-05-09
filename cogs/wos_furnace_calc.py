from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
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


BUILDING_ORDER = [
    "furnace",
    "embassy",
    "command_center",
    "infirmary",
    "infantry_camp",
    "marksman_camp",
    "lancer_camp",
    "war_academy",
]
BUILDING_DISPLAY_NAMES: Dict[str, str] = {
    "furnace": "Furnace",
    "embassy": "Embassy",
    "command_center": "Command Center",
    "infirmary": "Infirmary",
    "infantry_camp": "Infantry Camp",
    "marksman_camp": "Marksman Camp",
    "lancer_camp": "Lancer Camp",
    "war_academy": "War Academy",
}
DISPLAY_NAME_TO_BUILDING_KEY: Dict[str, str] = {
    value.casefold(): key for key, value in BUILDING_DISPLAY_NAMES.items()
}


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


def _building_req(label: str, fc: int, rfc: int) -> Dict[str, Any]:
    return {
        "building": label,
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
            _building_req("Furnace", costs["furnace"]["fc"], costs["furnace"]["rfc"]),
            _building_req("Embassy", costs["embassy"]["fc"], costs["embassy"]["rfc"]),
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
            )
        )

        all_camps_requirements = [
            _building_req("Furnace", costs["furnace"]["fc"], costs["furnace"]["rfc"]),
            _building_req("Embassy", costs["embassy"]["fc"], costs["embassy"]["rfc"]),
            _building_req("Infantry Camp", costs["infantry_camp"]["fc"], costs["infantry_camp"]["rfc"]),
            _building_req("Marksman Camp", costs["marksman_camp"]["fc"], costs["marksman_camp"]["rfc"]),
            _building_req("Lancer Camp", costs["lancer_camp"]["fc"], costs["lancer_camp"]["rfc"]),
        ]

        full_furnace_requirements = list(all_camps_requirements)
        full_furnace_requirements.extend(
            [
                _building_req("Command Center", costs["command_center"]["fc"], costs["command_center"]["rfc"]),
                _building_req("Infirmary", costs["infirmary"]["fc"], costs["infirmary"]["rfc"]),
            ]
        )
        if costs["war_academy"]["fc"] > 0 or costs["war_academy"]["rfc"] > 0:
            full_furnace_requirements.append(
                _building_req("War Academy", costs["war_academy"]["fc"], costs["war_academy"]["rfc"])
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
    forge = app_commands.Group(name="forge", description="Furnace maintenance tools")

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

    async def _ensure_tech_role(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            msg = "❌ This command can only be used inside a server."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return False

        has_tech = any(role.name == "Tech" for role in member.roles)
        if has_tech:
            return True

        msg = "❌ This command is only available to the Tech role."
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

    def _get_profile_buildings(self, user_id: int, fallback_level: str) -> Dict[str, str]:
        profile = self._get_profile(user_id)
        saved = profile.get("current_buildings") if isinstance(profile, dict) else {}
        buildings: Dict[str, str] = {}
        for key in BUILDING_ORDER:
            value = saved.get(key) if isinstance(saved, dict) else None
            buildings[key] = str(value or fallback_level)
        return buildings

    def _default_buildings_for_level(self, level_name: str) -> Dict[str, str]:
        return {key: level_name for key in BUILDING_ORDER}

    def _normalize_building_key(self, building_name: str) -> str:
        key = DISPLAY_NAME_TO_BUILDING_KEY.get(str(building_name).strip().casefold())
        if key:
            return key
        raise ReferenceError(f"Unknown building '{building_name}'.")

    @staticmethod
    def _level_number(level_name: str) -> int:
        value = str(level_name).strip().casefold()
        if not value.startswith("fc"):
            raise ReferenceError(f"Invalid level '{level_name}'.")
        try:
            return int(value[2:])
        except ValueError as exc:
            raise ReferenceError(f"Invalid level '{level_name}'.") from exc

    @staticmethod
    def _level_name(level_number: int) -> str:
        return f"FC{int(level_number)}"

    def _cost_to_raise_building(self, building_key: str, current_level: str, target_level: str) -> Dict[str, int]:
        current_num = self._level_number(current_level)
        target_num = self._level_number(target_level)
        if current_num >= target_num:
            return {"fc": 0, "rfc": 0}
        total_fc = 0
        total_rfc = 0
        for level_num in range(current_num + 1, target_num + 1):
            level_name = self._level_name(level_num)
            costs = DEFAULT_BUILDING_COSTS_BY_TARGET.get(level_name)
            if not costs or building_key not in costs:
                raise ReferenceError(f"Missing building cost for {building_key} at {level_name}.")
            total_fc += int(costs[building_key]["fc"])
            total_rfc += int(costs[building_key]["rfc"])
        return {"fc": total_fc, "rfc": total_rfc}

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
        merged = {
            "current_level": current_level if current_level is not None else profile.get("current_level"),
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
        if not merged["current_level"]:
            raise ReferenceError("current_level is required. Set a profile or pass it in the command.")
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


    def _format_weekly_schedule(self, weekly_refines: int) -> str:
        counts = self._weekly_day_counts(weekly_refines, 7)
        labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return " | ".join(f"{labels[i]} {counts[i]}" for i in range(7))

    def _segment_plan(self, weekly_refines: int, segment_start: date, segment_days: int) -> str:
        if segment_days <= 0:
            return "None"
        segment_attempts = math.floor((weekly_refines * segment_days) / 7)
        counts = self._weekly_day_counts(segment_attempts, segment_days)
        labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        start_idx = segment_start.weekday()
        return " | ".join(
            f"{labels[start_idx + i]} {counts[i]}"
            for i in range(segment_days)
        )

    def _current_and_weekly_plan_text(self, weekly_refines: int, start_date: date, target_date: date) -> str:
        if target_date < start_date:
            return "No refines needed."
        total_days = (target_date - start_date).days + 1
        current_segment_days = min(7 - start_date.weekday(), total_days)
        current_week = self._segment_plan(weekly_refines, start_date, current_segment_days)
        if current_segment_days >= total_days:
            return f"This week only: {current_week}"

        lines = [f"This week: {current_week}", f"From next Monday: {self._format_weekly_schedule(weekly_refines)}"]

        # Final partial week, if the target ends mid-week after at least one Monday reset.
        next_monday = start_date + timedelta(days=(7 - start_date.weekday()))
        if target_date >= next_monday and target_date.weekday() != 6:
            final_segment_days = target_date.weekday() + 1
            final_segment_start = target_date - timedelta(days=final_segment_days - 1)
            lines.append(f"Final week: {self._segment_plan(weekly_refines, final_segment_start, final_segment_days)}")
        return "\n".join(lines)

    def _window_segments(self, start_date: date, target_date: date) -> List[int]:
        if target_date < start_date:
            return []
        segments: List[int] = []
        cursor = start_date
        while cursor <= target_date:
            days_until_sunday = 6 - cursor.weekday()
            segment_end = min(target_date, cursor + timedelta(days=days_until_sunday))
            segments.append((segment_end - cursor).days + 1)
            cursor = segment_end + timedelta(days=1)
        return segments

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

        for segment_days in self._window_segments(start_date, target_date):
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


    def resolve_package(
        self,
        level_entry: Dict[str, Any],
        package_name: str,
        current_buildings: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
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
        target_level = str(level_entry["next_level"])
        building_levels = dict(current_buildings or self._default_buildings_for_level(level_entry["level"]))
        total_fc = 0
        total_rfc = 0
        selected_buildings: List[Dict[str, Any]] = []

        for req in package.get("requirements", []):
            building_name = str(req.get("building", "Unknown"))
            building_key = self._normalize_building_key(building_name)
            current_building_level = str(building_levels.get(building_key, level_entry["level"]))
            upgrade_cost = self._cost_to_raise_building(building_key, current_building_level, target_level)
            selected_buildings.append(
                {
                    "building": building_name,
                    "building_key": building_key,
                    "from_level": current_building_level,
                    "to_level": target_level,
                    "fire_crystals": int(upgrade_cost["fc"]),
                    "refined_fire_crystals": int(upgrade_cost["rfc"]),
                }
            )
            total_fc += int(upgrade_cost["fc"])
            total_rfc += int(upgrade_cost["rfc"])

        return {
            "package_name": actual_package_name,
            "description": package.get("description", ""),
            "fire_crystals": total_fc,
            "refined_fire_crystals": total_rfc,
            "selected_buildings": selected_buildings,
        }

    def build_upgrade_steps(
        self,
        current_level: str,
        target_level: str,
        package_name: str,
        current_buildings: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        current_entry = self._get_level_entry(current_level)
        target_key = self._normalize_level_name(target_level)
        building_levels = dict(current_buildings or self._default_buildings_for_level(current_level))

        # Catch-up case: furnace is already at the target level, but support buildings are not.
        # Example: furnace FC10 with command_center FC9 / infirmary FC8 / war_academy FC4.
        if self._normalize_level_name(current_entry["level"]) == target_key:
            target_num = self._level_number(target_level)
            if target_num <= 1:
                return []
            previous_level_name = self._level_name(target_num - 1)
            previous_entry = self._get_level_entry(previous_level_name)
            resolved = self.resolve_package(previous_entry, package_name, building_levels)
            if resolved["fire_crystals"] <= 0 and resolved["refined_fire_crystals"] <= 0:
                return []
            return [{
                "from_level": current_entry["level"],
                "to_level": target_level,
                **resolved,
            }]

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
            resolved = self.resolve_package(level_entry, package_name, building_levels)
            step = {
                "from_level": level_entry["level"],
                "to_level": next_level,
                **resolved,
            }
            steps.append(step)
            for building in resolved["selected_buildings"]:
                building_levels[building["building_key"]] = next_level
            level_name = next_level
        return steps

    def summarize_steps(self, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "fire_crystals": sum(step["fire_crystals"] for step in steps),
            "refined_fire_crystals": sum(step["refined_fire_crystals"] for step in steps),
            "steps": steps,
        }

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
        building_levels = dict(current_buildings or self._default_buildings_for_level(current_level))
        while True:
            level_entry = self._get_level_entry(cursor_level)
            next_level = level_entry.get("next_level")
            if not next_level:
                break
            resolved = self.resolve_package(level_entry, package_name, building_levels)
            if remaining_fc < resolved["fire_crystals"] or remaining_rfc < resolved["refined_fire_crystals"]:
                break
            remaining_fc -= resolved["fire_crystals"]
            remaining_rfc -= resolved["refined_fire_crystals"]
            step = {
                "from_level": level_entry["level"],
                "to_level": next_level,
                **resolved,
            }
            steps_taken.append(step)
            for building in resolved["selected_buildings"]:
                building_levels[building["building_key"]] = next_level
            cursor_level = next_level
        next_step = None
        level_entry = self._get_level_entry(cursor_level)
        if level_entry.get("next_level"):
            next_step = {
                "from_level": level_entry["level"],
                "to_level": level_entry["next_level"],
                **self.resolve_package(level_entry, package_name, building_levels),
            }
        return {
            "reached_level": cursor_level,
            "remaining_fc": remaining_fc,
            "remaining_rfc": remaining_rfc,
            "steps_taken": steps_taken,
            "next_step": next_step,
        }

    def _step_building_summary(self, step: Dict[str, Any]) -> str:
        if not step.get("selected_buildings"):
            return "No buildings"
        return ", ".join(
            f"{building['building']} {building.get('from_level', '?')}→{building.get('to_level', '?')}"
            for building in step["selected_buildings"]
        )

    # -----------------------------
    # help embeds
    # -----------------------------
    def _build_help_embeds(self) -> List[discord.Embed]:
        overview = self._base_embed(
            "WoS Furnace Calculator Help",
            "Still in development. Use with care and double-check important upgrade plans.",
        )
        overview.add_field(
            name="Quick setup",
            value=(
                "1. `/furnace_set ...` to save your resources and current building levels\n"
                "2. `/furnace_view` to check what is saved\n"
                "3. `/furnace_refines_needed` to see what RFC/refines you need by a date\n"
                "4. `/furnace_upgrade_forecast` to test a weekly refine plan"
            ),
            inline=False,
        )
        overview.add_field(
            name="Main commands",
            value=(
                "`/furnace_set` = create or update your saved profile in one command\n"
                "`/furnace_view` = check your saved profile\n"
                "`/furnace_refines_needed` = bot works out the missing RFC/refines by a date\n"
                "`/furnace_upgrade_forecast` = you enter weekly refines and it tells you what level you can reach"
            ),
            inline=False,
        )
        overview.add_field(
            name="Packages",
            value=(
                "`minimum` = Furnace + Embassy + required troop camp\n"
                "`all_camps` = Furnace + Embassy + all three troop camps\n"
                "`full_furnace` = Full package including support buildings"
            ),
            inline=False,
        )

        details = self._base_embed("WoS Furnace Calculator Notes")
        details.add_field(
            name="Refine maths",
            value=(
                "- Tiers reset each Monday in UTC\n"
                "- First refine of each day is 50% off\n"
                "- Output shows the current partial week plan and the Monday-reset weekly plan\n"
                "- Output shows both guaranteed/minimum RFC and expected/theoretical RFC"
            ),
            inline=False,
        )
        details.add_field(
            name="Building levels",
            value=(
                "You can save the actual level of each building in `/furnace_set`. "
                "If your buildings are uneven, the bot only charges the missing upgrades from where you actually are."
            ),
            inline=False,
        )
        details.add_field(
            name="Example",
            value=(
                "`/furnace_set current_level:FC5 current_fire_crystals:2622 current_refined_fire_crystals:118 furnace:FC5 embassy:FC4 infantry_camp:FC2 marksman_camp:FC3 lancer_camp:FC4 command_center:FC2 infirmary:FC1 war_academy:FC4`\n"
                "`/furnace_refines_needed target_level:FC6 target_date:2026-05-17 package:full_furnace use_saved:true`"
            ),
            inline=False,
        )
        return [overview, details]

    # -----------------------------
    # profile commands
    # -----------------------------
    @app_commands.command(name="furnace_set", description="Create or update your saved furnace profile.")
    @app_commands.choices(current_level=LEVEL_CHOICES, preferred_package=PACKAGE_CHOICES, furnace=LEVEL_CHOICES, embassy=LEVEL_CHOICES, command_center=LEVEL_CHOICES, infirmary=LEVEL_CHOICES, infantry_camp=LEVEL_CHOICES, marksman_camp=LEVEL_CHOICES, lancer_camp=LEVEL_CHOICES, war_academy=LEVEL_CHOICES)
    @app_commands.describe(
        current_level="Your main current furnace level",
        current_fire_crystals="Current Fire Crystals",
        current_refined_fire_crystals="Current Refined Fire Crystals",
        weekly_refines="Planned refines per week",
        preferred_package="Default package name",
        weekly_fire_crystals_income="Fire Crystal gain per week",
        weekly_rfc_income="Refined Fire Crystal gain per week",
        furnace="Actual Furnace level if different",
        embassy="Actual Embassy level if different",
        command_center="Actual Command Center level if different",
        infirmary="Actual Infirmary level if different",
        infantry_camp="Actual Infantry Camp level if different",
        marksman_camp="Actual Marksman Camp level if different",
        lancer_camp="Actual Lancer Camp level if different",
        war_academy="Actual War Academy level if different",
    )
    async def furnace_set(
        self,
        interaction: discord.Interaction,
        current_level: Optional[str] = None,
        current_fire_crystals: Optional[int] = None,
        current_refined_fire_crystals: Optional[int] = None,
        weekly_refines: Optional[int] = None,
        preferred_package: Optional[str] = None,
        weekly_fire_crystals_income: Optional[int] = None,
        weekly_rfc_income: Optional[int] = None,
        furnace: Optional[str] = None,
        embassy: Optional[str] = None,
        command_center: Optional[str] = None,
        infirmary: Optional[str] = None,
        infantry_camp: Optional[str] = None,
        marksman_camp: Optional[str] = None,
        lancer_camp: Optional[str] = None,
        war_academy: Optional[str] = None,
    ) -> None:
        log_cmd("furnace_set", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        try:
            existing = dict(self._get_profile(interaction.user.id) or {})
            base_level = current_level or existing.get("current_level")
            if not base_level:
                raise ReferenceError("current_level is required the first time you save your furnace profile.")
            self._get_level_entry(base_level)

            merged_fire_crystals = int(current_fire_crystals if current_fire_crystals is not None else existing.get("fire_crystals", 0))
            merged_refined_fire_crystals = int(current_refined_fire_crystals if current_refined_fire_crystals is not None else existing.get("refined_fire_crystals", 0))
            merged_weekly_refines = int(weekly_refines if weekly_refines is not None else existing.get("weekly_refines", 0))
            merged_preferred_package = str(preferred_package if preferred_package is not None else existing.get("preferred_package", "minimum"))
            merged_weekly_fc_income = int(weekly_fire_crystals_income if weekly_fire_crystals_income is not None else existing.get("weekly_fire_crystals_income", 0))
            merged_weekly_rfc_income = int(weekly_rfc_income if weekly_rfc_income is not None else existing.get("weekly_refined_fire_crystals_income", 0))

            self._require_non_negative("current_fire_crystals", merged_fire_crystals)
            self._require_non_negative("current_refined_fire_crystals", merged_refined_fire_crystals)
            self._require_non_negative("weekly_refines", merged_weekly_refines)
            self._require_non_negative("weekly_fire_crystals_income", merged_weekly_fc_income)
            self._require_non_negative("weekly_rfc_income", merged_weekly_rfc_income)

            buildings = dict(existing.get("current_buildings") or self._default_buildings_for_level(base_level))
            for key in BUILDING_ORDER:
                buildings.setdefault(key, base_level)
            for key, value in {
                "furnace": furnace,
                "embassy": embassy,
                "command_center": command_center,
                "infirmary": infirmary,
                "infantry_camp": infantry_camp,
                "marksman_camp": marksman_camp,
                "lancer_camp": lancer_camp,
                "war_academy": war_academy,
            }.items():
                if value is not None:
                    self._get_level_entry(value)
                    buildings[key] = value

            self.profiles[str(interaction.user.id)] = {
                "current_level": base_level,
                "current_buildings": buildings,
                "fire_crystals": merged_fire_crystals,
                "refined_fire_crystals": merged_refined_fire_crystals,
                "weekly_refines": merged_weekly_refines,
                "preferred_package": merged_preferred_package,
                "weekly_fire_crystals_income": merged_weekly_fc_income,
                "weekly_refined_fire_crystals_income": merged_weekly_rfc_income,
                "updated_at": datetime.now(self._tz()).isoformat(timespec="seconds"),
            }
            self.save_profiles()
            await interaction.followup.send("✅ Furnace profile saved.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(name="furnace_view", description="View your saved furnace profile.")
    async def furnace_view(self, interaction: discord.Interaction) -> None:
        log_cmd("furnace_view", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        profile = self._get_profile(interaction.user.id)
        if not profile:
            await interaction.followup.send("No saved furnace profile found.", ephemeral=True)
            return
        embed = self._base_embed("Saved Furnace Profile")
        embed.add_field(name="Current level", value=str(profile.get("current_level", "-")), inline=True)
        embed.add_field(name="Fire Crystals", value=self._fmt_int(int(profile.get("fire_crystals", 0))), inline=True)
        embed.add_field(name="Refined Fire Crystals", value=self._fmt_int(int(profile.get("refined_fire_crystals", 0))), inline=True)
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
        building_levels = profile.get("current_buildings", {})
        embed.add_field(
            name="Saved building levels",
            value="\n".join(
                f"{BUILDING_DISPLAY_NAMES[key]}: **{building_levels.get(key, profile.get('current_level', '-'))}**"
                for key in BUILDING_ORDER
            ),
            inline=False,
        )
        embed.add_field(name="Updated", value=str(profile.get("updated_at", "-")), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -----------------------------
    # help commands
    # -----------------------------
    @app_commands.command(name="furnace_help", description="Show the furnace calculator help sheet.")
    async def furnace_help(self, interaction: discord.Interaction) -> None:
        log_cmd("furnace_help", interaction)
        if not await self._ensure_allowed(interaction):
            return
        await ensure_deferred(interaction, ephemeral=True)
        await interaction.followup.send(embeds=self._build_help_embeds(), ephemeral=True)

    @forge.command(name="post_help", description="Post the furnace help sheet into a channel.")
    @app_commands.describe(channel="Channel to post the help sheet into")
    async def forge_post_help(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        log_cmd("forge_post_help", interaction)
        if not await self._ensure_allowed(interaction):
            return
        if not await self._ensure_tech_role(interaction):
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
    @app_commands.command(name="furnace_refines_needed", description="Work out the weekly refines needed by a target date.")
    @app_commands.choices(target_level=LEVEL_CHOICES, current_level=LEVEL_CHOICES, package=PACKAGE_CHOICES)
    @app_commands.describe(
        target_level="Target furnace level",
        target_date="YYYY-MM-DD, DD/MM/YYYY, or DD-MM-YYYY",
        current_level="Leave blank to use saved profile",
        current_fire_crystals="Leave blank to use saved profile",
        current_refined_fire_crystals="Leave blank to use saved profile",
        package="Leave blank to use saved profile/default",
        use_saved="Use saved profile defaults for blank fields",
        weekly_fire_crystals_income="Optional Fire Crystal income per week",
        weekly_rfc_income="Optional Refined Fire Crystal income per week",
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
            current_fc = self._require_non_negative("current_fire_crystals", int(merged["current_fire_crystals"]))
            current_rfc = self._require_non_negative(
                "current_refined_fire_crystals", int(merged["current_refined_fire_crystals"])
            )
            weekly_fc_income = self._require_non_negative(
                "weekly_fire_crystals_income", int(merged["weekly_fire_crystals_income"])
            )
            weekly_rfc_income_val = self._require_non_negative("weekly_rfc_income", int(merged["weekly_rfc_income"]))

            current_buildings = self._get_profile_buildings(interaction.user.id, current_level_name) if use_saved else self._default_buildings_for_level(current_level_name)
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

            def build_mode_block(projection: RefineWindowProjection, viable: bool, theoretical: bool) -> str:
                produced = projection.expected_rfc if theoretical else float(projection.minimum_rfc)
                remaining_fc_after_refines = fc_budget_for_refines - projection.fire_crystal_spent
                status = "✅ Works" if viable else "❌ Not enough FC budget"
                monday_refines = projection.weekly_refines - 6 if projection.weekly_refines >= 7 else min(projection.weekly_refines, 1)
                delta_rfc = produced - rfc_shortfall_before_refines
                return (
                    f"{status}\n"
                    f"Weekly refines needed: **{self._fmt_int(projection.weekly_refines)}**\n"
                    f"Monday refines: **{self._fmt_int(max(monday_refines, 0))}**\n"
                    f"Attempts in window: **{self._fmt_int(projection.total_attempts)}**\n"
                    f"FC spent on refines: **{self._fmt_int(projection.fire_crystal_spent)}**\n"
                    f"RFC from refines: **{self._fmt_float(produced) if theoretical else self._fmt_int(int(produced))}**\n"
                    f"RFC delta vs target: **{self._fmt_float(delta_rfc) if theoretical else self._fmt_int(int(delta_rfc))}**\n"
                    f"FC left after refines: **{self._fmt_int(remaining_fc_after_refines)}**\n"
                    f"{self._current_and_weekly_plan_text(projection.weekly_refines, start_date, parsed_date)}"
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
                        f"Weekly pattern: {self._format_weekly_schedule(affordable_projection.weekly_refines)}\n"
                        f"FC spent on refines: **{self._fmt_int(affordable_projection.fire_crystal_spent)}**\n"
                        f"Guaranteed RFC from refines: **{self._fmt_int(affordable_projection.minimum_rfc)}**\n"
                        f"Expected RFC from refines: **{self._fmt_float(affordable_projection.expected_rfc)}**\n"
                        f"Guaranteed reachable level: **{guaranteed_budget_result['reached_level']}**\n"
                        f"Expected reachable level: **{expected_budget_result['reached_level']}**"
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

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(name="furnace_upgrade_forecast", description="Given weekly refines, show the highest level reachable by date.")
    @app_commands.choices(current_level=LEVEL_CHOICES, package=PACKAGE_CHOICES)
    @app_commands.describe(
        target_date="YYYY-MM-DD, DD/MM/YYYY, or DD-MM-YYYY",
        weekly_refines="Refines you plan to do each week",
        current_level="Leave blank to use saved profile",
        current_fire_crystals="Leave blank to use saved profile",
        current_refined_fire_crystals="Leave blank to use saved profile",
        package="Leave blank to use saved profile/default",
        use_saved="Use saved profile defaults for blank fields",
        weekly_fire_crystals_income="Optional Fire Crystal income per week",
        weekly_rfc_income="Optional Refined Fire Crystal income per week",
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

            current_buildings = self._get_profile_buildings(interaction.user.id, current_level_name) if use_saved else self._default_buildings_for_level(current_level_name)
            guaranteed_result = self.forecast_reachable_level(current_level_name, total_fc_pool, guaranteed_rfc_pool, package_name, current_buildings)
            expected_result = self.forecast_reachable_level(current_level_name, total_fc_pool, expected_rfc_pool, package_name, current_buildings)

            days_available = (parsed_date - start_date).days + 1
            embed = self._base_embed(
                title="WoS Furnace Upgrade Forecast",
                description=(
                    f"**Start:** {current_level_name}\n"
                    f"**Package:** {package_name}\n"
                    f"**Window:** {start_date.isoformat()} → {parsed_date.isoformat()} ({days_available} days, inclusive)"
                ),
            )
            monday_refines = weekly_refines - 6 if weekly_refines >= 7 else min(weekly_refines, 1)
            embed.add_field(
                name="Weekly Refine Plan",
                value=(
                    f"Weekly refines: **{self._fmt_int(weekly_refines)}**\n"
                    f"Monday refines: **{self._fmt_int(max(monday_refines, 0))}**\n"
                    f"{self._current_and_weekly_plan_text(weekly_refines, start_date, parsed_date)}\n"
                    f"Attempts in window: **{self._fmt_int(refine_projection.total_attempts)}**"
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
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @forge.command(name="reference_check", description="Show loaded furnace reference metadata.")
    async def forge_reference_check(self, interaction: discord.Interaction) -> None:
        log_cmd("forge_reference_check", interaction)
        if not await self._ensure_allowed(interaction):
            return
        if not await self._ensure_tech_role(interaction):
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

    @forge.command(name="reference_reload", description="Reload the furnace JSON references.")
    async def forge_reference_reload(self, interaction: discord.Interaction) -> None:
        log_cmd("forge_reference_reload", interaction)
        if not await self._ensure_allowed(interaction):
            return
        if not await self._ensure_tech_role(interaction):
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

async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)
    cog = WOSFurnaceCalculator(bot)
    guild_obj = discord.Object(id=bot.hot_config["guild_id"])
    for cmd in cog.get_app_commands():
        cmd._guild_ids = {bot.hot_config["guild_id"]}
        cmd.guilds = (guild_obj,)
    await bot.add_cog(cog)
