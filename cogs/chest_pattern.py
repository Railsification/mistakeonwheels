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
        "runs": [],
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
    g.setdefault("runs", [])

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


def rows_to_run_record(file_hash, rows):
    cell_map = {row["cell"]: row["symbol"] for row in rows}
    opened_count = sum(1 for symbol in cell_map.values() if symbol != "TARGET")
    unopened_count = 12 - opened_count
    return {
        "hash": file_hash,
        "opened_count": opened_count,
        "unopened_count": unopened_count,
        "cells": cell_map,
    }


def build_cell_stats(guild_data):
    totals = guild_data["totals"]
    runs = guild_data.get("runs", [])
    stats = []

    for cell, counts in totals.items():
        attempts = sum(int(counts.get(s, 0)) for s in SYMBOLS)

        key = int(counts.get("KEY", 0))
        upgrade = int(counts.get("UPGRADE", 0))
        plus = int(counts.get("PLUS", 0))
        target = int(counts.get("TARGET", 0))
        opened = attempts - target

        key_rate_opened = key / opened if opened else 1.0
        upgrade_rate_opened = upgrade / opened if opened else 0.0
        plus_rate_opened = plus / opened if opened else 0.0
        target_rate_total = target / attempts if attempts else 1.0
        opened_rate_total = opened / attempts if attempts else 0.0

        opened_depths = []
        unopened_penalties = []

        if runs:
            for run in runs:
                run_cells = run.get("cells", {})
                symbol = run_cells.get(cell, "TARGET")
                opened_count = int(run.get("opened_count", 0))
                unopened_count = int(run.get("unopened_count", 12))

                if symbol == "TARGET":
                    # Worse if this cell stayed unopened when lots were still unopened.
                    unopened_penalties.append(unopened_count / 12.0)
                else:
                    # Approximation: cell was opened by the time the run ended.
                    # Lower end-depth means likely earlier / safer.
                    opened_depths.append(opened_count)

        if opened_depths:
            avg_opened_depth = sum(opened_depths) / len(opened_depths)
            early_bonus = (12.0 - avg_opened_depth) / 11.0
        else:
            avg_opened_depth = 12.0
            early_bonus = 0.0

        if unopened_penalties:
            unopened_badness = sum(unopened_penalties) / len(unopened_penalties)
        else:
            unopened_badness = 0.0

        value_rate_opened = (upgrade_rate_opened * 1.0) + (plus_rate_opened * 1.25)

        # Locked scoring:
        # - keys are very bad
        # - being frequently opened is good
        # - being opened in shallower runs is good
        # - staying unopened when many are unopened is bad
        # - upgrades/+1 help, but not more than key safety
        score = (
            (value_rate_opened * 3.0)
            + (opened_rate_total * 1.5)
            + (early_bonus * 1.5)
            - (key_rate_opened * 12.0)
            - (unopened_badness * 2.5)
        )

        stats.append({
            "cell": cell,
            "attempts": attempts,
            "opened": opened,
            "key": key,
            "upgrade": upgrade,
            "plus": plus,
            "target": target,
            "key_rate_opened": key_rate_opened,
            "upgrade_rate_opened": upgrade_rate_opened,
            "plus_rate_opened": plus_rate_opened,
            "target_rate_total": target_rate_total,
            "opened_rate_total": opened_rate_total,
            "avg_opened_depth": avg_opened_depth,
            "early_bonus": early_bonus,
            "unopened_badness": unopened_badness,
            "score": score,
        })

    return stats


def rank_cells(stats):
    ranked = sorted(
        stats,
        key=lambda x: (
            -x["score"],
            x["key_rate_opened"],
            x["key"],
            x["unopened_badness"],
            -x["plus_rate_opened"],
            -x["upgrade_rate_opened"],
            x["cell"],
        ),
    )

    for i, row in enumerate(ranked, start=1):
        row["rank"] = i

    return ranked


def latest_remaining_cells(latest_rows):
    if not latest_rows:
        return set()
    return {row["cell"] for row in latest_rows if row["symbol"] == "TARGET"}


