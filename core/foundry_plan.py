# core/foundry_plan.py
"""Pure, storage-independent model for the WoS Foundry assignment planner."""
from __future__ import annotations

from copy import deepcopy

__version__ = "1.0.0"

ATTACKER_SLOTS = tuple(f"a{i}" for i in range(1, 8))
MAIN_SLOTS = ATTACKER_SLOTS[:4]
BACKUP_SLOTS = ATTACKER_SLOTS[4:]
SUPPORT_SLOTS = tuple(f"s{i}" for i in range(1, 9))
ALL_SLOTS = ATTACKER_SLOTS + SUPPORT_SLOTS

BUILDING_NAMES = {
    "prototype_1": "PROTOTYPE 1",
    "prototype_2": "PROTOTYPE 2",
    "boiler": "BOILER",
    "transit": "TRANSIT",
    "repair_1": "REPAIR 1",
    "repair_2": "REPAIR 2",
    "repair_3": "REPAIR 3",
    "imperial": "IMPERIAL",
    "munition": "MUNITION",
    "mercenary": "MERCENARY",
    "workshop": "WORKSHOP",
}

OPENING_BUILDINGS = (
    "prototype_1", "prototype_2", "boiler", "transit",
    "repair_1", "repair_2", "repair_3",
)
MID_BUILDINGS = (
    "imperial", "prototype_1", "prototype_2", "munition",
    "boiler", "transit", "mercenary",
)
PHASE_BUILDINGS = {"opening": OPENING_BUILDINGS, "mid": MID_BUILDINGS}

# These descriptions deliberately reproduce the user-supplied plan rather than
# silently reinterpreting building buffs or correcting apparent inconsistencies.
OPENING_DETAILS = {
    "prototype_1": "1,200pts/m",
    "prototype_2": "1,200pts/m",
    "boiler": "240/m",
    "transit": "Control time -50%",
    "repair_1": "600pts/m",
    "repair_2": "600pts/m",
    "repair_3": "600pts/m",
}
MID_DETAILS = {
    "imperial": "1,800pts/m",
    "prototype_1": "1,200pts/m",
    "prototype_2": "1,200pts/m",
    "munition": "Troop damage +15%",
    "boiler": "Control time -50%",
    "transit": "Teleport cooldown -50%",
    "mercenary": "240pts/m",
}

DEFAULT_ROSTER = {
    "a1": "Alaska", "a2": "Rails", "a3": "Destro", "a4": "Pookiemon",
    "a5": "Jjk", "a6": "Astra", "a7": "Mia",
    **{f"s{i}": f"Support {i}" for i in range(1, 9)},
}
DEFAULT_OPENING = dict(zip(OPENING_BUILDINGS, ATTACKER_SLOTS))
# Mid-game follows the provided player movement, independent of ordering.
DEFAULT_MID = {
    "imperial": "a1", "prototype_1": "a2", "prototype_2": "a5",
    "munition": "a3", "boiler": "a4", "transit": "a6", "mercenary": "a7",
}


def default_plan() -> dict:
    return {
        "schema_version": 1,
        "roster": deepcopy(DEFAULT_ROSTER),
        "opening": dict(DEFAULT_OPENING),
        "mid": dict(DEFAULT_MID),
    }


def clean_name(value: str) -> str:
    """Validate a player name without destroying Unicode WoS names."""
    name = str(value).strip()
    if not name or len(name) > 24:
        raise ValueError("Player names must contain 1–24 characters.")
    if any(ch in name for ch in ("\n", "\r", "`", "@", "<", ">", "\t")):
        raise ValueError("Names cannot contain mentions, line breaks or backticks.")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("Names cannot contain control characters.")
    return name


def validate_plan(plan: dict) -> None:
    if not isinstance(plan, dict) or not isinstance(plan.get("roster"), dict):
        raise ValueError("Invalid Foundry plan data.")
    roster = plan["roster"]
    if set(roster) != set(ALL_SLOTS):
        raise ValueError("A Foundry plan must have exactly 7 attackers and 8 support players.")
    names = [clean_name(roster[slot]).casefold() for slot in ALL_SLOTS]
    if len(set(names)) != len(names):
        raise ValueError("Player names must be unique across all 15 positions.")
    for phase, buildings in PHASE_BUILDINGS.items():
        assigned = plan.get(phase)
        if not isinstance(assigned, dict) or set(assigned) != set(buildings):
            raise ValueError(f"Invalid {phase} building assignments.")
        if set(assigned.values()) != set(ATTACKER_SLOTS):
            raise ValueError(f"Every {phase} building needs a different attacker.")


def set_assignment(plan: dict, phase: str, building: str, slot: str) -> tuple[str, str]:
    """Swap occupants if the selected attacker already owns another objective.

    Returns (target_old_slot, previous_building_of_selected_slot).
    """
    if phase not in PHASE_BUILDINGS or building not in PHASE_BUILDINGS[phase]:
        raise ValueError("That objective is not active in the selected phase.")
    if slot not in ATTACKER_SLOTS:
        raise ValueError("Only the seven attackers can occupy combat objectives.")
    validate_plan(plan)
    assignments = plan[phase]
    previous_building = next(key for key, value in assignments.items() if value == slot)
    old_slot = assignments[building]
    assignments[building] = slot
    assignments[previous_building] = old_slot
    validate_plan(plan)
    return old_slot, previous_building


