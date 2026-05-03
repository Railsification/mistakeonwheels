from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from pathlib import Path
from typing import Optional

import cv2
import discord
import numpy as np
from discord import app_commands
from discord.ext import commands


# ===== DATA =====
try:
    from core.utils import DATA_DIR
except Exception:
    DATA_DIR = Path("data")

DATA_DIR.mkdir(parents=True, exist_ok=True)
KNOWLEDGE_FILE = DATA_DIR / "chest_pattern_knowledge.json"


# ===== GRID CONFIG =====
CELL_X = [0.135, 0.376, 0.616, 0.852]
CELL_Y = [0.547, 0.670, 0.792]
CROP_SIZE_REL = 0.18

VALID_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

SYMBOLS = ["UPGRADE", "PLUS", "TARGET", "KEY"]

DEFAULT_WEIGHTS = {
    "UPGRADE": 3.0,
    "PLUS": 2.0,
    "TARGET": 0.0,
    "KEY": -10.0,
}


# ===== STORAGE =====
def load_knowledge():
    if not KNOWLEDGE_FILE.exists():
        return {"guilds": {}}
    try:
        return json.loads(KNOWLEDGE_FILE.read_text())
    except Exception:
        return {"guilds": {}}


def save_knowledge(data):
    KNOWLEDGE_FILE.write_text(json.dumps(data, indent=2))


def guild_key(interaction: discord.Interaction):
    return str(interaction.guild_id or "dm")


def fresh_guild():
    totals = {}
    for r in range(1, 4):
        for c in range(1, 5):
            totals[f"R{r}C{c}"] = {s: 0 for s in SYMBOLS}
    return {"totals": totals, "hashes": [], "images": 0}


# ===== IMAGE =====
def crop_cell(img, row, col):
    h, w = img.shape[:2]
    cx = int(w * CELL_X[col])
    cy = int(h * CELL_Y[row])
    size = int(w * CROP_SIZE_REL)
    half = size // 2
    return img[max(cy-half,0):min(cy+half,h), max(cx-half,0):min(cx+half,w)]


def classify_cell(crop):
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    area = crop.shape[0] * crop.shape[1]

    red = cv2.inRange(hsv, (0,80,100),(12,255,255)) | cv2.inRange(hsv,(170,80,100),(180,255,255))
    yellow = cv2.inRange(hsv,(15,80,120),(45,255,255))

    red_ratio = np.count_nonzero(red)/area
    yellow_ratio = np.count_nonzero(yellow)/area

    if red_ratio > 0.25:
        return "TARGET"
    elif yellow_ratio < 0.10:
        return "PLUS"
    elif yellow_ratio < 0.19:
        return "KEY"
    else:
        return "UPGRADE"


def analyse(img, name):
    out = []
    for r in range(3):
        for c in range(4):
            sym = classify_cell(crop_cell(img, r, c))
            out.append({"cell": f"R{r+1}C{c+1}", "symbol": sym})
    return out


# ===== SCORING =====
def add_to_totals(totals, rows):
    for r in rows:
        totals[r["cell"]][r["symbol"]] += 1


def score(totals):
    scored = []
    for cell, counts in totals.items():
        total = sum(counts.values())
        if not total:
            continue

        s = sum((counts[k]/total)*DEFAULT_WEIGHTS[k] for k in SYMBOLS)

        scored.append({
            "cell": cell,
            "score": s,
            "key_rate": counts["KEY"]/total,
            "counts": counts,
            "total": total
        })

    return sorted(scored, key=lambda x: x["score"], reverse=True)


def summary(scored, imgs, learned, dupes, total_imgs):
    best = scored[0]
    txt = [
        "**Chest Pattern**",
        f"New: {learned} | Dupes: {dupes} | Total DB: {total_imgs}",
        "",
        f"**Best:** {best['cell']} (score {best['score']:.2f})",
        "",
        "**Top:**"
    ]

    for x in scored[:5]:
        txt.append(f"{x['cell']} | 🔑 {x['counts']['KEY']}/{x['total']}")

    txt.append("\n**Avoid:**")
    for x in sorted(scored, key=lambda x: x["key_rate"], reverse=True)[:5]:
        txt.append(f"{x['cell']} | {x['key_rate']*100:.0f}% keys")

    return "\n".join(txt)


def make_csv(scored):
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(["cell","score","keys","total"])
    for x in scored:
        w.writerow([x["cell"], round(x["score"],3), x["counts"]["KEY"], x["total"]])
    return discord.File(io.BytesIO(s.getvalue().encode()), filename="chest.csv")


# ===== COG =====
class ChestPatternCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="chest_pattern", description="Analyse chest screenshots")
    async def chest_pattern(
        self,
        interaction: discord.Interaction,
        screenshot_1: discord.Attachment,
        screenshot_2: Optional[discord.Attachment] = None,
        screenshot_3: Optional[discord.Attachment] = None,
        screenshot_4: Optional[discord.Attachment] = None,
        screenshot_5: Optional[discord.Attachment] = None,
        screenshot_6: Optional[discord.Attachment] = None,
        screenshot_7: Optional[discord.Attachment] = None,
        screenshot_8: Optional[discord.Attachment] = None,
        screenshot_9: Optional[discord.Attachment] = None,
        screenshot_10: Optional[discord.Attachment] = None,
        screenshot_11: Optional[discord.Attachment] = None,
        screenshot_12: Optional[discord.Attachment] = None,
        screenshot_13: Optional[discord.Attachment] = None,
        screenshot_14: Optional[discord.Attachment] = None,
        screenshot_15: Optional[discord.Attachment] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        atts = [x for x in [
            screenshot_1,screenshot_2,screenshot_3,screenshot_4,screenshot_5,
            screenshot_6,screenshot_7,screenshot_8,screenshot_9,screenshot_10,
            screenshot_11,screenshot_12,screenshot_13,screenshot_14,screenshot_15
        ] if x]

        data = load_knowledge()
        gk = guild_key(interaction)
        if gk not in data["guilds"]:
            data["guilds"][gk] = fresh_guild()

        g = data["guilds"][gk]
        hashes = set(g["hashes"])

        new_rows = []
        learned = dupes = 0

        for att in atts:
            raw = await att.read()
            h = hashlib.sha256(raw).hexdigest()

            if h in hashes:
                dupes += 1
                continue

            img = cv2.imdecode(np.frombuffer(raw,np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue

            rows = analyse(img, att.filename)
            new_rows.extend(rows)

            hashes.add(h)
            g["hashes"].append(h)
            g["images"] += 1
            learned += 1

        if new_rows:
            add_to_totals(g["totals"], new_rows)
            save_knowledge(data)

        scored = score(g["totals"])

        await interaction.followup.send(
            summary(scored, len(atts), learned, dupes, g["images"]),
            file=make_csv(scored),
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(ChestPatternCog(bot))
