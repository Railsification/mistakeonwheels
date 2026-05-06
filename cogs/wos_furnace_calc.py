from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
UPGRADES_PATH = DATA_DIR / "wos_furnace_upgrades.json"
REFINES_PATH = DATA_DIR / "wos_refine_rates.json"
PROFILES_PATH = DATA_DIR / "wos_furnace_profiles.json"


class ReferenceError(ValueError):
    pass


@dataclass
class RefineWindowProjection:
    weekly_refines: int
    full_week_counts: List[int]
    total_attempts: int
    fire_crystal_spent: int
    minimum_rfc: int
    expected_rfc: float
    maximum_rfc: int


class WOSFurnaceCalculator(commands.Cog):
    """Whiteout Survival furnace calculator with editable JSON references and saved user profiles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.upgrades: Dict[str, Any] = {}
        self.refines: Dict[str, Any] = {}
        self.profiles: Dict[str, Any] = {}
        self.level_map: Dict[str, Dict[str, Any]] = {}
        self.level_names: List[str] = []
        self.timezone_name: str = "Australia/Brisbane"
        self.load_reference_files()
        self.load_profiles()

    # ------------------------------------------------------------------
    # Loading / saving
    # ------------------------------------------------------------------
    def load_reference_files(self) -> None:
        self.upgrades = self._load_json(UPGRADES_PATH)
        self.refines = self._load_json(REFINES_PATH)
        self._validate_upgrades(self.upgrades)
        self._validate_refines(self.refines)
        self.timezone_name = self.upgrades.get("timezone", "Australia/Brisbane")
        self.level_map = {
            str(entry["level"]).strip().casefold(): entry
            for entry in self.upgrades["levels"]
        }
        self.level_names = [entry["level"] for entry in self.upgrades["levels"]]

    def load_profiles(self) -> None:
        if not PROFILES_PATH.exists():
            self.profiles = {}
            self.save_profiles()
            return

        data = self._load_json(PROFILES_PATH)
        if not isinstance(data, dict):
            raise ReferenceError("wos_furnace_profiles.json must be a JSON object.")
        self.profiles = {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def save_profiles(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with PROFILES_PATH.open("w", encoding="utf-8") as f:
            json.dump(self.profiles, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _load_json(path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise ReferenceError(f"Missing reference file: {path}")
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as exc:
            raise ReferenceError(f"Invalid JSON in {path.name}: {exc}") from exc

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
            if next_level is None:
                continue
            if not packages:
                raise ReferenceError(f"levels[{idx}] must define packages while next_level exists.")
            for package_name, package in packages.items():
                if not isinstance(package_name, str) or not package_name.strip():
                    raise ReferenceError(f"levels[{idx}] has an invalid package name.")
                if not isinstance(package, dict):
                    raise ReferenceError(f"Package '{package_name}' at {level} must be an object.")
                requirements = package.get("requirements")
                if not isinstance(requirements, list):
                    raise ReferenceError(
                        f"Package '{package_name}' at {level} must contain a 'requirements' list."
                    )
                for req_index, req in enumerate(requirements, start=1):
                    WOSFurnaceCalculator._validate_requirement(level, package_name, req_index, req)

    @staticmethod
    def _validate_requirement(level: str, package_name: str, req_index: int, req: Dict[str, Any]) -> None:
        if not isinstance(req, dict):
            raise ReferenceError(
                f"Requirement {req_index} in package '{package_name}' at level {level} must be an object."
            )
        if "options" in req:
            options = req.get("options")
            choose = req.get("choose", 1)
            if not isinstance(options, list) or not options:
                raise ReferenceError(
                    f"Choice requirement {req_index} in package '{package_name}' at level {level} must have options."
                )
            if not isinstance(choose, int) or choose < 1 or choose > len(options):
                raise ReferenceError(
                    f"Choice requirement {req_index} in package '{package_name}' at level {level} has invalid choose."
                )
            for opt_index, opt in enumerate(options, start=1):
                WOSFurnaceCalculator._validate_building_cost(level, package_name, f"{req_index}.{opt_index}", opt)
            return
        WOSFurnaceCalculator._validate_building_cost(level, package_name, str(req_index), req)

    @staticmethod
    def _validate_building_cost(level: str, package_name: str, req_label: str, req: Dict[str, Any]) -> None:
        building = req.get("building")
        fire_crystals = req.get("fire_crystals")
        refined_fire_crystals = req.get("refined_fire_crystals")
        if not isinstance(building, str) or not building.strip():
            raise ReferenceError(
                f"Requirement {req_label} in package '{package_name}' at {level} must have a building name."
            )
        if not isinstance(fire_crystals, int) or fire_crystals < 0:
            raise ReferenceError(
                f"Requirement {req_label} in package '{package_name}' at {level} must have integer fire_crystals >= 0."
            )
        if not isinstance(refined_fire_crystals, int) or refined_fire_crystals < 0:
            raise ReferenceError(
                f"Requirement {req_label} in package '{package_name}' at {level} must have integer refined_fire_crystals >= 0."
            )

    @staticmethod
    def _validate_refines(data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ReferenceError("wos_refine_rates.json must be a JSON object.")
        tiers = data.get("tiers")
        if not isinstance(tiers, list) or not tiers:
            raise ReferenceError("wos_refine_rates.json must contain a non-empty 'tiers' list.")

        previous_max = 0
        for idx, tier in enumerate(tiers, start=1):
            if not isinstance(tier, dict):
                raise ReferenceError(f"tiers[{idx}] must be an object.")
            name = tier.get("name")
            min_attempt = tier.get("min_attempt")
            max_attempt = tier.get("max_attempt")
            fire_cost = tier.get("fire_crystal_cost")
            outcomes = tier.get("outcomes")
            if not isinstance(name, str) or not name.strip():
                raise ReferenceError(f"tiers[{idx}].name must be a non-empty string.")
            if not isinstance(min_attempt, int) or min_attempt < 1:
                raise ReferenceError(f"tiers[{idx}].min_attempt must be an integer >= 1.")
            if not isinstance(max_attempt, int) or max_attempt < min_attempt:
                raise ReferenceError(f"tiers[{idx}].max_attempt must be an integer >= min_attempt.")
            if min_attempt != previous_max + 1:
                raise ReferenceError(
                    f"Refine tiers must be contiguous. Tier '{name}' starts at {min_attempt}, expected {previous_max + 1}."
                )
            previous_max = max_attempt
            if not isinstance(fire_cost, int) or fire_cost < 0:
                raise ReferenceError(f"tiers[{idx}].fire_crystal_cost must be an integer >= 0.")
            if not isinstance(outcomes, list) or not outcomes:
                raise ReferenceError(f"tiers[{idx}].outcomes must be a non-empty list.")

            probability_total = 0.0
            for outcome_index, outcome in enumerate(outcomes, start=1):
                if not isinstance(outcome, dict):
                    raise ReferenceError(f"tiers[{idx}].outcomes[{outcome_index}] must be an object.")
                rfc = outcome.get("refined_fire_crystals")
                probability = outcome.get("probability")
                if not isinstance(rfc, int) or rfc < 0:
                    raise ReferenceError(
                        f"tiers[{idx}].outcomes[{outcome_index}].refined_fire_crystals must be an integer >= 0."
                    )
                if not isinstance(probability, (int, float)) or float(probability) < 0:
                    raise ReferenceError(
                        f"tiers[{idx}].outcomes[{outcome_index}].probability must be a number >= 0."
                    )
                probability_total += float(probability)

            if not (
                math.isclose(probability_total, 1.0, rel_tol=1e-9, abs_tol=1e-9)
                or math.isclose(probability_total, 100.0, rel_tol=1e-9, abs_tol=1e-9)
            ):
                raise ReferenceError(
                    f"Tier '{name}' probabilities must sum to 1.0 or 100.0. Found {probability_total}."
                )

        discount = data.get("first_refine_discount", 0.5)
        if not isinstance(discount, (int, float)) or not (0.0 <= float(discount) <= 1.0):
            raise ReferenceError("first_refine_discount must be a number between 0.0 and 1.0.")

    # ------------------------------------------------------------------
    # General helpers
    # ------------------------------------------------------------------
    def _now_local_date(self) -> date:
        return datetime.now(ZoneInfo(self.timezone_name)).date()

    def _parse_target_date(self, value: str) -> date:
        value = value.strip()
        formats = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]
        for fmt in formats:
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        raise ReferenceError("Date must be one of: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY")

    @staticmethod
    def _normalize_level_name(level_name: str) -> str:
        return level_name.strip().casefold()

    def _get_level_entry(self, level_name: str) -> Dict[str, Any]:
        entry = self.level_map.get(self._normalize_level_name(level_name))
        if entry is None:
            raise ReferenceError(f"Unknown level '{level_name}'. Known levels: {', '.join(self.level_names)}")
        return entry

    def _get_profile(self, user_id: int) -> Dict[str, Any]:
        return self.profiles.get(str(user_id), {})

    def _set_profile_value(self, user_id: int, key: str, value: Any) -> None:
        profile = self.profiles.setdefault(str(user_id), {})
        profile[key] = value
        profile["updated_at"] = datetime.now(ZoneInfo(self.timezone_name)).isoformat(timespec="seconds")
        self.save_profiles()

    def _merge_profile_defaults(
        self,
        user_id: int,
        use_saved: bool,
        current_level: Optional[str],
        current_fire_crystals: Optional[int],
        current_refined_fire_crystals: Optional[int],
        package: Optional[str],
        weekly_fire_crystals_income: Optional[int],
        weekly_refined_fire_crystals_income: Optional[int],
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
            "weekly_refined_fire_crystals_income": (
                weekly_refined_fire_crystals_income
                if weekly_refined_fire_crystals_income is not None
                else profile.get("weekly_refined_fire_crystals_income", 0)
            ),
        }
        if not merged["current_level"]:
            raise ReferenceError("current_level is required. Set a profile or pass it in the command.")
        return merged

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
    def _project_weekly_amount(amount_per_week: int, start_date: date, target_date: date) -> int:
        if amount_per_week <= 0 or target_date < start_date:
            return 0
        total_days = (target_date - start_date).days + 1
        full_weeks = total_days // 7
        partial_days = total_days % 7
        return (full_weeks * amount_per_week) + math.floor((amount_per_week * partial_days) / 7)

    def _base_embed(self, title: str, description: str = "") -> discord.Embed:
        embed = discord.Embed(title=title, description=description, colour=discord.Colour.orange())
        embed.set_footer(text=f"TZ: {self.timezone_name}")
        return embed

    # ------------------------------------------------------------------
    # Refine helpers
    # ------------------------------------------------------------------
    def _tier_probability_scale(self, tier: Dict[str, Any]) -> float:
        total = sum(float(outcome["probability"]) for outcome in tier["outcomes"])
        return 100.0 if math.isclose(total, 100.0, rel_tol=1e-9, abs_tol=1e-9) else 1.0

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
        raise ReferenceError(
            f"Attempt #{attempt_number} exceeds the last defined refine tier and attempts_above_max_use_last_tier is false."
        )

    @staticmethod
    def _weekly_day_counts(total_attempts: int, days: int = 7) -> List[int]:
        if total_attempts <= 0 or days <= 0:
            return [0] * days

        triangle = days * (days - 1) // 2
        if total_attempts >= triangle:
            monday_base = (total_attempts - triangle) // days
            counts = [monday_base + i for i in range(days)]
            remainder = total_attempts - sum(counts)
            for idx in range(days - remainder, days):
                if 0 <= idx < days:
                    counts[idx] += 1
            return counts

        counts = [0] * days
        active_days = min(total_attempts, days)
        for idx in range(active_days):
            counts[idx] = 1
        remaining = total_attempts - active_days
        idx = active_days - 1
        while remaining > 0 and idx >= 0:
            counts[idx] += 1
            remaining -= 1
            idx -= 1
            if idx < 0:
                idx = active_days - 1
        return counts

    @staticmethod
    def _weekday_labels() -> List[str]:
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def _format_weekly_schedule(self, weekly_refines: int) -> str:
        counts = self._weekly_day_counts(weekly_refines, 7)
        labels = self._weekday_labels()
        lines = [f"{labels[i]} **{counts[i]}**" for i in range(7)]
        return " | ".join(lines)

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
            return RefineWindowProjection(weekly_refines, [0] * 7, 0, 0, 0, 0.0, 0)

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
            full_week_counts=self._weekly_day_counts(weekly_refines, 7),
            total_attempts=total_attempts,
            fire_crystal_spent=int(round(total_fc_spent)),
            minimum_rfc=total_min_rfc,
            expected_rfc=total_expected_rfc,
            maximum_rfc=total_max_rfc,
        )

    def find_min_weekly_refines_for_rfc(
        self,
        required_rfc: int,
        start_date: date,
        target_date: date,
        mode: str,
    ) -> RefineWindowProjection:
        if required_rfc <= 0:
            return self.simulate_window_refines(0, start_date, target_date)
        if mode not in {"minimum", "expected"}:
            raise ReferenceError("mode must be 'minimum' or 'expected'.")

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
            raise ReferenceError(
                f"Could not satisfy required RFC within a weekly refine plan up to {max_weekly_refines:,}."
            )

        low = 0
        while low < high:
            mid = (low + high) // 2
            if produced(mid) >= required_rfc:
                high = mid
            else:
                low = mid + 1
        return self.simulate_window_refines(low, start_date, target_date)

    # ------------------------------------------------------------------
    # Upgrade helpers
    # ------------------------------------------------------------------
    def _get_package_name(self, level_entry: Dict[str, Any], package_name: str) -> str:
        wanted = package_name.strip().casefold()
        for actual_name in level_entry.get("packages", {}).keys():
            if actual_name.casefold() == wanted:
                return actual_name
        available = ", ".join(level_entry.get("packages", {}).keys())
        raise ReferenceError(
            f"Unknown package '{package_name}' for level {level_entry['level']}. Available: {available}"
        )

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

        for req in package["requirements"]:
            if "options" in req:
                choose = req.get("choose", 1)
                options = sorted(
                    req["options"],
                    key=lambda x: (x["refined_fire_crystals"], x["fire_crystals"], x["building"].casefold()),
                )
                chosen = options[:choose]
                for option in chosen:
                    total_fc += option["fire_crystals"]
                    total_rfc += option["refined_fire_crystals"]
                    selected_buildings.append(
                        {
                            "building": option["building"],
                            "fire_crystals": option["fire_crystals"],
                            "refined_fire_crystals": option["refined_fire_crystals"],
                            "selected_from_choice_group": req.get("choice_group", "choice"),
                        }
                    )
            else:
                total_fc += req["fire_crystals"]
                total_rfc += req["refined_fire_crystals"]
                selected_buildings.append(
                    {
                        "building": req["building"],
                        "fire_crystals": req["fire_crystals"],
                        "refined_fire_crystals": req["refined_fire_crystals"],
                    }
                )

        return {
            "package_name": actual_package_name,
            "description": package.get("description", ""),
            "fire_crystals": total_fc,
            "refined_fire_crystals": total_rfc,
            "selected_buildings": selected_buildings,
        }

    def build_upgrade_steps(self, current_level: str, target_level: str, package_name: str) -> List[Dict[str, Any]]:
        current_entry = self._get_level_entry(current_level)
        target_name = self._normalize_level_name(target_level)
        if self._normalize_level_name(current_entry["level"]) == target_name:
            return []

        steps: List[Dict[str, Any]] = []
        visited: set[str] = set()
        level_name = current_entry["level"]

        while self._normalize_level_name(level_name) != target_name:
            level_entry = self._get_level_entry(level_name)
            key = self._normalize_level_name(level_entry["level"])
            if key in visited:
                raise ReferenceError("Upgrade path loop detected in reference file.")
            visited.add(key)

            next_level = level_entry.get("next_level")
            if not next_level:
                raise ReferenceError(f"Cannot continue from level {level_entry['level']}. It has no next_level defined.")

            resolved = self.resolve_package(level_entry, package_name)
            steps.append(
                {
                    "from_level": level_entry["level"],
                    "to_level": next_level,
                    **resolved,
                }
            )
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
    ) -> Dict[str, Any]:
        steps_taken: List[Dict[str, Any]] = []
        cursor_level = current_level
        remaining_fc = available_fc
        remaining_rfc = available_rfc

        while True:
            level_entry = self._get_level_entry(cursor_level)
            next_level = level_entry.get("next_level")
            if not next_level:
                break
            resolved = self.resolve_package(level_entry, package_name)
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
            cursor_level = next_level

        next_step = None
        level_entry = self._get_level_entry(cursor_level)
        if level_entry.get("next_level"):
            next_step = {
                "from_level": level_entry["level"],
                "to_level": level_entry["next_level"],
                **self.resolve_package(level_entry, package_name),
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
        return ", ".join(building["building"] for building in step["selected_buildings"])

    # ------------------------------------------------------------------
    # Autocomplete helpers
    # ------------------------------------------------------------------
    async def _level_choices(self, current: str) -> List[app_commands.Choice[str]]:
        current_cf = current.casefold().strip()
        matches = [level for level in self.level_names if current_cf in level.casefold()]
        return [app_commands.Choice(name=level, value=level) for level in matches[:25]]

    async def _package_choices(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        level_name = None
        namespace = getattr(interaction, "namespace", None)
        if namespace is not None:
            level_name = getattr(namespace, "current_level", None)
        if not level_name:
            profile = self._get_profile(interaction.user.id)
            level_name = profile.get("current_level")
        package_names: List[str] = []
        if level_name:
            try:
                package_names = list(self._get_level_entry(level_name).get("packages", {}).keys())
            except Exception:
                package_names = []
        if not package_names:
            seen: set[str] = set()
            for entry in self.upgrades["levels"]:
                for package_name in entry.get("packages", {}).keys():
                    key = package_name.casefold()
                    if key not in seen:
                        seen.add(key)
                        package_names.append(package_name)
        current_cf = current.casefold().strip()
        matches = [name for name in package_names if current_cf in name.casefold()]
        return [app_commands.Choice(name=name, value=name) for name in matches[:25]]

    # ------------------------------------------------------------------
    # Profile commands
    # ------------------------------------------------------------------
    @app_commands.command(name="furnace_profile_set", description="Create or replace your saved furnace profile.")
    @app_commands.describe(
        current_level="Your current furnace level, e.g. FC5",
        current_fire_crystals="Current Fire Crystals",
        current_refined_fire_crystals="Current Refined Fire Crystals",
        weekly_refines="Your usual planned refines per week",
        preferred_package="Default upgrade package name",
        weekly_fire_crystals_income="Optional weekly Fire Crystal gain",
        weekly_refined_fire_crystals_income="Optional weekly Refined Fire Crystal gain",
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
        weekly_refined_fire_crystals_income: int = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            self._get_level_entry(current_level)
            self._require_non_negative("current_fire_crystals", current_fire_crystals)
            self._require_non_negative("current_refined_fire_crystals", current_refined_fire_crystals)
            self._require_non_negative("weekly_refines", weekly_refines)
            self._require_non_negative("weekly_fire_crystals_income", weekly_fire_crystals_income)
            self._require_non_negative(
                "weekly_refined_fire_crystals_income", weekly_refined_fire_crystals_income
            )
            self.profiles[str(interaction.user.id)] = {
                "current_level": current_level,
                "fire_crystals": current_fire_crystals,
                "refined_fire_crystals": current_refined_fire_crystals,
                "weekly_refines": weekly_refines,
                "preferred_package": preferred_package,
                "weekly_fire_crystals_income": weekly_fire_crystals_income,
                "weekly_refined_fire_crystals_income": weekly_refined_fire_crystals_income,
                "updated_at": datetime.now(ZoneInfo(self.timezone_name)).isoformat(timespec="seconds"),
            }
            self.save_profiles()
            await interaction.followup.send("✅ Furnace profile saved.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(name="furnace_profile_view", description="View your saved furnace profile.")
    async def furnace_profile_view(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        profile = self._get_profile(interaction.user.id)
        if not profile:
            await interaction.followup.send("No saved furnace profile found.", ephemeral=True)
            return
        embed = self._base_embed("Saved Furnace Profile")
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
        embed.add_field(name="Updated", value=str(profile.get("updated_at", "-")), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="furnace_profile_update", description="Update one or more saved furnace profile values.")
    async def furnace_profile_update(
        self,
        interaction: discord.Interaction,
        current_level: Optional[str] = None,
        current_fire_crystals: Optional[int] = None,
        current_refined_fire_crystals: Optional[int] = None,
        weekly_refines: Optional[int] = None,
        preferred_package: Optional[str] = None,
        weekly_fire_crystals_income: Optional[int] = None,
        weekly_refined_fire_crystals_income: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            profile = self._get_profile(interaction.user.id)
            if not profile:
                raise ReferenceError("No saved profile found. Use /furnace_profile_set first.")
            if current_level is not None:
                self._get_level_entry(current_level)
                profile["current_level"] = current_level
            if current_fire_crystals is not None:
                self._require_non_negative("current_fire_crystals", current_fire_crystals)
                profile["fire_crystals"] = current_fire_crystals
            if current_refined_fire_crystals is not None:
                self._require_non_negative("current_refined_fire_crystals", current_refined_fire_crystals)
                profile["refined_fire_crystals"] = current_refined_fire_crystals
            if weekly_refines is not None:
                self._require_non_negative("weekly_refines", weekly_refines)
                profile["weekly_refines"] = weekly_refines
            if preferred_package is not None:
                profile["preferred_package"] = preferred_package
            if weekly_fire_crystals_income is not None:
                self._require_non_negative("weekly_fire_crystals_income", weekly_fire_crystals_income)
                profile["weekly_fire_crystals_income"] = weekly_fire_crystals_income
            if weekly_refined_fire_crystals_income is not None:
                self._require_non_negative(
                    "weekly_refined_fire_crystals_income", weekly_refined_fire_crystals_income
                )
                profile["weekly_refined_fire_crystals_income"] = weekly_refined_fire_crystals_income
            profile["updated_at"] = datetime.now(ZoneInfo(self.timezone_name)).isoformat(timespec="seconds")
            self.save_profiles()
            await interaction.followup.send("✅ Furnace profile updated.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(name="furnace_profile_clear", description="Delete your saved furnace profile.")
    async def furnace_profile_clear(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(interaction.user.id) in self.profiles:
            self.profiles.pop(str(interaction.user.id), None)
            self.save_profiles()
            await interaction.followup.send("✅ Furnace profile cleared.", ephemeral=True)
        else:
            await interaction.followup.send("No saved furnace profile found.", ephemeral=True)

    # ------------------------------------------------------------------
    # Main commands
    # ------------------------------------------------------------------
    @app_commands.command(
        name="furnace_refines_needed",
        description="Show the weekly refine pace needed to hit a target furnace level by a target date.",
    )
    @app_commands.describe(
        target_level="Target furnace level to reach by the date",
        target_date="Target date: YYYY-MM-DD, DD/MM/YYYY, or DD-MM-YYYY",
        current_level="Current furnace level. Leave blank to use saved profile",
        current_fire_crystals="Current Fire Crystals. Leave blank to use saved profile",
        current_refined_fire_crystals="Current Refined Fire Crystals. Leave blank to use saved profile",
        package="Upgrade package name. Leave blank to use saved profile/default",
        use_saved="Use saved profile defaults for any blank fields",
        weekly_fire_crystals_income="Optional Fire Crystal income per week before the target date",
        weekly_refined_fire_crystals_income="Optional Refined Fire Crystal income per week before the target date",
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
        weekly_refined_fire_crystals_income: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
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
                weekly_refined_fire_crystals_income=weekly_refined_fire_crystals_income,
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
            weekly_rfc_income = self._require_non_negative(
                "weekly_refined_fire_crystals_income", int(merged["weekly_refined_fire_crystals_income"])
            )

            steps = self.build_upgrade_steps(current_level_name, target_level, package_name)
            summary = self.summarize_steps(steps)
            projected_fc_income = self._project_weekly_amount(weekly_fc_income, start_date, parsed_date)
            projected_rfc_income = self._project_weekly_amount(weekly_rfc_income, start_date, parsed_date)
            fc_budget_for_refines = current_fc + projected_fc_income - summary["fire_crystals"]
            rfc_shortfall_before_refines = max(0, summary["refined_fire_crystals"] - (current_rfc + projected_rfc_income))

            min_projection = self.find_min_weekly_refines_for_rfc(
                rfc_shortfall_before_refines, start_date, parsed_date, mode="minimum"
            )
            exp_projection = self.find_min_weekly_refines_for_rfc(
                rfc_shortfall_before_refines, start_date, parsed_date, mode="expected"
            )

            min_viable = min_projection.fire_crystal_spent <= fc_budget_for_refines
            exp_viable = exp_projection.fire_crystal_spent <= fc_budget_for_refines

            days_available = (parsed_date - start_date).days + 1
            embed = self._base_embed(
                title="WoS Furnace Refines Needed",
                description=(
                    f"**Start:** {current_level_name}\n"
                    f"**Target:** {target_level}\n"
                    f"**Package:** {package_name}\n"
                    f"**Window:** {start_date.isoformat()} → {parsed_date.isoformat()} ({days_available} day{'s' if days_available != 1 else ''}, inclusive)"
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
                    f"RFC still needed: **{self._fmt_int(rfc_shortfall_before_refines)}**"
                ),
                inline=True,
            )

            def build_mode_block(label: str, projection: RefineWindowProjection, viable: bool, theoretical: bool) -> str:
                produced = projection.expected_rfc if theoretical else projection.minimum_rfc
                remaining_fc_after_refines = fc_budget_for_refines - projection.fire_crystal_spent
                status = "✅ Works" if viable else "❌ Not enough FC budget"
                return (
                    f"{status}\n"
                    f"Weekly refines needed: **{self._fmt_int(projection.weekly_refines)}**\n"
                    f"Attempts in window: **{self._fmt_int(projection.total_attempts)}**\n"
                    f"FC spent on refines: **{self._fmt_int(projection.fire_crystal_spent)}**\n"
                    f"RFC from refines: **{self._fmt_float(produced) if theoretical else self._fmt_int(int(produced))}**\n"
                    f"FC left after refines: **{self._fmt_int(remaining_fc_after_refines)}**\n"
                    f"Weekly template: {self._format_weekly_schedule(projection.weekly_refines)}"
                )

            embed.add_field(
                name="Guaranteed / Minimum RFC Plan",
                value=build_mode_block("minimum", min_projection, min_viable, theoretical=False),
                inline=False,
            )
            embed.add_field(
                name="Expected / Theoretical RFC Plan",
                value=build_mode_block("expected", exp_projection, exp_viable, theoretical=True),
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

            if not min_viable and not exp_viable:
                embed.add_field(
                    name="Result",
                    value="You do not have enough Fire Crystal budget left for the required refines in this window, even before considering bad rolls.",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(
        name="furnace_upgrade_forecast",
        description="Given a weekly refine plan, show the highest guaranteed and expected level reachable by the target date.",
    )
    @app_commands.describe(
        target_date="Target date: YYYY-MM-DD, DD/MM/YYYY, or DD-MM-YYYY",
        weekly_refines="How many total refines you plan to do per week",
        current_level="Current furnace level. Leave blank to use saved profile",
        current_fire_crystals="Current Fire Crystals. Leave blank to use saved profile",
        current_refined_fire_crystals="Current Refined Fire Crystals. Leave blank to use saved profile",
        package="Upgrade package name. Leave blank to use saved profile/default",
        use_saved="Use saved profile defaults for any blank fields",
        weekly_fire_crystals_income="Optional Fire Crystal income per week before the target date",
        weekly_refined_fire_crystals_income="Optional Refined Fire Crystal income per week before the target date",
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
        weekly_refined_fire_crystals_income: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
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
                weekly_refined_fire_crystals_income=weekly_refined_fire_crystals_income,
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
            weekly_rfc_income = self._require_non_negative(
                "weekly_refined_fire_crystals_income", int(merged["weekly_refined_fire_crystals_income"])
            )

            projected_fc_income = self._project_weekly_amount(weekly_fc_income, start_date, parsed_date)
            projected_rfc_income = self._project_weekly_amount(weekly_rfc_income, start_date, parsed_date)
            refine_projection = self.simulate_window_refines(weekly_refines, start_date, parsed_date)

            total_fc_pool = current_fc + projected_fc_income - refine_projection.fire_crystal_spent
            guaranteed_rfc_pool = current_rfc + projected_rfc_income + refine_projection.minimum_rfc
            expected_rfc_pool = current_rfc + projected_rfc_income + math.floor(refine_projection.expected_rfc)

            guaranteed_result = self.forecast_reachable_level(
                current_level=current_level_name,
                available_fc=total_fc_pool,
                available_rfc=guaranteed_rfc_pool,
                package_name=package_name,
            )
            expected_result = self.forecast_reachable_level(
                current_level=current_level_name,
                available_fc=total_fc_pool,
                available_rfc=expected_rfc_pool,
                package_name=package_name,
            )

            days_available = (parsed_date - start_date).days + 1
            embed = self._base_embed(
                title="WoS Furnace Upgrade Forecast",
                description=(
                    f"**Start:** {current_level_name}\n"
                    f"**Package:** {package_name}\n"
                    f"**Window:** {start_date.isoformat()} → {parsed_date.isoformat()} ({days_available} day{'s' if days_available != 1 else ''}, inclusive)"
                ),
            )
            embed.add_field(
                name="Weekly Refine Plan",
                value=(
                    f"Weekly refines: **{self._fmt_int(weekly_refines)}**\n"
                    f"Template: {self._format_weekly_schedule(weekly_refines)}\n"
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

    @app_commands.command(name="furnace_reference_check", description="Show loaded furnace/reference metadata.")
    async def furnace_reference_check(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
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

    @app_commands.command(
        name="furnace_reference_reload",
        description="Reload the furnace JSON reference files without restarting the bot.",
    )
    async def furnace_reference_reload(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            self.load_reference_files()
            self.load_profiles()
            await interaction.followup.send(
                f"✅ Reloaded `{UPGRADES_PATH.name}`, `{REFINES_PATH.name}`, and `{PROFILES_PATH.name}` successfully.",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Reload failed: {exc}", ephemeral=True)

    # ------------------------------------------------------------------
    # Autocomplete bindings
    # ------------------------------------------------------------------
    @furnace_profile_set.autocomplete("current_level")
    @furnace_profile_update.autocomplete("current_level")
    @furnace_refines_needed.autocomplete("current_level")
    @furnace_refines_needed.autocomplete("target_level")
    @furnace_upgrade_forecast.autocomplete("current_level")
    async def furnace_level_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        return await self._level_choices(current)

    @furnace_profile_set.autocomplete("preferred_package")
    @furnace_profile_update.autocomplete("preferred_package")
    @furnace_refines_needed.autocomplete("package")
    @furnace_upgrade_forecast.autocomplete("package")
    async def furnace_package_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        return await self._package_choices(interaction, current)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WOSFurnaceCalculator(bot))