def build_summary(ranked, uploads, learned, dupes, total_images, bad_files, latest_rows):
    lines = [
        "**Chest Pattern**",
        f"Uploads this run: `{uploads}`",
        f"Learned this run: `{learned}`",
        f"Duplicates skipped: `{dupes}`",
        f"Total knowledge base images: `{total_images}`",
    ]

    if not ranked:
        lines.append("")
        lines.append("No usable data yet.")
        return "\n".join(lines)

    best = ranked[0]
    remaining = latest_remaining_cells(latest_rows)
    remaining_ranked = [row for row in ranked if row["cell"] in remaining]

    lines.extend([
        "",
        f"**Best pick now:** `{best['cell']}`",
        "",
        "**Full order 1-12:**",
    ])

    for row in ranked:
        lines.append(f"{row['rank']}. `{row['cell']}`")

    if remaining_ranked:
        lines.extend([
            "",
            "**Remaining unopened from last uploaded screenshot:**",
        ])
        for i, row in enumerate(remaining_ranked, start=1):
            lines.append(f"{i}. `{row['cell']}`")

    lines.extend([
        "",
        "**Top 6 detail:**",
    ])

    for row in ranked[:6]:
        lines.append(
            f"`{row['cell']}` | keys `{row['key']}/{max(row['opened'], 1)}` | "
            f"unknown `{row['target']}` | +1 `{row['plus']}` | up `{row['upgrade']}` | "
            f"depth `{row['avg_opened_depth']:.2f}` | unopened-bad `{row['unopened_badness']:.2f}`"
        )

    if bad_files:
        lines.extend([
            "",
            "Skipped unreadable files:",
        ])
        for name in bad_files:
            lines.append(f"- `{name}`")

    lines.extend([
        "",
        "Rows top->bottom. Columns left->right.",
        "Order-aware = approximate from how many cells were already opened when the run ended.",
    ])

    return "\n".join(lines)


def make_csv(ranked):
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow([
        "rank",
        "cell",
        "attempts",
        "opened",
        "key",
        "upgrade",
        "plus",
        "target",
        "key_rate_opened",
        "upgrade_rate_opened",
        "plus_rate_opened",
        "target_rate_total",
        "opened_rate_total",
        "avg_opened_depth",
        "early_bonus",
        "unopened_badness",
        "score",
    ])

    for row in ranked:
        w.writerow([
            row["rank"],
            row["cell"],
            row["attempts"],
            row["opened"],
            row["key"],
            row["upgrade"],
            row["plus"],
            row["target"],
            round(row["key_rate_opened"], 4),
            round(row["upgrade_rate_opened"], 4),
            round(row["plus_rate_opened"], 4),
            round(row["target_rate_total"], 4),
            round(row["opened_rate_total"], 4),
            round(row["avg_opened_depth"], 4),
            round(row["early_bonus"], 4),
            round(row["unopened_badness"], 4),
            round(row["score"], 4),
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
            screenshot_1,
            screenshot_2,
            screenshot_3,
            screenshot_4,
            screenshot_5,
            screenshot_6,
            screenshot_7,
            screenshot_8,
            screenshot_9,
            screenshot_10,
        ]
        attachments = [a for a in attachments if a is not None]

        data = load_knowledge()
        guild_data = ensure_guild_data(data, get_guild_key(interaction))

        known_hashes = set(guild_data["hashes"])
        learned = 0
        dupes = 0
        bad_files = []
        latest_rows = []

        for att in attachments:
            filename = att.filename or "unknown"

            if not any(filename.lower().endswith(ext) for ext in VALID_EXTS):
                bad_files.append(filename)
                continue

            try:
                raw = await att.read()
                file_hash = hashlib.sha256(raw).hexdigest()

                arr = np.frombuffer(raw, np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)

                if img is None:
                    bad_files.append(filename)
                    continue

                current_rows = analyse_image(cv2, np, img)
                latest_rows = current_rows

                if file_hash in known_hashes:
                    dupes += 1
                    continue

                add_rows_to_totals(guild_data["totals"], current_rows)
                guild_data["runs"].append(rows_to_run_record(file_hash, current_rows))
                guild_data["hashes"].append(file_hash)
                guild_data["images"] += 1
                known_hashes.add(file_hash)
                learned += 1

            except Exception:
                bad_files.append(filename)

        if learned > 0:
            save_knowledge(data)

        stats = build_cell_stats(guild_data)
        ranked = rank_cells(stats)
        summary = build_summary(
            ranked=ranked,
            uploads=len(attachments),
            learned=learned,
            dupes=dupes,
            total_images=guild_data["images"],
            bad_files=bad_files,
            latest_rows=latest_rows,
        )

        if ranked:
            await interaction.followup.send(
                content=summary,
                file=make_csv(ranked),
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
        guild_data = ensure_guild_data(data, get_guild_key(interaction))
        stats = build_cell_stats(guild_data)
        ranked = rank_cells(stats)

        summary = build_summary(
            ranked=ranked,
            uploads=0,
            learned=0,
            dupes=0,
            total_images=guild_data["images"],
            bad_files=[],
            latest_rows=[],
        )

        if ranked:
            await interaction.followup.send(
                content=summary,
                file=make_csv(ranked),
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
