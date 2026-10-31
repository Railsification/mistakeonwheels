# cogs/wos/chest_pattern.py
"""WoS Dead Shot (shooting mini-game) helper.

Game facts this model relies on (Century Games "How to Play Dead Shot"):
  * every stage has 12 targets (3 rows x 4 columns in the screenshot);
  * exactly two of them are Chest Keys and the stage ends on the second key;
  * every other target is a reward (Point Boost +1 or Point Upgrade).

So the only thing worth predicting is WHERE THE TWO KEYS ARE.  A finished
board gives complete key information: both keys are visible and every other
cell (opened or not) is known not to be a key.  Version 1 scored cells from
colour guesses, counted unopened cells as a positive outcome and ranked noise;
this version learns key positions only from confirmed boards, reports whether
the data shows any real pattern at all, and gives second-key odds once the
first key is found.

All version 1 data (totals, order stats, hashes, image count, last order) is
kept in the same file and the version 1 key counts are used as a half-weight
prior until enough confirmed boards exist.
"""
__version__ = "2.0.1"

import asyncio
import csv
import io
import json
import hashlib
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

from core.storage import load_guild_json, migrate_legacy_file_to_primary, save_guild_json

KNOWLEDGE_FILE = DATA_DIR / "chest_pattern_knowledge.json"
KNOWLEDGE_FILENAME = "chest_pattern_knowledge.json"

CELL_X = [0.135, 0.376, 0.616, 0.852]
CELL_Y = [0.547, 0.670, 0.792]
CROP_SIZE_REL = 0.18

VALID_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SYMBOLS = ["UPGRADE", "PLUS", "TARGET", "KEY"]

# Version 1 scoring weights. Kept for compatibility; no longer used to rank.
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

FEATURE_NAME = "chest_pattern"
POSITIONS = list(range(1, 13))
KEYS_PER_BOARD = 2
KEY_RATE = KEYS_PER_BOARD / 12.0
PRIOR_STRENGTH = 6.0          # pseudo-boards pulling each cell toward 2/12
PAIR_PRIOR_STRENGTH = 5.0     # pseudo-boards for second-key odds
LEGACY_WEIGHT = 0.5           # v1 colour-guess key counts are half as trusted
MAX_BOARDS = 5000
MAX_PENDING = 40
PENDING_DAYS = 14
MAX_SAMPLES = 600
MAX_DHASHES = 3000
MAX_IMAGE_BYTES = 15 * 1024 * 1024
LEARNED_MIN_KEY = 6
LEARNED_MIN_OTHER = 12
AUTO_SAVE_MIN_SAMPLES = 40
BOARD_ID = re.compile(r"^[0-9a-f]{12}$")


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


def pos_to_cell(pos):
    pos = int(pos)
    return f"R{(pos - 1) // 4 + 1}C{(pos - 1) % 4 + 1}"


def cell_to_pos(cell):
    m = re.fullmatch(r"R([1-3])C([1-4])", str(cell))
    if not m:
        return None
    return (int(m.group(1)) - 1) * 4 + int(m.group(2))


def blank_totals():
    return {cell: blank_symbol_counts() for cell in all_cells()}


def blank_order_stats():
    return {str(pos): blank_symbol_counts() for pos in range(1, 13)}


def blank_cell_order_stats():
    return {cell: blank_order_stats() for cell in all_cells()}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def fresh_guild_data():
    return {
        "totals": blank_totals(),
        "order_stats": blank_order_stats(),
        "cell_order_stats": blank_cell_order_stats(),
        "hashes": [],
        "images": 0,
        "last_order": [],
        "model": 2,
        "legacy": {"key_counts": {str(p): 0 for p in POSITIONS}, "images": 0},
        "boards": [],
        "pending": {},
        "samples": [],
        "dhashes": [],
    }


def load_knowledge(bot, guild_key: str):
    try:
        guild_id = int(guild_key)
    except Exception:
        return {"guilds": {guild_key: fresh_guild_data()}}

    migrate_legacy_file_to_primary(KNOWLEDGE_FILENAME, bot, {"guilds": {}})
    stored = load_guild_json(guild_id, KNOWLEDGE_FILENAME, {})

    if isinstance(stored, dict) and "guilds" in stored and isinstance(stored["guilds"], dict):
        guild_data = stored["guilds"].get(guild_key, fresh_guild_data())
    elif isinstance(stored, dict) and "totals" in stored:
        guild_data = stored
    else:
        guild_data = fresh_guild_data()

    return {"guilds": {guild_key: guild_data}}


def save_knowledge(bot, guild_key: str, data):
    try:
        guild_id = int(guild_key)
    except Exception:
        return
    guild_data = (data.get("guilds") or {}).get(guild_key, fresh_guild_data())
    save_guild_json(guild_id, KNOWLEDGE_FILENAME, guild_data)


