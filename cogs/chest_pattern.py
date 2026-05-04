import os
import csv
import io
import json
import hashlib
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
KNOWLEDGE_FILE = DATA_DIR / "chest_pattern_knowledge.json"

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


def import_image_libs():
    try:
        import cv2
        import numpy as np
        return cv2, np, None
    except Exception as e:
        return None, None, str(e)


def fresh_guild_data():
    totals = {}
    for r in range(1, 4):
        for c in range(1, 5):
            totals[f"R{r}C{c}"] = {s: 0 for s in SYMBOLS}
    return {
        "totals": totals,
        "hashes": [],
        "images": 0,
    }


def load_knowledge():
    if not KNOWLEDGE_FILE.exists():
        return {"guilds": {}}

    try:
        with KNOWLEDGE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"guilds": {}}
        if "guilds" not in data or not isinstance(data["guilds"], dict):
            data["guilds"] = {}
        return data
    except Exception:
        return {"guilds": {}}


def save_knowledge(data):
    tmp = KNOWLEDGE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(KNOWLEDGE_FILE)


def get_guild_key(interaction: discord.Interaction):
    return str(interaction.guild_id or "dm")


def ensure_guild_data(data, guild_key):
    data.setdefault("guilds", {})
    if guild_key not in data["guilds"]:
        data["guilds"][guild_key] = fresh_guild_data()

    g = data["guilds"][guild_key]
    g.setdefault("totals", {})
    g.setdefault("hashes", [])
    g.setdefault("images", 0)

    for r in range(1, 4):
        for c in range(1, 5):
            cell = f"R{r}C{c}"
            if cell not in g["totals"]:
                g["totals"][cell] = {s: 0 for s in SYMBOLS}
            else:
                for s in SYMBOLS:
                    g["totals"][cell].setdefault(s, 0)

    return g


def crop_cell(img, row, col):
    h, w = img.shape[:2]
    cx = int(w * CELL_X[col])
    cy = int(h * CELL_Y[row])
    size = max(int(w * CROP_SIZE_REL), 8)
    half = size // 2

    x1 = max(cx - half, 0)
    x2 = min(cx + half, w)
    y1 = max(cy - half, 0)
    y2 = min(cy + half, h)

    return img[y1:y2, x1:x2]


def classify_cell(cv2, np, crop):
    if crop is None or crop.size == 0:
        return "TARGET"

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    area = max(crop.shape[0] * crop.shape[1], 1)

    red_1 = cv2.inRange(hsv, (0, 80, 100), (12, 255, 255))
    red_2 = cv2.inRange(hsv, (170, 80, 100), (180, 255, 255))
    red_mask = red_1 | red_2

    yellow_mask = cv2.inRange(hsv, (15, 80, 120), (45, 255, 255))

    red_ratio = float(np.count_nonzero(red_mask)) / area
    yellow_ratio = float(np.count_nonzero(yellow_mask)) / area

    if red_ratio > 0.25:
        return "TARGET"
    if yellow_ratio < 0.10:
        return "PLUS"
    if yellow_ratio < 0.19:
        return "KEY"
    return "UPGRADE"


def analyse_image(cv2, np, img):
    rows = []
    for r in range(3):
        for c in range(4):
            crop = crop_cell(img, r, c)
            symbol = classify_cell(cv2, np, crop)
            rows.append({
                "cell": f"R{r + 1}C{c + 1}",
                "symbol": symbol,
            })
    return rows


def add_rows_to_totals(totals, rows):
    for row in rows:
        totals[row["cell"]][row["symbol"]] += 1