def rename_player(plan: dict, slot: str, value: str) -> tuple[str, str]:
    validate_plan(plan)
    if slot not in ALL_SLOTS:
        raise ValueError("Unknown player position.")
    name = clean_name(value)
    if any(
        other != slot and str(plan["roster"][other]).casefold() == name.casefold()
        for other in ALL_SLOTS
    ):
        raise ValueError("That player is already on the roster.")
    previous = plan["roster"][slot]
    plan["roster"][slot] = name
    validate_plan(plan)
    return previous, name


def player_at(plan: dict, phase: str, building: str) -> str:
    return plan["roster"][plan[phase][building]]


def opening_location_for_slot(plan: dict, slot: str) -> str:
    for building, assigned_slot in plan["opening"].items():
        if assigned_slot == slot:
            return BUILDING_NAMES[building]
    raise ValueError("Missing opening assignment.")


def phase_lines(plan: dict, phase: str, *, wos: bool = False) -> list[str]:
    validate_plan(plan)
    lines = []
    details = OPENING_DETAILS if phase == "opening" else MID_DETAILS
    for number, building in enumerate(PHASE_BUILDINGS[phase], 1):
        slot = plan[phase][building]
        label = BUILDING_NAMES[building]
        movement = "" if phase == "opening" else f"{opening_location_for_slot(plan, slot)} > "
        note = details[building]
        if wos:
            # Compact enough for small in-game messages, while retaining the
            # user's numbered player / origin / objective / bonus structure.
            note = note.replace("pts", "pt").replace("Troop damage", "Dmg")
            note = note.replace("Teleport cooldown", "TP cooldown").replace("Control time", "Control")
        lines.append(f"{number}. {plan['roster'][slot]} - {movement}{label} ({note})")
    return lines


def wos_length(value: str) -> int:
    """Count UTF-16 code units to be conservative with in-game text limits."""
    return len(value.encode("utf-16-le")) // 2


def _split_section(title: str, lines: list[str], limit: int) -> list[str]:
    result: list[str] = []
    current = title
    for line in lines:
        next_text = f"{current}\n{line}"
        if wos_length(next_text) <= limit:
            current = next_text
            continue
        if current != title:
            result.append(current)
            current = title + " (cont.)"
        if wos_length(f"{current}\n{line}") > limit:
            raise ValueError("Player names or objective lines are too long for this WoS limit.")
        current += "\n" + line
    if current != title:
        result.append(current)
    return result


def _name_lines(label: str, names: list[str]) -> list[str]:
    """Keep roster lines short even when all players use wide/emoji names."""
    lines = []
    current = label
    for name in names:
        candidate = (current + (" " if current.endswith(":") else ", ") + name)
        if wos_length(candidate) > 140 and current != label:
            lines.append(current)
            current = label + " (cont.): " + name
        else:
            current = candidate
    if current != label:
        lines.append(current)
    return lines


def wos_messages(plan: dict, foundry: int, limit: int = 200) -> list[str]:
    """Return individually copyable text chunks, each within the chosen limit."""
    validate_plan(plan)
    if foundry not in (1, 2):
        raise ValueError("Foundry must be 1 or 2.")
    if limit not in (200, 300, 400):
        raise ValueError("Use the 200, 300 or 400 character limit.")
    roster = plan["roster"]
    teams = [
        "15 players: 7 attackers + 8 support",
        *_name_lines("Main 4:", [roster[s] for s in MAIN_SLOTS]),
        *_name_lines("Backup 3:", [roster[s] for s in BACKUP_SLOTS]),
        *_name_lines("Support 1-4:", [roster[s] for s in SUPPORT_SLOTS[:4]]),
        *_name_lines("Support 5-8:", [roster[s] for s in SUPPORT_SLOTS[4:]]),
    ]
    sections = [
        (f"FOUNDRY {foundry} - TEAM", teams),
        (f"FOUNDRY {foundry} - PHASE 1 OPENING", phase_lines(plan, "opening", wos=True)),
        (f"FOUNDRY {foundry} - PHASE 2 MID", phase_lines(plan, "mid", wos=True)),
        (
            f"FOUNDRY {foundry} - SUPPORT/LATE",
            [
                "7 attackers hold the 7 priority objectives.",
                "8 support players avoid combat.",
                "When Workshop opens, support gathers with remaining marches for points.",
            ],
        ),
    ]
    messages = []
    for title, lines in sections:
        messages.extend(_split_section(title, lines, limit))
    if not all(0 < wos_length(x) <= limit for x in messages):
        raise AssertionError("WoS output exceeds the requested limit.")
    return messages