def get_guild_key(interaction: discord.Interaction):
    return str(interaction.guild_id or "dm")


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ensure_guild_data(data, guild_key):
    data.setdefault("guilds", {})
    if guild_key not in data["guilds"] or not isinstance(data["guilds"][guild_key], dict):
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

    # --- Version 2 migration (additive; nothing from version 1 is removed) ---
    if g.get("model") != 2 or not isinstance(g.get("legacy"), dict):
        # Snapshot the version 1 colour-guess key counts once, so they can act
        # as a weak prior without being double counted by later boards.
        g["legacy"] = {
            "key_counts": {
                str(p): _int(g["totals"][pos_to_cell(p)].get("KEY", 0)) for p in POSITIONS
            },
            "images": _int(g.get("images", 0)),
            "migrated_utc": utcnow(),
        }
        g["model"] = 2
    g["legacy"].setdefault("key_counts", {})
    for p in POSITIONS:
        g["legacy"]["key_counts"].setdefault(str(p), 0)
    g["legacy"].setdefault("images", 0)
    for name, default in (("boards", list), ("samples", list), ("dhashes", list)):
        if not isinstance(g.get(name), list):
            g[name] = default()
    if not isinstance(g.get("pending"), dict):
        g["pending"] = {}

    return g


# ---------------------------------------------------------------------------
# Image reading
# ---------------------------------------------------------------------------

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
    """Version 1 colour heuristic. Still the fallback until enough confirmed
    boards exist to learn what keys look like on the users' own screens."""
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


def cell_features(cv2, np, crop):
    """32-value colour fingerprint of one cell (hue 8 x saturation 2 x value 2)."""
    if crop is None or crop.size == 0:
        return [0.0] * 32
    small = cv2.resize(crop, (24, 24), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 2, 2], [0, 180, 0, 256, 0, 256])
    hist = hist.flatten().astype("float64")
    total = float(hist.sum()) or 1.0
    return [round(float(v) / total, 4) for v in hist]


def board_dhash(cv2, img):
    """Perceptual fingerprint of the target area; catches re-screenshots of
    the same board that have different file bytes."""
    h = img.shape[0]
    region = img[int(h * 0.44):int(h * 0.88), :]
    if region.size == 0:
        region = img
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | int(small[y, x + 1] > small[y, x])
    return f"{bits:016x}"


def hamming_hex(a, b):
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def sample_counts(samples):
    key = sum(1 for s in samples if isinstance(s, dict) and s.get("label") == "KEY")
    other = sum(1 for s in samples if isinstance(s, dict) and s.get("label") == "NOT")
    return key, other


def learned_ready(samples):
    key, other = sample_counts(samples)
    return key >= LEARNED_MIN_KEY and other >= LEARNED_MIN_OTHER


def key_distances(np, feats, samples):
    """Per position: (mean distance to 3 nearest KEY samples,
    mean distance to 3 nearest non-key samples)."""
    key_rows = [s["f"] for s in samples if s.get("label") == "KEY" and len(s.get("f", [])) == 32]
    not_rows = [s["f"] for s in samples if s.get("label") == "NOT" and len(s.get("f", [])) == 32]
    key_m = np.array(key_rows, dtype="float64")
    not_m = np.array(not_rows, dtype="float64")
    out = {}
    for pos, f in feats.items():
        v = np.array(f, dtype="float64")
        dk = np.sort(np.abs(key_m - v).sum(axis=1))[:3].mean()
        dn = np.sort(np.abs(not_m - v).sum(axis=1))[:3].mean()
        out[int(pos)] = (float(dk), float(dn))
    return out


def detect_board(cv2, np, img, samples):
    """Read one finished-board screenshot.

    Returns symbols per position (1-12), the two proposed key positions, how
    they were found and whether the read is trustworthy enough to save
    without a human check.
    """
    rows = analyse_image(cv2, np, img)
    symbols = {cell_to_pos(r["cell"]): r["symbol"] for r in rows}
    feats = {}
    for r in range(3):
        for c in range(4):
            feats[r * 4 + c + 1] = cell_features(cv2, np, crop_cell(img, r, c))

    method = "colour"
    confident = False
    if learned_ready(samples):
        method = "learned"
        dist = key_distances(np, feats, samples)
        # Lower ratio = looks more like the confirmed keys.
        ratio = {p: dk / max(dn, 1e-6) for p, (dk, dn) in dist.items()}
        ranked = sorted(POSITIONS, key=lambda p: (ratio[p], p))
        keys = sorted(ranked[:2])
        clear_keys = all(ratio[p] < 0.8 for p in keys)
        clear_rest = all(ratio[p] > 1.25 for p in ranked[2:])
        confident = clear_keys and clear_rest and len(samples) >= AUTO_SAVE_MIN_SAMPLES
        for p in POSITIONS:
            if p in keys:
                symbols[p] = "KEY"
            elif symbols[p] == "KEY":
                # The colour rule called it a key but the learned reader did not;
                # its yellow level sits nearest the Point Boost band.
                symbols[p] = "PLUS"
    else:
        keys = sorted(p for p in POSITIONS if symbols[p] == "KEY")
        if len(keys) > 2:
            keys = keys[:2]

    return {
        "symbols": {str(p): symbols[p] for p in POSITIONS},
        "keys": keys,
        "method": method,
        "confident": bool(confident and len(keys) == 2),
        "feats": {str(p): feats[p] for p in POSITIONS},
    }


# ---------------------------------------------------------------------------
# Version 1 helpers (kept; legacy totals and order stats still maintained)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Version 2 key-position model
# ---------------------------------------------------------------------------

def valid_keys(keys):
    try:
        ks = sorted({int(k) for k in keys})
    except (TypeError, ValueError):
        return None
    if len(ks) != KEYS_PER_BOARD or any(k not in POSITIONS for k in ks):
        return None
    return ks


