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
TABLE_PATH = DATA_DIR / "chief_gear_table.json"
PROFILES_FILENAME = "chief_gear_profiles.json"

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
        data = load_json(TABLE_PATH, None)
        if not isinstance(data, dict) or not isinstance(data.get("levels"), list):
            raise RuntimeError(f"Missing/invalid {TABLE_PATH}")
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
        # Prefer comma-separated six/seven digit numbers near the current power line.
        candidates = []
        for match in re.finditer(r"\d{1,3}(?:,\d{3})+", text):
            value = self._parse_int(match.group(0))
            if 200000 <= value <= 6000000:
                candidates.append(value)
        if candidates:
            # Current power is normally the first big number OCR sees; ignore inventory later by using full-text order.
            return candidates[0]

        for match in re.finditer(r"\b\d{6,7}\b", text):
            value = int(match.group(0))
            if 200000 <= value <= 6000000:
                return value
        return None

    def _crop_slot_card(self, pil_img, slot: str):
        w, h = pil_img.size
        x1, y1, x2, y2 = self.SLOT_CARD_REGIONS[slot]
        return pil_img.crop((int(w * x1), int(h * y1), int(w * x2), int(h * y2)))

    def _yellow_mask(self, rgb_arr):
        if cv2 is None or np is None:
            raise RuntimeError("opencv/numpy is not installed.")
        hsv = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2HSV)
        # WoS tier labels and red-gear stars are saturated yellow/gold.
        return cv2.inRange(hsv, np.array([12, 70, 110]), np.array([48, 255, 255]))

    def _ocr_tier_from_card(self, card_img) -> tuple[Optional[int], str]:
        if pytesseract is None or cv2 is None or np is None:
            return None, ""

        arr = np.array(card_img.convert("RGB"))
        ch, cw = arr.shape[:2]
        label = arr[0:int(ch * 0.34), 0:int(cw * 0.52)]
        big = cv2.resize(label, None, fx=6, fy=6, interpolation=cv2.INTER_CUBIC)
        mask = self._yellow_mask(big)
        mask = cv2.dilate(mask, np.ones((2, 2), np.uint8), iterations=1)

        texts: list[str] = []
        for psm in (7, 8, 10, 13):
            try:
                text = pytesseract.image_to_string(
                    mask,
                    config=f"--psm {psm} -c tessedit_char_whitelist=Tt1234Il|",
                ) or ""
                if text.strip():
                    texts.append(text.strip())
            except Exception:
                pass

        raw = " ".join(texts).upper()
        cleaned = raw.replace(" ", "").replace("\n", "")
        cleaned = cleaned.replace("I", "1").replace("L", "1").replace("|", "1")

        # Prefer explicit digits. Tesseract sometimes reads T2 as 12, and T1 as TT/T.
        for digit in ("4", "3", "2", "1"):
            if digit in cleaned:
                return int(digit), raw
        if "T" in cleaned:
            return 1, raw
        return None, raw

    def _count_gear_stars(self, card_img) -> int:
        if cv2 is None or np is None:
            return 0
        arr = np.array(card_img.convert("RGB"))
        ch, cw = arr.shape[:2]
        mask = self._yellow_mask(arr)

        # Real gear stars sit vertically on the left side of the gear tile.
        # This removes the T label at the top and the charm icons under the card.
        mask[:int(ch * 0.25), :] = 0
        mask[:, int(cw * 0.45):] = 0
        mask[int(ch * 0.86):, :] = 0

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        centers: list[tuple[int, int]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            if not (0.12 * cw <= w <= 0.32 * cw):
                continue
            if not (0.12 * ch <= h <= 0.32 * ch):
                continue
            if area < 0.010 * cw * ch:
                continue
            centers.append((x + w // 2, y + h // 2))

        # Merge broken/star contours by vertical position.
        merged: list[int] = []
        for _, cy in sorted(centers, key=lambda item: item[1]):
            if not merged or abs(cy - merged[-1]) > max(8, int(ch * 0.12)):
                merged.append(cy)
        return max(0, min(3, len(merged)))

    def _level_key_from_visible_card(self, tier: Optional[int], stars: int) -> Optional[str]:
        if tier is None:
            return None
        if tier <= 0:
            key = f"red_{stars}"
        else:
            key = f"red_t{tier}_{stars}"
        return key if key in self.level_by_key else None

    def _parse_all_visible_slots(self, pil_img) -> dict[str, ParsedSlotScan]:
        parsed: dict[str, ParsedSlotScan] = {}
        for slot in SLOT_KEYS:
            notes: list[str] = []
            try:
                card = self._crop_slot_card(pil_img, slot)
                tier, raw_tier = self._ocr_tier_from_card(card)
                stars = self._count_gear_stars(card)
                level_key = self._level_key_from_visible_card(tier, stars)
                if level_key is None:
                    notes.append("Could not confidently read tier/stars from the top gear tile.")
                parsed[slot] = ParsedSlotScan(
                    slot=slot,
                    tier=tier,
                    stars=stars,
                    level_key=level_key,
                    raw_tier_text=raw_tier,
                    notes=notes,
                )
            except Exception as exc:
                parsed[slot] = ParsedSlotScan(
                    slot=slot,
                    tier=None,
                    stars=None,
                    level_key=None,
                    raw_tier_text="",
                    notes=[f"Slot scan failed: {exc}"],
                )
        return parsed

    def _parse_screenshot(self, pil_img) -> ParsedScan:
        w, h = pil_img.size
        notes: list[str] = []

        full_text = self._ocr_text(pil_img, psm=6)
        power = self._parse_power(full_text)
        level_key = None
        if power is not None:
            level_key, diff = self._find_nearest_power_level(power)
            if level_key is None:
                notes.append(f"Power `{power:,}` did not match a known table row. Closest diff: {diff:,}.")
        else:
            notes.append("Could not OCR the current power total.")

        # Resource counts are in the lower area of the Chief Gear screen.
        resource_crop = pil_img.crop((0, int(h * 0.78), w, int(h * 0.86)))
        resource_text = self._ocr_text(resource_crop, numeric_only=True, psm=6)
        inventory, next_cost = self._parse_inventory_line(resource_text)

        if not any(inventory.values()):
            # Fallback: full OCR sometimes captures the line better.
            inv2, cost2 = self._parse_inventory_line(full_text)
            if any(inv2.values()):
                inventory, next_cost = inv2, cost2
            else:
                notes.append("Could not OCR inventory counts from the screenshot.")

        return ParsedScan(
            level_key=level_key,
            power_total=power,
            inventory=inventory,
            next_cost=next_cost,
            raw_text=(full_text + "\n" + resource_text).strip(),
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
                "`/chief_gear recommend` — spends saved inventory on cheapest next upgrades"
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
        if any(parsed.next_cost.values()):
            embed.add_field(name="Visible next-click cost", value=self._format_costs(parsed.next_cost), inline=False)
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
    @app_commands.describe(max_steps="Maximum upgrades to show")
    async def recommend_cmd(self, interaction: discord.Interaction, max_steps: app_commands.Range[int, 1, 25] = 10):
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
            for idx, (slot_key, old_key, next_key, cost) in enumerate(steps, start=1):
                lines.append(f"**{idx}. {SLOT_NAMES[slot_key]}**: {self._level_label(old_key)} → {self._level_label(next_key)}")
            embed.add_field(name="Upgrade order", value="\n".join(lines)[:1000], inline=False)
            embed.add_field(name="Inventory left after plan", value=self._format_costs(inventory), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

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