def score_totals(totals):
    scored = []

    for cell, counts in totals.items():
        total = sum(int(counts.get(s, 0)) for s in SYMBOLS)
        if total <= 0:
            continue

        score = 0.0
        for s in SYMBOLS:
            score += (int(counts.get(s, 0)) / total) * DEFAULT_WEIGHTS[s]

        scored.append({
            "cell": cell,
            "score": score,
            "total": total,
            "counts": {
                "UPGRADE": int(counts.get("UPGRADE", 0)),
                "PLUS": int(counts.get("PLUS", 0)),
                "TARGET": int(counts.get("TARGET", 0)),
                "KEY": int(counts.get("KEY", 0)),
            },
            "key_rate": int(counts.get("KEY", 0)) / total,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def build_summary(scored, uploads, learned, dupes, total_images, bad_files):
    lines = [
        "**Chest Pattern**",
        f"Uploads this run: `{uploads}`",
        f"Learned this run: `{learned}`",
        f"Duplicates skipped: `{dupes}`",
        f"Total knowledge base images: `{total_images}`",
    ]

    if not scored:
        lines.append("")
        lines.append("No usable pattern data yet.")
    else:
        best = scored[0]
        top = scored[:5]
        avoid = sorted(scored, key=lambda x: x["key_rate"], reverse=True)[:5]

        lines.extend([
            "",
            f"**Best pick now:** `{best['cell']}`",
            f"Score: `{best['score']:.2f}` | Keys: `{best['counts']['KEY']}/{best['total']}`",
            "",
            "**Top cells:**",
        ])

        for row in top:
            lines.append(
                f"`{row['cell']}` | score `{row['score']:.2f}` | "
                f"🔑 `{row['counts']['KEY']}/{row['total']}` | "
                f"⬆️ `{row['counts']['UPGRADE']}` | +1 `{row['counts']['PLUS']}` | 🎯 `{row['counts']['TARGET']}`"
            )

        lines.append("")
        lines.append("**Avoid / key-heavy:**")

        for row in avoid:
            lines.append(
                f"`{row['cell']}` | keys `{row['counts']['KEY']}/{row['total']}` "
                f"({row['key_rate'] * 100:.0f}%)"
            )

    if bad_files:
        lines.append("")
        lines.append("Skipped unreadable files:")
        for name in bad_files:
            lines.append(f"- `{name}`")

    lines.append("")
    lines.append("Rows/cols counted from top-left.")
    lines.append("KEY is treated as bad.")

    return "\n".join(lines)


def make_csv(scored):
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(["cell", "score", "keys", "upgrade", "plus", "target", "total", "key_rate"])

    for row in scored:
        w.writerow([
            row["cell"],
            round(row["score"], 4),
            row["counts"]["KEY"],
            row["counts"]["UPGRADE"],
            row["counts"]["PLUS"],
            row["counts"]["TARGET"],
            row["total"],
            round(row["key_rate"], 4),
        ])

    return discord.File(
        io.BytesIO(s.getvalue().encode("utf-8")),
        filename="chest_pattern_scores.csv",
    )


class ChestPatternCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="chest_pattern", description="Privately analyse chest screenshots")
    @app_commands.describe(
        screenshot_1="Screenshot 1",
        screenshot_2="Screenshot 2",
        screenshot_3="Screenshot 3",
        screenshot_4="Screenshot 4",
        screenshot_5="Screenshot 5",
        screenshot_6="Screenshot 6",
        screenshot_7="Screenshot 7",
        screenshot_8="Screenshot 8",
        screenshot_9="Screenshot 9",
        screenshot_10="Screenshot 10",
    )
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
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        cv2, np, import_error = import_image_libs()
        if import_error:
            await interaction.followup.send(
                f"Image libs failed to load:\n`{import_error}`",
                ephemeral=True,
            )
            return

        attachments = [
            screenshot_1, screenshot_2, screenshot_3, screenshot_4, screenshot_5,
            screenshot_6, screenshot_7, screenshot_8, screenshot_9, screenshot_10,
        ]
        attachments = [a for a in attachments if a is not None]

        data = load_knowledge()
        g = ensure_guild_data(data, get_guild_key(interaction))

        known_hashes = set(g["hashes"])
        learned = 0
        dupes = 0
        bad_files = []
        new_rows = []

        for att in attachments:
            filename = att.filename or "unknown"

            if not any(filename.lower().endswith(ext) for ext in VALID_EXTS):
                bad_files.append(filename)
                continue

            try:
                raw = await att.read()
                file_hash = hashlib.sha256(raw).hexdigest()

                if file_hash in known_hashes:
                    dupes += 1
                    continue

                arr = np.frombuffer(raw, np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)

                if img is None:
                    bad_files.append(filename)
                    continue

                rows = analyse_image(cv2, np, img)
                new_rows.extend(rows)

                known_hashes.add(file_hash)
                g["hashes"].append(file_hash)
                g["images"] += 1
                learned += 1

            except Exception:
                bad_files.append(filename)

        if new_rows:
            add_rows_to_totals(g["totals"], new_rows)
            save_knowledge(data)

        scored = score_totals(g["totals"])
        summary = build_summary(scored, len(attachments), learned, dupes, g["images"], bad_files)

        if scored:
            await interaction.followup.send(
                content=summary,
                file=make_csv(scored),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                content=summary,
                ephemeral=True,
            )

    @app_commands.command(name="chest_stats", description="Private chest stats")
    async def chest_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        data = load_knowledge()
        g = ensure_guild_data(data, get_guild_key(interaction))
        scored = score_totals(g["totals"])
        summary = build_summary(scored, 0, 0, 0, g["images"], [])

        if scored:
            await interaction.followup.send(
                content=summary,
                file=make_csv(scored),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                content=summary,
                ephemeral=True,
            )

    @app_commands.command(name="chest_reset", description="Private chest reset")
    async def chest_reset(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if interaction.guild and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("Admin only.", ephemeral=True)
            return

        data = load_knowledge()
        data.setdefault("guilds", {})
        data["guilds"][get_guild_key(interaction)] = fresh_guild_data()
        save_knowledge(data)

        await interaction.followup.send("Chest knowledge reset.", ephemeral=True)


async def setup(bot):
    cog = ChestPatternCog(bot)
    if GUILD_ID:
        await bot.add_cog(cog, guild=discord.Object(id=GUILD_ID))
    else:
        await bot.add_cog(cog)