def verified_boards(guild_data):
    out = []
    for b in guild_data.get("boards", []):
        if isinstance(b, dict):
            ks = valid_keys(b.get("keys", []))
            if ks:
                out.append(ks)
    return out


def chi2_sf(x, df):
    """Survival function of the chi-square distribution (no SciPy needed)."""
    if x <= 0:
        return 1.0
    a, z = df / 2.0, x / 2.0
    gln = math.lgamma(a)
    if z < a + 1.0:
        term = total = 1.0 / a
        n = a
        for _ in range(500):
            n += 1.0
            term *= z / n
            total += term
            if abs(term) < abs(total) * 1e-12:
                break
        return max(0.0, min(1.0, 1.0 - total * math.exp(-z + a * math.log(z) - gln)))
    b = z + 1.0 - a
    c = 1.0 / 1e-300
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return max(0.0, min(1.0, math.exp(-z + a * math.log(z) - gln) * h))


def build_model(guild_data):
    boards = verified_boards(guild_data)
    n = len(boards)
    key_counts = {p: 0 for p in POSITIONS}
    pair = {p: {q: 0 for q in POSITIONS} for p in POSITIONS}
    for a, b in boards:
        key_counts[a] += 1
        key_counts[b] += 1
        pair[a][b] += 1
        pair[b][a] += 1

    legacy = guild_data.get("legacy", {}) or {}
    legacy_counts = {p: max(0, _int(legacy.get("key_counts", {}).get(str(p), 0))) for p in POSITIONS}
    legacy_images = max(0, _int(legacy.get("images", 0)))
    # A board can only ever show two keys; cap v1 boards that over-read keys.
    legacy_total_keys = sum(legacy_counts.values())
    if legacy_images and legacy_total_keys > legacy_images * KEYS_PER_BOARD:
        scale = legacy_images * KEYS_PER_BOARD / legacy_total_keys
        legacy_counts = {p: legacy_counts[p] * scale for p in POSITIONS}
    # The weak prior fades out as confirmed boards arrive.
    lw = LEGACY_WEIGHT if n < 60 else LEGACY_WEIGHT * 60.0 / n

    prob = {}
    for p in POSITIONS:
        num = key_counts[p] + lw * legacy_counts[p] + PRIOR_STRENGTH * KEY_RATE
        den = n + lw * legacy_images + PRIOR_STRENGTH
        prob[p] = num / den if den else KEY_RATE

    order = sorted(POSITIONS, key=lambda p: (prob[p], p))

    chi2 = None
    p_value = None
    if n >= 12:
        expected = n * KEYS_PER_BOARD / 12.0
        chi2 = sum((key_counts[p] - expected) ** 2 / expected for p in POSITIONS)
        p_value = chi2_sf(chi2, 11)

    return {
        "boards": n,
        "key_counts": key_counts,
        "pair": pair,
        "prob": prob,
        "order": order,
        "legacy_images": legacy_images,
        "legacy_weight": lw,
        "chi2": chi2,
        "p_value": p_value,
    }


def second_key_odds(model, first):
    """P(second key at j | first key found at `first`), for every other cell."""
    first = int(first)
    others = [p for p in POSITIONS if p != first]
    marg = {p: model["prob"][p] for p in others}
    total = sum(marg.values()) or 1.0
    prior = {p: marg[p] / total for p in others}
    seen = sum(model["pair"][first][p] for p in others)
    odds = {p: (model["pair"][first][p] + PAIR_PRIOR_STRENGTH * prior[p]) / (seen + PAIR_PRIOR_STRENGTH)
            for p in others}
    return odds, seen


def pattern_verdict(model):
    n = model["boards"]
    if model["p_value"] is None:
        need = max(0, 12 - n)
        return (f"Pattern check: needs {need} more confirmed board{'s' if need != 1 else ''} "
                "before it can say anything. Until then the order is mostly a guess.")
    pv = model["p_value"]
    if pv < 0.01:
        return (f"Pattern check: **strong evidence** keys favour certain spots "
                f"(p={pv:.3f}, {n} boards).")
    if pv < 0.05:
        return (f"Pattern check: **some evidence** keys favour certain spots "
                f"(p={pv:.3f}, {n} boards). Keep adding boards to confirm.")
    return (f"Pattern check: **no evidence of a pattern** so far (p={pv:.2f}, {n} boards) — "
            "key spots look random, so no order beats any other yet.")


def build_ranked_order(guild_data):
    """Safest-first order of all 12 cells.

    Same row shape as version 1 so the CSV export and existing callers keep
    working; score is now the estimated chance the cell is NOT a key.
    """
    base_stats = build_base_cell_stats(guild_data)
    model = build_model(guild_data)
    ranked = []
    for rank, pos in enumerate(model["order"], start=1):
        cell = pos_to_cell(pos)
        row = dict(base_stats[cell])
        row["score"] = 1.0 - model["prob"][pos]
        row["key_probability"] = model["prob"][pos]
        row["verified_keys"] = model["key_counts"][pos]
        row["verified_boards"] = model["boards"]
        row["position"] = pos
        row["rank"] = rank
        row["position_samples"] = model["boards"]
        row["cell_position_samples"] = model["key_counts"][pos]
        ranked.append(row)
    return ranked


