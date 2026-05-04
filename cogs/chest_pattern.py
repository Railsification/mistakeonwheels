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

BASE_WEIGHTS = {
    "UPGRADE": 2.5,
    "PLUS": 4.0,
    "TARGET": 1.0,
    "KEY": -14.0,
}

EXACT_WEIGHTS = {
    "UPGRADE": 3.0,
    "PLUS": 4.5,
    "TARGET": 0.5,
    "KEY": -18.0,
}


def import_image_libs():
    try:
        import cv2
        import numpy as np
        return cv2, np, None
    except Exception as e:
        return None, None, str(e)


def blank_symbol_counts():
    return {s: 0 for s in SYMBOLS}


def all_cells():
    return [f"R{r}C{c}" for r in range(1, 4) for c in range(1, 5)]


def blank_totals():
    return {cell: blank_symbol_counts() for cell in all_cells()}


def blank_order_stats():
    return {str(pos): blank_symbol_counts() for pos in range(1, 13)}


def blank_cell_order_stats():
    return {cell: blank_order_stats() for cell in all_cells()}


def fresh_guild_data():
    return {
        "totals": blank_totals(),
        "order_stats": blank_order_stats(),
        "cell_order_stats": blank_cell_order_stats(),
        "hashes": [],
        "images": 0,
        "last_order": [],
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
    g.setdefault("totals", blank_totals())
    g.setdefault("order_stats", blank_order_stats())
    g.setdefault("cell_order_stats", blank_cell_order_stats())
    g.setdefault("hashes", [])
    g.setdefault("images", 0)
    g.setdefault("last_order", [])

    for cell in all_cells():
        g["totals"].setdefault(cell, blank_symbol_counts())
        for s in SYMBOLS:
            g["totals"][cell].setdefault(s, 0)

        g["cell_order_stats"].setdefault(cell, blank_order_stats())
        for pos in range(1, 13):
            pos_key = str(pos)
            g["cell_order_stats"][cell].setdefault(pos_key, blank_symbol_counts())
            for s in SYMBOLS:
                g["cell_order_stats"][cell][pos_key].setdefault(s, 0)

    for pos in range(1, 13):
        pos_key = str(pos)
        g["order_stats"].setdefault(pos_key, blank_symbol_counts())
        for s in SYMBOLS:
            g["order_stats"][pos_key].setdefault(s, 0)

    if not isinstance(g["last_order"], list):
        g["last_order"] = []

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


def rows_to_cell_map(rows):
    return {row["cell"]: row["symbol"] for row in rows}


def add_rows_to_totals(totals, rows):
    for row in rows:
        totals[row["cell"]][row["symbol"]] += 1


def score_from_counts(counts, weights):
    total = sum(int(counts.get(s, 0)) for s in SYMBOLS)
    if total <= 0:
        return 0.0, 0

    score = 0.0
    for s in SYMBOLS:
        score += (int(counts.get(s, 0)) / total) * weights[s]
    return score, total


def add_exact_order_stats(guild_data, cell_map, prior_order):
    opened_count = sum(1 for symbol in cell_map.values() if symbol != "TARGET")
    if opened_count <= 0:
        return 0

    used_order = prior_order[:opened_count]
    for idx, cell in enumerate(used_order, start=1):
        symbol = cell_map.get(cell, "TARGET")
        guild_data["order_stats"][str(idx)][symbol] += 1
        guild_data["cell_order_stats"][cell][str(idx)][symbol] += 1

    return opened_count


def build_base_cell_stats(guild_data):
    stats = {}
    for cell in all_cells():
        counts = guild_data["totals"][cell]
        attempts = sum(int(counts.get(s, 0)) for s in SYMBOLS)
        opened = attempts - int(counts.get("TARGET", 0))

        base_score, _ = score_from_counts(counts, BASE_WEIGHTS)

        key = int(counts.get("KEY", 0))
        upgrade = int(counts.get("UPGRADE", 0))
        plus = int(counts.get("PLUS", 0))
        target = int(counts.get("TARGET", 0))

        key_rate_total = key / attempts if attempts else 0.0
        upgrade_rate_total = upgrade / attempts if attempts else 0.0
        plus_rate_total = plus / attempts if attempts else 0.0
        target_rate_total = target / attempts if attempts else 0.0

        stats[cell] = {
            "cell": cell,
            "attempts": attempts,
            "opened": opened,
            "key": key,
            "upgrade": upgrade,
            "plus": plus,
            "target": target,
            "key_rate_total": key_rate_total,
            "upgrade_rate_total": upgrade_rate_total,
            "plus_rate_total": plus_rate_total,
            "target_rate_total": target_rate_total,
            "base_score": base_score,
        }

    return stats


def score_cell_for_position(cell, pos, base_stats, guild_data):
    cell_base = base_stats[cell]["base_score"]

    pos_counts = guild_data["order_stats"][str(pos)]
    pos_score, pos_samples = score_from_counts(pos_counts, EXACT_WEIGHTS)

    cell_pos_counts = guild_data["cell_order_stats"][cell][str(pos)]
    cell_pos_score, cell_pos_samples = score_from_counts(cell_pos_counts, EXACT_WEIGHTS)

    score = cell_base

    if pos_samples > 0:
        score += pos_score * min(pos_samples / 20.0, 1.0) * 0.20

    if cell_pos_samples > 0:
        score += cell_pos_score * min(cell_pos_samples / 8.0, 1.0) * 0.55

    score -= base_stats[cell]["key_rate_total"] * 3.0

    return score, pos_samples, cell_pos_samples


def build_ranked_order(guild_data):
    base_stats = build_base_cell_stats(guild_data)
    remaining = set(all_cells())
    ranked = []

    for pos in range(1, 13):
        scored = []
        for cell in remaining:
            score, pos_samples, cell_pos_samples = score_cell_for_position(
                cell=cell,
                pos=pos,
                base_stats=base_stats,
                guild_data=guild_data,
            )
            row = dict(base_stats[cell])
            row["score"] = score
            row["position"] = pos
            row["position_samples"] = pos_samples
            row["cell_position_samples"] = cell_pos_samples
            scored.append(row)

        scored.sort(
            key=lambda x: (
                -x["score"],
                x["key_rate_total"],
                -x["target_rate_total"],
                -x["plus_rate_total"],
                -x["upgrade_rate_total"],
                x["cell"],
            )
        )

        chosen = scored[0]
        chosen["rank"] = pos
        ranked.append(chosen)
        remaining.remove(chosen["cell"])

    return ranked


def latest_remaining_cells(latest_rows):
    if not latest_rows:
        return set()
    return {row["cell"] for row in latest_rows if row["symbol"] == "TARGET"}


def build_summary(
    ranked,
    uploads,
    learned,
    dupes,
    total_images,
    bad_files,
    latest_rows,
    used_previous_order,
    exact_order_applied,
):
    lines = [
        "**Chest Pattern**",
        f"Uploads this run: `{uploads}`",
        f"Learned this run: `{learned}`",
        f"Duplicates skipped: `{dupes}`",
        f"Total knowledge base images: `{total_images}`",
        f"Used previous order toggle: `{'Yes' if used_previous_order else 'No'}`",
        f"Exact order inference used: `{'Yes' if exact_order_applied else 'No'}`",
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
            "**Remaining unopened from latest screenshot:**",
        ])
        for idx, row in enumerate(remaining_ranked, start=1):
            lines.append(f"{idx}. `{row['cell']}`")

    lines.extend([
        "",
        "**Top 6 detail:**",
    ])

    for row in ranked[:6]:
        lines.append(
            f"`{row['cell']}` | keys `{row['key']}/{max(row['attempts'], 1)}` | "
            f"unknown `{row['target']}` | +1 `{row['plus']}` | up `{row['upgrade']}` | "
            f"score `{row['score']:.2f}`"
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
        "key_rate_total",
        "upgrade_rate_total",
        "plus_rate_total",
        "target_rate_total",
        "score",
        "position_samples",
        "cell_position_samples",
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
            round(row["key_rate_total"], 4),
            round(row["upgrade_rate_total"], 4),
            round(row["plus_rate_total"], 4),
            round(row["target_rate_total"], 4),
            round(row["score"], 4),
            row["position_samples"],
            row["cell_position_samples"],
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
        used_previous_order="Did you follow the previous full 1-12 order exactly?",
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
        used_previous_order: bool,
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
        prior_order = list(guild_data.get("last_order", []))
        can_use_exact = used_previous_order and len(prior_order) == 12

        learned = 0
        dupes = 0
        bad_files = []
        latest_rows = []
        exact_order_applied = False

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

                if can_use_exact:
                    cell_map = rows_to_cell_map(current_rows)
                    add_exact_order_stats(guild_data, cell_map, prior_order)
                    exact_order_applied = True

                guild_data["hashes"].append(file_hash)
                guild_data["images"] += 1
                known_hashes.add(file_hash)
                learned += 1

            except Exception:
                bad_files.append(filename)

        ranked = build_ranked_order(guild_data)
        guild_data["last_order"] = [row["cell"] for row in ranked]

        if learned > 0:
            save_knowledge(data)

        summary = build_summary(
            ranked=ranked,
            uploads=len(attachments),
            learned=learned,
            dupes=dupes,
            total_images=guild_data["images"],
            bad_files=bad_files,
            latest_rows=latest_rows,
            used_previous_order=used_previous_order,
            exact_order_applied=exact_order_applied,
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
        ranked = build_ranked_order(guild_data)

        summary = build_summary(
            ranked=ranked,
            uploads=0,
            learned=0,
            dupes=0,
            total_images=guild_data["images"],
            bad_files=[],
            latest_rows=[],
            used_previous_order=False,
            exact_order_applied=False,
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
