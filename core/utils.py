# core/utils.py
import os
import json
import re
import asyncio
from pathlib import Path
from typing import Any

import discord
from .logger import warn

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any):
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            warn(f"Could not load {path}: {e!r}")
    return default


def save_json(path: Path, data: Any):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def humanize_secs(s: int) -> str:
    s = max(0, int(s))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


_TIMER_RE = re.compile(r"(\d+)([smhdSMHD]?)")


def parse_timer(timer_str: str) -> int:
    m = _TIMER_RE.fullmatch(timer_str.strip())
    if not m:
        raise ValueError("Use formats like 30s, 5m, 2h, or 1d.")
    val = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit == "s":
        return val
    if unit == "m":
        return val * 60
    if unit == "h":
        return val * 3600
    if unit == "d":
        return val * 86400
    raise ValueError("Unknown time unit.")


# ================================
# SAFE DEFER (NEW, DOES NOT BREAK)
# ================================
async def ensure_deferred(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = False,
) -> bool:
    """
    Acknowledge an interaction safely.
    Returns False if the interaction is already dead (10062).
    Existing callers that ignore the return value continue to work.
    """
    try:
        if interaction.response.is_done():
            return True
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        return True
    except (discord.NotFound, discord.HTTPException) as e:
        warn(f"ensure_deferred failed: {e!r}")
        return False


async def safe_ephemeral(interaction: discord.Interaction, content: str):
    """Try ephemeral response -> ephemeral followup -> channel."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content, ephemeral=True)
            return
    except Exception:
        pass
    try:
        await interaction.followup.send(content, ephemeral=True)
        return
    except Exception:
        pass
    try:
        await interaction.channel.send(content)
    except Exception as e:
        warn(f"safe_ephemeral channel send failed: {e!r}")