def latest_remaining_cells(latest_rows):
    if not latest_rows:
        return set()
    return {row["cell"] for row in latest_rows if row["symbol"] == "TARGET"}


def odds_grid(values, mark=None):
    """3x4 monospace grid of percentages, numbered like the game (1-12)."""
    lines = []
    for r in range(3):
        cells = []
        for c in range(4):
            p = r * 4 + c + 1
            if mark and p in mark:
                cells.append(f"{p:>2} {mark[p]:>4}")
            elif p in values:
                cells.append(f"{p:>2} {round(values[p] * 100):>3}%")
            else:
                cells.append(f"{p:>2}   — ")
        lines.append("  ".join(cells))
    return "```\n" + "\n".join(lines) + "\n```"


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
    model=None,
):
    lines = ["**Dead Shot — key odds**"]
    if uploads:
        lines.append(f"Screenshots this run: `{uploads}` · saved `{learned}` · duplicates skipped `{dupes}`")

    if model is None:
        return "\n".join(lines + ["", "No usable data yet."])

    legacy = ""
    if model["legacy_images"]:
        legacy = f" + {model['legacy_images']} older auto-read scans at low weight"
    lines.append(f"Confirmed boards: `{model['boards']}`{legacy}")
    lines.append("Chance each target hides a key (lower = safer):")
    lines.append(odds_grid(model["prob"]))
    lines.append("**Shoot order (safest first):** " + " → ".join(str(p) for p in model["order"]))

    remaining = latest_remaining_cells(latest_rows)
    if remaining:
        rem = [p for p in model["order"] if pos_to_cell(p) in remaining]
        lines.append("Unopened in latest screenshot: " + ", ".join(str(p) for p in rem))

    lines.append(pattern_verdict(model))
    lines.append("Found your first key? Tap its number below (or `!ds <number>`) for the safest next shots.")

    if bad_files:
        lines.append("Skipped unreadable files: " + ", ".join(f"`{n}`" for n in bad_files[:10]))

    return "\n".join(lines)[:1990]


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
        "position",
        "key_probability",
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
            row.get("position", ""),
            round(row.get("key_probability", 0.0), 4),
        ])

    return discord.File(
        io.BytesIO(s.getvalue().encode("utf-8")),
        filename="chest_pattern_scores.csv",
    )


def followed_order(guild_data, model=None):
    """The shoot order `!ds` currently shows. It only changes when a board is
    recorded (between stages), so it is the order a player was following when
    they hit their first key."""
    return list((model or build_model(guild_data))["order"])


def second_key_text(model, first, shoot_order=None):
    """Second-key advice. With `shoot_order`, everything shot before the first
    key is treated as already opened (safe) and only the rest are ranked."""
    first = int(first)
    odds, seen = second_key_odds(model, first)
    opened = []
    if shoot_order and first in shoot_order:
        opened = list(shoot_order[:shoot_order.index(first)])
        left = {p: odds[p] for p in odds if p not in opened}
        total = sum(left.values()) or 1.0
        odds = {p: v / total for p, v in left.items()}
    order = sorted(odds, key=lambda p: (odds[p], p))
    mark = {first: "KEY"}
    mark.update({p: "open" for p in opened})
    lines = [f"**First key at {first}** — chance the second key is at each target left:"]
    if shoot_order:
        lines.append("Assuming you shot in the order shown: "
                     + (", ".join(str(p) for p in opened) + " already open." if opened
                        else "the key was your first shot."))
    lines.append(odds_grid(odds, mark=mark))
    values = sorted({round(v, 6) for v in odds.values()})
    if len(order) == 1:
        lines.append(f"**Only {order[0]} is left** — it must be the second key.")
    elif len(values) == 1:
        lines.append(f"**Shoot next:** {order[0]}. All {len(order)} targets left are equal "
                     f"({round(values[0] * 100)}% each) — any of them is as safe as another.")
    else:
        lines.append("**Shoot next (safest first):** " + " → ".join(str(p) for p in order))
    lines.append(
        f"Based on `{seen}` confirmed board{'s' if seen != 1 else ''} with a key at {first}"
        + (" — too few to trust; this is close to the overall odds." if seen < 10 else "."))
    lines.append(f"When the stage ends, record it: `!ds {first} <second key>`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Board recording (pure functions; saved by the cog under a lock)
# ---------------------------------------------------------------------------

def new_board_id(guild_data):
    used = set(guild_data.get("pending", {}))
    used.update(b.get("id") for b in guild_data.get("boards", []) if isinstance(b, dict))
    while True:
        bid = secrets.token_hex(6)
        if bid not in used:
            return bid


def prune_pending(guild_data, now=None):
    now = now or datetime.now(timezone.utc)
    pend = guild_data["pending"]
    for pid in list(pend):
        try:
            made = datetime.fromisoformat(pend[pid].get("t"))
        except (TypeError, ValueError, AttributeError):
            made = None
        if made is None or now - made > timedelta(days=PENDING_DAYS):
            pend.pop(pid, None)
    while len(pend) > MAX_PENDING:
        pend.pop(next(iter(pend)), None)


def find_board(guild_data, board_id):
    for b in guild_data["boards"]:
        if isinstance(b, dict) and b.get("id") == board_id:
            return b
    return None


def record_board(guild_data, pending, keys, author_id):
    """Turn a pending read into a confirmed board. Returns the board record."""
    keys = valid_keys(keys)
    if not keys:
        raise ValueError("Pick exactly two keys.")

    replaces = pending.get("replaces")
    if replaces:
        board = find_board(guild_data, replaces)
        if board is not None:
            old = board.get("symbols") or {}
            if board.get("totals_added") and old:
                new = {}
                for p in POSITIONS:
                    s = str(old.get(str(p), "TARGET"))
                    s = s if s in SYMBOLS else "TARGET"
                    if p in keys:
                        new[str(p)] = "KEY"
                    elif s == "KEY":
                        new[str(p)] = "PLUS"
                    else:
                        new[str(p)] = s
                for p in POSITIONS:
                    cell = pos_to_cell(p)
                    before = str(old.get(str(p), "TARGET"))
                    if before in SYMBOLS and guild_data["totals"][cell][before] > 0:
                        guild_data["totals"][cell][before] -= 1
                    guild_data["totals"][cell][new[str(p)]] += 1
                board["symbols"] = new
            board["keys"] = keys
            board["fixed_by"] = str(author_id)
            board["fixed_utc"] = utcnow()
            guild_data["last_order"] = [pos_to_cell(p) for p in build_model(guild_data)["order"]]
            return board

    symbols = {}
    for p in POSITIONS:
        s = str((pending.get("symbols") or {}).get(str(p), "TARGET"))
        s = s if s in SYMBOLS else "TARGET"
        if p in keys:
            s = "KEY"
        elif s == "KEY":
            s = "PLUS"
        symbols[str(p)] = s

    board = {
        "id": pending.get("id") or new_board_id(guild_data),
        "keys": keys,
        "symbols": symbols,
        "by": str(pending.get("by") or author_id),
        "t": utcnow(),
        "src": pending.get("src", "image"),
        "totals_added": True,
    }

    # Keep version 1 totals and order stats growing exactly as before.
    for p in POSITIONS:
        guild_data["totals"][pos_to_cell(p)][symbols[str(p)]] += 1
    prior_order = pending.get("exact_order")
    if isinstance(prior_order, list) and len(prior_order) == 12:
        add_exact_order_stats(
            guild_data, {pos_to_cell(p): symbols[str(p)] for p in POSITIONS}, prior_order)

    # Teach the reader what keys / non-keys look like on these screenshots.
    feats = pending.get("feats") or {}
    for p in POSITIONS:
        f = feats.get(str(p))
        if isinstance(f, list) and len(f) == 32:
            guild_data["samples"].append({"label": "KEY" if p in keys else "NOT", "f": f})
    if len(guild_data["samples"]) > MAX_SAMPLES:
        guild_data["samples"] = guild_data["samples"][-MAX_SAMPLES:]

    if pending.get("hash") and pending["hash"] not in guild_data["hashes"]:
        guild_data["hashes"].append(pending["hash"])
    if pending.get("dhash"):
        guild_data["dhashes"].append(pending["dhash"])
        guild_data["dhashes"] = guild_data["dhashes"][-MAX_DHASHES:]
    if board["src"] == "image":
        guild_data["images"] = _int(guild_data.get("images", 0)) + 1

    guild_data["boards"].append(board)
    if len(guild_data["boards"]) > MAX_BOARDS:
        guild_data["boards"] = guild_data["boards"][-MAX_BOARDS:]
    guild_data["last_order"] = [pos_to_cell(p) for p in build_model(guild_data)["order"]]
    return board


def remove_board(guild_data, board_id):
    board = find_board(guild_data, board_id)
    if board is None:
        return None
    guild_data["boards"].remove(board)
    if board.get("totals_added"):
        for p, s in (board.get("symbols") or {}).items():
            cell = pos_to_cell(p)
            if s in SYMBOLS and guild_data["totals"][cell][s] > 0:
                guild_data["totals"][cell][s] -= 1
    if board.get("src") == "image" and _int(guild_data.get("images", 0)) > 0:
        guild_data["images"] = _int(guild_data["images"]) - 1
    guild_data["last_order"] = [pos_to_cell(p) for p in build_model(guild_data)["order"]]
    return board


def board_grid(symbols, keys):
    icon = {"KEY": "🔑", "TARGET": "🎯", "PLUS": "⬆️", "UPGRADE": "⭐"}
    lines = []
    for r in range(3):
        row = []
        for c in range(4):
            p = r * 4 + c + 1
            s = "KEY" if p in keys else str((symbols or {}).get(str(p), "TARGET"))
            if s == "KEY" and p not in keys:
                s = "PLUS"
            row.append(f"`{p:>2}`{icon.get(s, '▫️')}")
        lines.append(" ".join(row))
    return "\n".join(lines)


def pending_text(pending, note=""):
    keys = pending.get("keys") or []
    head = "**Dead Shot board — check the keys**" if not pending.get("replaces") else \
        "**Dead Shot board — fixing a saved board**"
    lines = [head, board_grid(pending.get("symbols"), keys)]
    if len(keys) == 2:
        lines.append(f"Keys read at **{keys[0]}** and **{keys[1]}**. If that's right, press **Save**.")
    else:
        lines.append(f"Read {len(keys)} key(s). Tap the numbers so exactly **two** keys are red, then **Save**.")
    lines.append("Only finished boards (both keys found) can be saved.")
    if note:
        lines.append(note)
    return "\n".join(lines)


def saved_text(board, model, note=""):
    k = board["keys"]
    lines = [
        f"✅ **Saved Dead Shot board** — keys at **{k[0]}** and **{k[1]}**. "
        f"Confirmed boards: `{model['boards']}`.",
        "Shoot order now: " + " → ".join(str(p) for p in model["order"]),
    ]
    if note:
        lines.append(note)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Restart-safe buttons (state lives in the guild JSON, not in memory)
# ---------------------------------------------------------------------------

def _cog(interaction):
    return interaction.client.get_cog("ChestPatternCog")


class DeadShotBoardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ds:(?P<pid>[0-9a-f]{12}):(?P<act>k(?:[1-9]|1[0-2])|save|drop|fix)",
):
    def __init__(self, pid, act, label="·", style=discord.ButtonStyle.secondary, row=None):
        super().__init__(discord.ui.Button(
            label=label, style=style, custom_id=f"ds:{pid}:{act}", row=row))
        self.pid = pid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["pid"], match["act"])

    async def callback(self, interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Dead Shot helper is not loaded.", ephemeral=True)
            return
        await cog.handle_board_button(interaction, self.pid, self.act)


class DeadShotNextButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dsn:(?P<first>[1-9]|1[0-2])",
):
    def __init__(self, first, row=None):
        super().__init__(discord.ui.Button(
            label=str(first), style=discord.ButtonStyle.secondary,
            custom_id=f"dsn:{first}", row=row))
        self.first = int(first)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["first"]))

    async def callback(self, interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Dead Shot helper is not loaded.", ephemeral=True)
            return
        await cog.handle_next_button(interaction, self.first)


def pending_view(pid, keys):
    view = discord.ui.View(timeout=None)
    for p in POSITIONS:
        style = discord.ButtonStyle.danger if p in keys else discord.ButtonStyle.secondary
        view.add_item(DeadShotBoardButton(pid, f"k{p}", label=str(p), style=style, row=(p - 1) // 4))
    view.add_item(DeadShotBoardButton(pid, "save", label="Save", style=discord.ButtonStyle.success, row=3))
    view.add_item(DeadShotBoardButton(pid, "drop", label="Discard", style=discord.ButtonStyle.secondary, row=3))
    return view


def saved_view(board_id):
    view = discord.ui.View(timeout=None)
    view.add_item(DeadShotBoardButton(board_id, "fix", label="Fix keys", style=discord.ButtonStyle.secondary))
    return view


def helper_view():
    view = discord.ui.View(timeout=None)
    for p in POSITIONS:
        view.add_item(DeadShotNextButton(p, row=(p - 1) // 4))
    return view


PREFIX_HELP = (
    "**Dead Shot helper**\n"
    "`!ds` — key odds, safest shoot order and pattern check\n"
    "`!ds 7` — first key found at 7: safest next shots\n"
    "`!ds 3 10` — record a finished board (keys at 3 and 10), no screenshot needed\n"
    "`!ds undo` — remove the last board you recorded\n"
    "Screenshots: drop finished-board screenshots in the Dead Shot channel "
    "(council feature `chest_pattern`) or use `/chest_pattern`. Check the keys and press Save."
)


class ChestPatternCog(commands.Cog):
    HELP_META = {
        "title": "Dead Shot (chest pattern)",
        "summary": "Learns where Dead Shot keys appear and suggests the safest shoot order.",
        "goal": "Shoot all reward targets before the second key ends the stage.",
        "how_to_play": PREFIX_HELP,
        "rules": "Only finished boards with exactly two keys are learned. Each board counts once.",
        "details": "Data is per server and survives restarts. Pending checks keep their buttons after restarts.",
    }

    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()

    async def cog_unload(self):
        try:
            self.bot.remove_dynamic_items(DeadShotBoardButton, DeadShotNextButton)
        except Exception:
            pass

    # ----- storage helpers -----
    def _guild(self, guild_id):
        key = str(guild_id or "dm")
        data = load_knowledge(self.bot, key)
        return key, data, ensure_guild_data(data, key)

    def _save(self, key, data):
        save_knowledge(self.bot, key, data)

    @staticmethod
    def _can_edit(user, owner_id):
        perms = getattr(user, "guild_permissions", None)
        return (str(getattr(user, "id", "")) == str(owner_id)
                or bool(perms and (perms.manage_guild or perms.administrator)))

    # ----- reading screenshots -----
    async def _read_attachment(self, att, guild_data, author_id, cv2, np, exact_order=None):
        """Return ('dup'|'bad'|'saved'|'pending', payload)."""
        filename = att.filename or "unknown"
        if not any(filename.lower().endswith(ext) for ext in VALID_EXTS):
            return "bad", filename
        if getattr(att, "size", 0) and att.size > MAX_IMAGE_BYTES:
            return "bad", filename
        try:
            raw = await att.read()
        except Exception:
            return "bad", filename
        file_hash = hashlib.sha256(raw).hexdigest()
        if file_hash in set(guild_data["hashes"]):
            return "dup", filename
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return "bad", filename
        read = detect_board(cv2, np, img, guild_data["samples"])
        dhash = board_dhash(cv2, img)
        for old in guild_data["dhashes"][-MAX_DHASHES:]:
            if hamming_hex(old, dhash) <= 3:
                same = [b for b in guild_data["boards"][-400:]
                        if isinstance(b, dict) and b.get("keys") == read["keys"]]
                if same:
                    return "dup", filename

        pid = new_board_id(guild_data)
        pending = {
            "id": pid, "by": str(author_id), "t": utcnow(), "src": "image",
            "keys": read["keys"], "symbols": read["symbols"], "feats": read["feats"],
            "hash": file_hash, "dhash": dhash, "method": read["method"],
        }
        if isinstance(exact_order, list) and len(exact_order) == 12:
            pending["exact_order"] = list(exact_order)
        if read["confident"]:
            board = record_board(guild_data, pending, read["keys"], author_id)
            return "saved", board
        guild_data["pending"][pid] = pending
        prune_pending(guild_data)
        return "pending", pending

    async def _process_attachments(self, guild_id, author_id, attachments, exact=False):
        cv2, np, import_error = import_image_libs()
        if import_error:
            return None, import_error
        async with self._lock:
            key, data, g = self._guild(guild_id)
            prior = list(g.get("last_order", [])) if exact else None
            results = []
            for att in attachments:
                results.append(await self._read_attachment(att, g, author_id, cv2, np, prior))
            self._save(key, data)
            model = build_model(g)
        return (results, model), None

    # ----- slash commands (names and parameters unchanged) -----
    @app_commands.command(name="chest_pattern", description="Privately analyse Dead Shot screenshots")
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
        attachments = [a for a in (
            screenshot_1, screenshot_2, screenshot_3, screenshot_4, screenshot_5,
            screenshot_6, screenshot_7, screenshot_8, screenshot_9, screenshot_10,
        ) if a is not None]

        out, error = await self._process_attachments(
            interaction.guild_id, interaction.user.id, attachments, exact=used_previous_order)
        if error:
            await interaction.followup.send(f"Image libs failed to load:\n`{error}`", ephemeral=True)
            return
        await self._send_results(interaction.followup.send, out, len(attachments), ephemeral=True)

    async def _send_results(self, send, out, uploads, ephemeral=False):
        results, model = out
        saved = [p for kind, p in results if kind == "saved"]
        pending = [p for kind, p in results if kind == "pending"]
        dupes = sum(1 for kind, _ in results if kind == "dup")
        bad = [p for kind, p in results if kind == "bad"]
        kw = {"ephemeral": True} if ephemeral else {}

        for board in saved:
            await send(saved_text(board, model, "Read automatically from the screenshot."),
                       view=saved_view(board["id"]), **kw)
        for p in pending:
            await send(pending_text(p), view=pending_view(p["id"], p["keys"]), **kw)

        notes = []
        if dupes:
            notes.append(f"Skipped {dupes} screenshot{'s' if dupes != 1 else ''} already recorded.")
        if bad:
            notes.append("Could not read: " + ", ".join(f"`{n}`" for n in bad[:10]))
        if notes or not (saved or pending):
            await send("\n".join(notes) or "Nothing to record.", **kw)

    @app_commands.command(name="chest_stats", description="Private Dead Shot key odds and shoot order")
    async def chest_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, _, g = self._guild(interaction.guild_id)
        ranked = build_ranked_order(g)
        summary = build_summary(
            ranked=ranked, uploads=0, learned=0, dupes=0, total_images=g["images"],
            bad_files=[], latest_rows=[], used_previous_order=False,
            exact_order_applied=False, model=build_model(g))
        await interaction.followup.send(content=summary, file=make_csv(ranked),
                                        view=helper_view(), ephemeral=True)

    @app_commands.command(name="chest_reset", description="Private chest reset")
    async def chest_reset(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if interaction.guild and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("Admin only.", ephemeral=True)
            return

        guild_key = get_guild_key(interaction)
        async with self._lock:
            data = load_knowledge(self.bot, guild_key)
            data.setdefault("guilds", {})
            data["guilds"][guild_key] = fresh_guild_data()
            save_knowledge(self.bot, guild_key, data)

        await interaction.followup.send("Chest knowledge reset.", ephemeral=True)

    # ----- buttons -----
    async def handle_board_button(self, interaction, pid, act):
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        content = view = None
        error = None
        async with self._lock:
            key, data, g = self._guild(interaction.guild_id)
            if act == "fix":
                board = find_board(g, pid)
                if board is None:
                    error = "That board is no longer stored."
                elif not self._can_edit(interaction.user, board.get("by")):
                    error = "Only the person who recorded it or a server manager can fix it."
                else:
                    edit_id = new_board_id(g)
                    pend = {"id": edit_id, "replaces": pid, "by": board.get("by"), "t": utcnow(),
                            "src": board.get("src", "image"), "keys": list(board["keys"]),
                            "symbols": board.get("symbols") or {}}
                    g["pending"][edit_id] = pend
                    prune_pending(g)
                    self._save(key, data)
                    content, view = pending_text(pend), pending_view(edit_id, pend["keys"])
            else:
                pend = g["pending"].get(pid)
                if pend is None:
                    error = "This check was already saved, discarded or expired."
                elif not self._can_edit(interaction.user, pend.get("by")):
                    error = "Only the person who posted it or a server manager can change it."
                elif act == "drop":
                    g["pending"].pop(pid, None)
                    self._save(key, data)
                    content, view = "Discarded — nothing was recorded.", None
                elif act == "save":
                    if not valid_keys(pend.get("keys", [])):
                        content, view = pending_text(pend, "⚠️ Exactly two keys are needed."), \
                            pending_view(pid, pend.get("keys", []))
                    else:
                        board = record_board(g, pend, pend["keys"], interaction.user.id)
                        g["pending"].pop(pid, None)
                        self._save(key, data)
                        content, view = saved_text(board, build_model(g)), saved_view(board["id"])
                else:
                    pos = int(act[1:])
                    keys = list(pend.get("keys", []))
                    if pos in keys:
                        keys.remove(pos)
                    elif len(keys) < 2:
                        keys.append(pos)
                    else:
                        keys = [keys[-1], pos]
                    pend["keys"] = sorted(keys)
                    self._save(key, data)
                    content, view = pending_text(pend), pending_view(pid, pend["keys"])
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.edit_message(content=content, view=view)

    async def handle_next_button(self, interaction, first):
        _, _, g = self._guild(interaction.guild_id)
        model = build_model(g)
        await interaction.response.send_message(
            second_key_text(model, first, followed_order(g, model)), ephemeral=True)

    # ----- channel drop: screenshots posted in the Dead Shot channel -----
    @commands.Cog.listener("on_message")
    async def dead_shot_channel_scan(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.attachments:
            return
        settings = getattr(self.bot, "settings", None)
        if settings is None or not settings.is_feature_allowed(
                message.guild.id, message.channel.id, FEATURE_NAME):
            return
        if (message.content or "").startswith("!"):
            return
        images = [a for a in message.attachments
                  if any((a.filename or "").lower().endswith(e) for e in VALID_EXTS)][:10]
        if not images:
            return
        out, error = await self._process_attachments(message.guild.id, message.author.id, images)
        if error:
            await message.reply(f"Image reader unavailable: `{error}`", mention_author=False)
            return

        async def send(content, view=None, **_):
            if view is None:
                return await message.reply(content, mention_author=False)
            return await message.reply(content, view=view, mention_author=False)

        await self._send_results(send, out, len(images))

    # ----- !ds prefix command -----
    @commands.command(name="ds", aliases=["deadshot", "chest"])
    @commands.guild_only()
    async def ds_prefix(self, ctx: commands.Context, *args: str):
        none = discord.AllowedMentions.none()
        words = [a.strip().lower() for a in args if a.strip()]
        if words and words[0] in ("help", "commands", "?"):
            await ctx.send(PREFIX_HELP, allowed_mentions=none)
            return
        if words and words[0] in ("after", "next", "first"):
            words = words[1:]
        if words and words[0] == "undo":
            async with self._lock:
                key, data, g = self._guild(ctx.guild.id)
                mine = [b for b in g["boards"] if isinstance(b, dict) and b.get("by") == str(ctx.author.id)]
                if not mine:
                    msg = "You have no recorded boards to undo."
                else:
                    remove_board(g, mine[-1]["id"])
                    self._save(key, data)
                    msg = (f"Removed your last board (keys {mine[-1]['keys'][0]} and {mine[-1]['keys'][1]}). "
                           f"Confirmed boards: `{build_model(g)['boards']}`.")
            await ctx.send(msg, allowed_mentions=none)
            return

        nums = [w for w in words if w.isdigit()]
        if words and len(nums) != len(words):
            await ctx.send(PREFIX_HELP, allowed_mentions=none)
            return
        nums = [int(n) for n in nums]
        if any(n not in POSITIONS for n in nums) or len(nums) > 2:
            await ctx.send("Targets are numbered 1–12 (rows left→right, top→bottom).", allowed_mentions=none)
            return

        if len(nums) == 2:
            keys = valid_keys(nums)
            if not keys:
                await ctx.send("The two keys must be different targets.", allowed_mentions=none)
                return
            async with self._lock:
                key, data, g = self._guild(ctx.guild.id)
                board = record_board(g, {"by": str(ctx.author.id), "src": "manual",
                                         "id": new_board_id(g)}, keys, ctx.author.id)
                self._save(key, data)
                model = build_model(g)
            await ctx.send(saved_text(board, model), view=saved_view(board["id"]), allowed_mentions=none)
            return

        _, _, g = self._guild(ctx.guild.id)
        model = build_model(g)
        if len(nums) == 1:
            await ctx.send(second_key_text(model, nums[0], followed_order(g, model)),
                           allowed_mentions=none)
            return
        summary = build_summary(
            ranked=[], uploads=0, learned=0, dupes=0, total_images=g["images"], bad_files=[],
            latest_rows=[], used_previous_order=False, exact_order_applied=False, model=model)
        await ctx.send(summary, view=helper_view(), allowed_mentions=none)


async def setup(bot):
    from core.command_scope import bind_public_cog

    cog = ChestPatternCog(bot)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
    # Re-attach every Dead Shot button (pending checks, Fix, first-key helper)
    # after a restart; their state is read from storage when pressed.
    bot.add_dynamic_items(DeadShotBoardButton, DeadShotNextButton)
