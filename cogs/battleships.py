# cogs/battleships.py
from __future__ import annotations

import asyncio
import random
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.game_stats import record_head_to_head_result, register_game
from core.logger import log_cmd, warn
from core.settings import SettingsManager
from core.storage import load_guild_json, save_guild_json
from core.utils import ensure_deferred


GAMES_FILENAME = "battleships_games.json"
BOARD_SIZE = 10
COL_LABELS = "ABCDEFGHIJ"
FLEET_SPEC: tuple[tuple[str, int], ...] = (
    ("Carrier", 5),
    ("Battleship", 4),
    ("Cruiser", 3),
    ("Submarine", 3),
    ("Destroyer", 2),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def all_coordinates() -> list[str]:
    return [
        f"{COL_LABELS[col]}{row + 1}"
        for row in range(BOARD_SIZE)
        for col in range(BOARD_SIZE)
    ]


def normalise_coordinate(raw: str) -> str | None:
    value = re.sub(r"[\s,._-]+", "", str(raw or "").upper())

    match = re.fullmatch(r"([A-J])(10|[1-9])", value)
    if match:
        return f"{match.group(1)}{int(match.group(2))}"

    match = re.fullmatch(r"(10|[1-9])([A-J])", value)
    if match:
        return f"{match.group(2)}{int(match.group(1))}"

    return None


def coord_to_rc(coord: str) -> tuple[int, int]:
    value = normalise_coordinate(coord)
    if value is None:
        raise ValueError(f"Invalid Battleships coordinate: {coord!r}")
    col = COL_LABELS.index(value[0])
    row = int(value[1:]) - 1
    return row, col


def rc_to_coord(row: int, col: int) -> str:
    if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
        raise ValueError("Battleships row/column out of range")
    return f"{COL_LABELS[col]}{row + 1}"


def _normalise_fleet(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}

    out: dict[str, list[str]] = {}
    for name, expected_size in FLEET_SPEC:
        cells = raw.get(name)
        if not isinstance(cells, list):
            return {}

        clean: list[str] = []
        for cell in cells:
            coord = normalise_coordinate(str(cell))
            if coord is None or coord in clean:
                return {}
            clean.append(coord)

        if len(clean) != expected_size:
            return {}
        out[name] = clean

    all_cells = [cell for cells in out.values() for cell in cells]
    if len(set(all_cells)) != len(all_cells):
        return {}
    return out


def generate_fleet() -> dict[str, list[str]]:
    occupied: set[str] = set()
    fleet: dict[str, list[str]] = {}

    for name, size in FLEET_SPEC:
        for _attempt in range(1000):
            horizontal = bool(random.getrandbits(1))
            if horizontal:
                row = random.randrange(BOARD_SIZE)
                col = random.randrange(BOARD_SIZE - size + 1)
                cells = [rc_to_coord(row, col + offset) for offset in range(size)]
            else:
                row = random.randrange(BOARD_SIZE - size + 1)
                col = random.randrange(BOARD_SIZE)
                cells = [rc_to_coord(row + offset, col) for offset in range(size)]

            if occupied.isdisjoint(cells):
                fleet[name] = cells
                occupied.update(cells)
                break
        else:
            raise RuntimeError("Could not generate Battleships fleet")

    return fleet


def fleet_cells(fleet: dict[str, list[str]]) -> set[str]:
    return {cell for cells in fleet.values() for cell in cells}


def ship_for_coordinate(
    fleet: dict[str, list[str]],
    coord: str,
) -> str | None:
    for name, cells in fleet.items():
        if coord in cells:
            return name
    return None


def sunk_ships(
    fleet: dict[str, list[str]],
    shots: set[str],
) -> set[str]:
    return {
        name
        for name, cells in fleet.items()
        if cells and set(cells).issubset(shots)
    }


def remaining_ship_count(
    fleet: dict[str, list[str]],
    shots: set[str],
) -> int:
    return len(fleet) - len(sunk_ships(fleet, shots))


def fleet_destroyed(
    fleet: dict[str, list[str]],
    shots: set[str],
) -> bool:
    cells = fleet_cells(fleet)
    return bool(cells) and cells.issubset(shots)


def render_target_board(
    target_fleet: dict[str, list[str]],
    shots: set[str],
) -> str:
    target_cells = fleet_cells(target_fleet)
    lines = ["    A B C D E F G H I J"]

    for row in range(BOARD_SIZE):
        cells: list[str] = []
        for col in range(BOARD_SIZE):
            coord = rc_to_coord(row, col)
            if coord in shots:
                cells.append("X" if coord in target_cells else "o")
            else:
                cells.append(".")
        lines.append(f"{row + 1:>2}  " + " ".join(cells))

    return "\n".join(lines)


def render_own_board(
    fleet: dict[str, list[str]],
    opponent_shots: set[str],
) -> str:
    own_cells = fleet_cells(fleet)
    lines = ["    A B C D E F G H I J"]

    for row in range(BOARD_SIZE):
        cells: list[str] = []
        for col in range(BOARD_SIZE):
            coord = rc_to_coord(row, col)
            if coord in opponent_shots:
                cells.append("X" if coord in own_cells else "o")
            elif coord in own_cells:
                cells.append("S")
            else:
                cells.append(".")
        lines.append(f"{row + 1:>2}  " + " ".join(cells))

    return "\n".join(lines)


def _neighbours(coord: str) -> list[str]:
    row, col = coord_to_rc(coord)
    out: list[str] = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        rr = row + dr
        cc = col + dc
        if 0 <= rr < BOARD_SIZE and 0 <= cc < BOARD_SIZE:
            out.append(rc_to_coord(rr, cc))
    return out


def choose_computer_shot(
    human_fleet: dict[str, list[str]],
    previous_shots: set[str],
) -> str:
    available = set(all_coordinates()) - previous_shots
    if not available:
        raise RuntimeError("Computer has no Battleships shots remaining")

    target_candidates: list[str] = []
    for _name, cells in human_fleet.items():
        ship_cells = set(cells)
        hits = [coord for coord in cells if coord in previous_shots]
        if not hits or ship_cells.issubset(previous_shots):
            continue

        if len(hits) >= 2:
            points = [coord_to_rc(coord) for coord in hits]
            rows = {row for row, _col in points}
            cols = {col for _row, col in points}

            if len(rows) == 1:
                row = points[0][0]
                hit_cols = sorted(col for _row, col in points)
                for col in (hit_cols[0] - 1, hit_cols[-1] + 1):
                    if 0 <= col < BOARD_SIZE:
                        candidate = rc_to_coord(row, col)
                        if candidate in available:
                            target_candidates.append(candidate)
            elif len(cols) == 1:
                col = points[0][1]
                hit_rows = sorted(row for row, _col in points)
                for row in (hit_rows[0] - 1, hit_rows[-1] + 1):
                    if 0 <= row < BOARD_SIZE:
                        candidate = rc_to_coord(row, col)
                        if candidate in available:
                            target_candidates.append(candidate)

        if not target_candidates:
            for hit in hits:
                target_candidates.extend(
                    candidate
                    for candidate in _neighbours(hit)
                    if candidate in available
                )

    if target_candidates:
        return random.choice(list(dict.fromkeys(target_candidates)))

    parity = [
        coord
        for coord in available
        if sum(coord_to_rc(coord)) % 2 == 0
    ]
    if parity:
        return random.choice(parity)

    return random.choice(list(available))


def _shot_result_text(
    shooter: str,
    coord: str,
    *,
    hit: bool,
    sunk_ship: str | None,
) -> str:
    if sunk_ship:
        return f"{shooter} fired at **{coord}** — 💥 HIT and sank the **{sunk_ship}**!"
    if hit:
        return f"{shooter} fired at **{coord}** — 💥 HIT!"
    return f"{shooter} fired at **{coord}** — 🌊 miss."


def _placement_cells(
    start_coord: str,
    size: int,
    orientation: str,
) -> list[str] | None:
    row, col = coord_to_rc(start_coord)
    horizontal = str(orientation).upper().startswith("H")
    cells: list[str] = []
    for offset in range(size):
        rr = row if horizontal else row + offset
        cc = col + offset if horizontal else col
        if not (0 <= rr < BOARD_SIZE and 0 <= cc < BOARD_SIZE):
            return None
        cells.append(rc_to_coord(rr, cc))
    return cells


# Kept for backwards compatibility with the previous Battleships cog.
class FireModal(discord.ui.Modal, title="Fire at a coordinate"):
    coordinate = discord.ui.TextInput(
        label="Coordinate",
        placeholder="Example: B7",
        min_length=2,
        max_length=4,
        required=True,
    )

    def __init__(self, service: "BattleshipsService", message_id: int) -> None:
        super().__init__()
        self.service = service
        self.message_id = int(message_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.handle_fire(
            interaction,
            str(self.coordinate.value),
            source_message_id=self.message_id,
        )


class TargetRowSelect(discord.ui.Select):
    def __init__(self, view: "TargetingView") -> None:
        self.target_view = view
        options = [
            discord.SelectOption(label=f"Row {row}", value=str(row))
            for row in range(1, BOARD_SIZE + 1)
        ]
        super().__init__(
            placeholder="1️⃣ Pick a row",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.target_view.selected_row = int(self.values[0])
        self.target_view.selected_col = None
        self.target_view.refresh_columns()
        await self.target_view.refresh(interaction)


class TargetColumnSelect(discord.ui.Select):
    def __init__(self, view: "TargetingView") -> None:
        self.target_view = view
        super().__init__(
            placeholder="2️⃣ Pick an available column",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Choose a row first", value="none")],
            disabled=True,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.defer()
            return
        self.target_view.selected_col = self.values[0]
        await self.target_view.refresh(interaction)


class ConfirmFireButton(discord.ui.Button):
    def __init__(self, view: "TargetingView") -> None:
        self.target_view = view
        super().__init__(
            label="Fire",
            emoji="🎯",
            style=discord.ButtonStyle.danger,
            disabled=True,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        row = self.target_view.selected_row
        col = self.target_view.selected_col
        if row is None or col is None:
            await interaction.response.send_message(
                "Pick a row and column first.",
                ephemeral=True,
            )
            return
        await self.target_view.service.handle_fire(
            interaction,
            f"{col}{row}",
            source_message_id=self.target_view.source_message_id,
            close_picker=True,
        )


class ClosePickerButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Close",
            emoji="✖️",
            style=discord.ButtonStyle.secondary,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Target picker closed.",
            embed=None,
            view=None,
        )


class TargetingView(discord.ui.View):
    def __init__(
        self,
        service: "BattleshipsService",
        *,
        source_message_id: int,
        shooter_slot: int,
        shots: set[str],
    ) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.source_message_id = int(source_message_id)
        self.shooter_slot = int(shooter_slot)
        self.shots = set(shots)
        self.selected_row: int | None = None
        self.selected_col: str | None = None

        self.row_select = TargetRowSelect(self)
        self.column_select = TargetColumnSelect(self)
        self.fire_button = ConfirmFireButton(self)
        self.add_item(self.row_select)
        self.add_item(self.column_select)
        self.add_item(self.fire_button)
        self.add_item(ClosePickerButton())

    def refresh_columns(self) -> None:
        if self.selected_row is None:
            self.column_select.options = [
                discord.SelectOption(label="Choose a row first", value="none")
            ]
            self.column_select.disabled = True
            self.fire_button.disabled = True
            return

        available = [
            col
            for col in COL_LABELS
            if f"{col}{self.selected_row}" not in self.shots
        ]
        if not available:
            self.column_select.options = [
                discord.SelectOption(label="No targets left in this row", value="none")
            ]
            self.column_select.disabled = True
            self.fire_button.disabled = True
            return

        self.column_select.options = [
            discord.SelectOption(
                label=f"Column {col}",
                value=col,
                description=f"Fire at {col}{self.selected_row}",
            )
            for col in available
        ]
        self.column_select.disabled = False
        self.fire_button.disabled = self.selected_col is None

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.refresh_columns()
        self.fire_button.disabled = self.selected_row is None or self.selected_col is None
        game = self.service._get_game(interaction.guild_id or 0, interaction.channel_id or 0)
        if not game:
            await interaction.response.edit_message(
                content="That Battleships game is no longer available.",
                embed=None,
                view=None,
            )
            return
        await interaction.response.edit_message(
            embed=self.service._target_picker_embed(
                game,
                self.shooter_slot,
                self.selected_row,
                self.selected_col,
            ),
            view=self,
        )


class PlacementShipSelect(discord.ui.Select):
    def __init__(self, view: "FleetPlacementView") -> None:
        self.placement_view = view
        super().__init__(
            placeholder="1️⃣ Ship",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=name,
                    value=name,
                    description=f"{size} spaces",
                    default=name == view.ship_name,
                )
                for name, size in FLEET_SPEC
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.placement_view.ship_name = self.values[0]
        await self.placement_view.refresh(interaction)


class PlacementRowSelect(discord.ui.Select):
    def __init__(self, view: "FleetPlacementView") -> None:
        self.placement_view = view
        super().__init__(
            placeholder="2️⃣ Start row",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"Row {row}",
                    value=str(row),
                    default=row == view.row_number,
                )
                for row in range(1, BOARD_SIZE + 1)
            ],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.placement_view.row_number = int(self.values[0])
        await self.placement_view.refresh(interaction)


class PlacementColumnSelect(discord.ui.Select):
    def __init__(self, view: "FleetPlacementView") -> None:
        self.placement_view = view
        super().__init__(
            placeholder="3️⃣ Start column",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"Column {col}",
                    value=col,
                    default=col == view.col_label,
                )
                for col in COL_LABELS
            ],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.placement_view.col_label = self.values[0]
        await self.placement_view.refresh(interaction)


class PlacementOrientationSelect(discord.ui.Select):
    def __init__(self, view: "FleetPlacementView") -> None:
        self.placement_view = view
        super().__init__(
            placeholder="4️⃣ Direction",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Horizontal →",
                    value="H",
                    default=view.orientation == "H",
                ),
                discord.SelectOption(
                    label="Vertical ↓",
                    value="V",
                    default=view.orientation == "V",
                ),
            ],
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.placement_view.orientation = self.values[0]
        await self.placement_view.refresh(interaction)


class PlaceShipButton(discord.ui.Button):
    def __init__(self, view: "FleetPlacementView") -> None:
        self.placement_view = view
        super().__init__(
            label="Place Ship",
            emoji="🚢",
            style=discord.ButtonStyle.primary,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.placement_view.service.place_ship(
            interaction,
            source_message_id=self.placement_view.source_message_id,
            slot=self.placement_view.slot,
            ship_name=self.placement_view.ship_name,
            start_coord=f"{self.placement_view.col_label}{self.placement_view.row_number}",
            orientation=self.placement_view.orientation,
            placement_view=self.placement_view,
        )


class PlacementRandomButton(discord.ui.Button):
    def __init__(self, view: "FleetPlacementView") -> None:
        self.placement_view = view
        super().__init__(
            label="Randomise",
            emoji="🎲",
            style=discord.ButtonStyle.secondary,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.placement_view.service.randomize_fleet(
            interaction,
            source_message_id=self.placement_view.source_message_id,
            slot=self.placement_view.slot,
            placement_view=self.placement_view,
        )


class PlacementReadyButton(discord.ui.Button):
    def __init__(self, view: "FleetPlacementView") -> None:
        self.placement_view = view
        super().__init__(
            label="Ready",
            emoji="✅",
            style=discord.ButtonStyle.success,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.placement_view.service.mark_ready(
            interaction,
            source_message_id=self.placement_view.source_message_id,
            slot=self.placement_view.slot,
            placement_view=self.placement_view,
        )


class FleetPlacementView(discord.ui.View):
    def __init__(
        self,
        service: "BattleshipsService",
        *,
        source_message_id: int,
        slot: int,
    ) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.source_message_id = int(source_message_id)
        self.slot = int(slot)
        self.ship_name = FLEET_SPEC[0][0]
        self.row_number = 1
        self.col_label = "A"
        self.orientation = "H"
        self.rebuild_controls()

    def rebuild_controls(self) -> None:
        self.clear_items()
        self.add_item(PlacementShipSelect(self))
        self.add_item(PlacementRowSelect(self))
        self.add_item(PlacementColumnSelect(self))
        self.add_item(PlacementOrientationSelect(self))
        self.add_item(PlaceShipButton(self))
        self.add_item(PlacementRandomButton(self))
        self.add_item(PlacementReadyButton(self))

    async def refresh(
        self,
        interaction: discord.Interaction,
        *,
        notice: str | None = None,
    ) -> None:
        self.rebuild_controls()
        game = self.service._get_game(interaction.guild_id or 0, interaction.channel_id or 0)
        if not game:
            await interaction.response.edit_message(
                content="That Battleships setup is no longer available.",
                embed=None,
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=notice,
            embed=self.service._placement_embed(game, self.slot, self),
            view=self,
        )


class FireButton(discord.ui.Button):
    def __init__(self, service: "BattleshipsService") -> None:
        super().__init__(
            label="Fire",
            emoji="🎯",
            style=discord.ButtonStyle.danger,
            custom_id="hotbot:battleships:fire:v1",
            row=0,
        )
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.open_target_picker(interaction)


class FleetButton(discord.ui.Button):
    def __init__(self, service: "BattleshipsService") -> None:
        super().__init__(
            label="My Fleet",
            emoji="🚢",
            style=discord.ButtonStyle.primary,
            custom_id="hotbot:battleships:fleet:v1",
            row=0,
        )
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.show_fleet(interaction)


class ResignButton(discord.ui.Button):
    def __init__(self, service: "BattleshipsService") -> None:
        super().__init__(
            label="Resign",
            emoji="🏳️",
            style=discord.ButtonStyle.secondary,
            custom_id="hotbot:battleships:resign:v1",
            row=0,
        )
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.request_resign(interaction)


class ConfirmResignButton(discord.ui.Button):
    def __init__(self, confirm_view: "ResignConfirmView") -> None:
        super().__init__(
            label="Yes, Resign",
            emoji="🏳️",
            style=discord.ButtonStyle.danger,
            row=0,
        )
        self.confirm_view = confirm_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.confirm_view.user_id:
            await interaction.response.send_message(
                "That confirmation is not for you.",
                ephemeral=True,
            )
            return

        success = await self.confirm_view.service.resign(
            interaction,
            source_message_id=self.confirm_view.source_message_id,
        )
        if success:
            try:
                await interaction.edit_original_response(
                    content="🏳️ You resigned from the Battleships game.",
                    embed=None,
                    view=None,
                )
            except Exception:
                pass


class KeepPlayingButton(discord.ui.Button):
    def __init__(self, confirm_view: "ResignConfirmView") -> None:
        super().__init__(
            label="Keep Playing",
            emoji="✅",
            style=discord.ButtonStyle.success,
            row=0,
        )
        self.confirm_view = confirm_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.confirm_view.user_id:
            await interaction.response.send_message(
                "That confirmation is not for you.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            content="✅ Resignation cancelled. Keep playing.",
            embed=None,
            view=None,
        )


class ResignConfirmView(discord.ui.View):
    def __init__(
        self,
        service: "BattleshipsService",
        *,
        source_message_id: int,
        user_id: int,
    ) -> None:
        super().__init__(timeout=60)
        self.service = service
        self.source_message_id = int(source_message_id)
        self.user_id = int(user_id)
        self.add_item(ConfirmResignButton(self))
        self.add_item(KeepPlayingButton(self))


class CancelButton(discord.ui.Button):
    def __init__(self, service: "BattleshipsService") -> None:
        super().__init__(
            label="Cancel",
            emoji="🛑",
            style=discord.ButtonStyle.secondary,
            custom_id="hotbot:battleships:cancel:v1",
            row=0,
        )
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.cancel(interaction)


class SetupFleetButton(discord.ui.Button):
    def __init__(self, service: "BattleshipsService") -> None:
        super().__init__(
            label="Set Fleet",
            emoji="🚢",
            style=discord.ButtonStyle.primary,
            custom_id="hotbot:battleships:setup_fleet:v1",
            row=0,
        )
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.open_fleet_setup(interaction)


class SetupRandomButton(discord.ui.Button):
    def __init__(self, service: "BattleshipsService") -> None:
        super().__init__(
            label="Random Fleet",
            emoji="🎲",
            style=discord.ButtonStyle.secondary,
            custom_id="hotbot:battleships:setup_random:v1",
            row=0,
        )
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.randomize_from_public_setup(interaction)


class SetupReadyButton(discord.ui.Button):
    def __init__(self, service: "BattleshipsService") -> None:
        super().__init__(
            label="Ready",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id="hotbot:battleships:setup_ready:v1",
            row=0,
        )
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.mark_ready_from_public_setup(interaction)


class BattleshipsView(discord.ui.View):
    def __init__(
        self,
        service: "BattleshipsService",
        *,
        disabled: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.add_item(FireButton(service))
        self.add_item(FleetButton(service))
        self.add_item(ResignButton(service))

        if disabled:
            for child in self.children:
                child.disabled = True


class BattleshipsSetupView(discord.ui.View):
    def __init__(
        self,
        service: "BattleshipsService",
        *,
        disabled: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.add_item(SetupFleetButton(service))
        self.add_item(SetupRandomButton(service))
        self.add_item(SetupReadyButton(service))
        self.add_item(CancelButton(service))

        if disabled:
            for child in self.children:
                child.disabled = True


class BattleshipsService:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings: SettingsManager = bot.settings
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    def _lock_for(self, guild_id: int, channel_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(channel_id))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def allowed(self, guild_id: int | None, channel_id: int | None) -> bool:
        return self.settings.is_game_allowed(guild_id, channel_id, "battleships")

    def _load_games_blob(self, guild_id: int) -> dict[str, Any]:
        raw = load_guild_json(guild_id, GAMES_FILENAME, {"games": {}})
        if not isinstance(raw, dict):
            raw = {"games": {}}
        if not isinstance(raw.get("games"), dict):
            raw["games"] = {}
        return raw

    def _save_games_blob(self, guild_id: int, blob: dict[str, Any]) -> None:
        save_guild_json(guild_id, GAMES_FILENAME, blob)

    def _normalise_game(self, game: dict[str, Any]) -> dict[str, Any] | None:
        fleets = game.get("fleets")
        shots = game.get("shots")
        if not isinstance(fleets, dict) or not isinstance(shots, dict):
            return None

        fleet1 = _normalise_fleet(fleets.get("1"))
        fleet2 = _normalise_fleet(fleets.get("2"))
        if not fleet1 or not fleet2:
            return None

        clean_shots: dict[str, list[str]] = {"1": [], "2": []}
        for slot in ("1", "2"):
            raw_shots = shots.get(slot)
            if not isinstance(raw_shots, list):
                raw_shots = []
            for raw_coord in raw_shots:
                coord = normalise_coordinate(str(raw_coord))
                if coord and coord not in clean_shots[slot]:
                    clean_shots[slot].append(coord)

        game["fleets"] = {"1": fleet1, "2": fleet2}
        game["shots"] = clean_shots
        game["turn"] = 2 if int(game.get("turn") or 1) == 2 else 1
        game["computer"] = bool(game.get("computer"))
        phase = str(game.get("phase") or "active").lower()
        game["phase"] = "setup" if phase == "setup" else "active"

        ready = game.get("ready")
        if not isinstance(ready, dict):
            ready = {"1": True, "2": True}
        game["ready"] = {
            "1": bool(ready.get("1", game["phase"] == "active")),
            "2": bool(ready.get("2", game["phase"] == "active")),
        }
        if game["computer"]:
            game["ready"]["2"] = True

        for slot in (1, 2):
            key = f"p{slot}_name"
            if not str(game.get(key) or "").strip():
                game[key] = self._resolve_player_name(game, slot)
        return game

    def _get_game(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        raw = self._load_games_blob(guild_id)["games"].get(str(channel_id))
        if not isinstance(raw, dict):
            return None
        return self._normalise_game(raw)

    def _set_game(
        self,
        guild_id: int,
        channel_id: int,
        game: dict[str, Any],
    ) -> None:
        blob = self._load_games_blob(guild_id)
        blob["games"][str(channel_id)] = game
        self._save_games_blob(guild_id, blob)

    def _remove_game(self, guild_id: int, channel_id: int) -> None:
        blob = self._load_games_blob(guild_id)
        blob["games"].pop(str(channel_id), None)
        self._save_games_blob(guild_id, blob)

    @staticmethod
    def _player_id(game: dict[str, Any], slot: int) -> int:
        return int(game.get(f"p{slot}_id") or 0)

    @staticmethod
    def _other_slot(slot: int) -> int:
        return 2 if int(slot) == 1 else 1

    def _slot_for_user(self, game: dict[str, Any], user_id: int) -> int | None:
        if int(user_id) == self._player_id(game, 1):
            return 1
        if not bool(game.get("computer")) and int(user_id) == self._player_id(game, 2):
            return 2
        return None

    def _resolve_player_name(self, game: dict[str, Any], slot: int) -> str:
        if slot == 2 and bool(game.get("computer")):
            return "Computer"

        user_id = self._player_id(game, slot)
        guild_id = int(game.get("guild_id") or 0)
        guild = self.bot.get_guild(guild_id) if guild_id else None
        if guild is not None and user_id:
            member = guild.get_member(user_id)
            if member is not None:
                return member.display_name

        user = self.bot.get_user(user_id) if user_id else None
        if user is not None:
            return getattr(user, "display_name", None) or user.name

        return f"Player {slot}"

    def _slot_name(self, game: dict[str, Any], slot: int) -> str:
        if slot == 2 and bool(game.get("computer")):
            return "Computer"
        stored = str(game.get(f"p{slot}_name") or "").strip()
        return stored or self._resolve_player_name(game, slot)

    def _slot_label(self, game: dict[str, Any], slot: int) -> str:
        if slot == 2 and bool(game.get("computer")):
            return "🤖 **Computer**"
        return f"🎮 **{self._slot_name(game, slot)}**"

    def _fleet(self, game: dict[str, Any], slot: int) -> dict[str, list[str]]:
        fleets = game.get("fleets") if isinstance(game.get("fleets"), dict) else {}
        return _normalise_fleet(fleets.get(str(slot)))

    def _set_fleet(
        self,
        game: dict[str, Any],
        slot: int,
        fleet: dict[str, list[str]],
    ) -> None:
        fleets = game.get("fleets")
        if not isinstance(fleets, dict):
            fleets = {}
            game["fleets"] = fleets
        fleets[str(slot)] = fleet

    def _shots(self, game: dict[str, Any], slot: int) -> set[str]:
        shots = game.get("shots") if isinstance(game.get("shots"), dict) else {}
        raw = shots.get(str(slot)) if isinstance(shots, dict) else []
        return {
            coord
            for value in (raw if isinstance(raw, list) else [])
            if (coord := normalise_coordinate(str(value))) is not None
        }

    def _set_shots(self, game: dict[str, Any], slot: int, shots: set[str]) -> None:
        raw = game.get("shots")
        if not isinstance(raw, dict):
            raw = {"1": [], "2": []}
            game["shots"] = raw
        raw[str(slot)] = sorted(shots, key=lambda coord: coord_to_rc(coord))

    def _is_ready(self, game: dict[str, Any], slot: int) -> bool:
        ready = game.get("ready")
        return bool(ready.get(str(slot))) if isinstance(ready, dict) else False

    def _set_ready(self, game: dict[str, Any], slot: int, value: bool) -> None:
        ready = game.get("ready")
        if not isinstance(ready, dict):
            ready = {"1": False, "2": bool(game.get("computer"))}
            game["ready"] = ready
        ready[str(slot)] = bool(value)

    def _build_embed(
        self,
        game: dict[str, Any],
        *,
        status: str = "active",
        winner_slot: int | None = None,
        resigned_slot: int | None = None,
        cancelled_by: int | None = None,
    ) -> discord.Embed:
        phase = str(game.get("phase") or "active")

        if status == "active" and phase == "setup":
            embed = discord.Embed(
                title="🚢 Battleships — Fleet Setup",
                description=(
                    "Set your ships **privately**, or keep a random fleet. "
                    "When you're happy with the layout, press **Ready**."
                ),
            )
            embed.add_field(
                name="Players",
                value=(
                    f"{self._slot_label(game, 1)}\n"
                    f"{self._slot_label(game, 2)}"
                ),
                inline=False,
            )
            embed.add_field(
                name="Fleet Status",
                value=(
                    f"{self._slot_name(game, 1)} — "
                    f"{'✅ Ready' if self._is_ready(game, 1) else '🛠️ Setting up'}\n"
                    f"{self._slot_name(game, 2)} — "
                    f"{'✅ Ready' if self._is_ready(game, 2) else '🛠️ Setting up'}"
                ),
                inline=False,
            )
            embed.set_footer(
                text="Fleet layouts are private. Set Fleet lets you place every ship yourself."
            )
            return embed

        fleet1 = self._fleet(game, 1)
        fleet2 = self._fleet(game, 2)
        shots1 = self._shots(game, 1)
        shots2 = self._shots(game, 2)

        title = "🚢 Battleships"
        if status != "active":
            title += " — Game Over"

        embed = discord.Embed(
            title=title,
            description=(
                "Press **Fire**, choose a row, then choose one of the available columns. "
                "Sink all five enemy ships before yours are destroyed."
            ),
        )

        embed.add_field(
            name=f"🎯 {self._slot_name(game, 1)} — Target Board",
            value=f"```text\n{render_target_board(fleet2, shots1)}\n```",
            inline=False,
        )
        embed.add_field(
            name=f"🎯 {self._slot_name(game, 2)} — Target Board",
            value=f"```text\n{render_target_board(fleet1, shots2)}\n```",
            inline=False,
        )

        p1_remaining = remaining_ship_count(fleet1, shots2)
        p2_remaining = remaining_ship_count(fleet2, shots1)
        embed.add_field(
            name="Ships Remaining",
            value=(
                f"{self._slot_label(game, 1)} — **{p1_remaining}/5**\n"
                f"{self._slot_label(game, 2)} — **{p2_remaining}/5**"
            ),
            inline=True,
        )

        if status == "active":
            turn = int(game.get("turn") or 1)
            embed.add_field(
                name="Turn",
                value=self._slot_label(game, turn),
                inline=True,
            )
            last_action = str(game.get("last_action") or "").strip()
            if last_action:
                embed.add_field(
                    name="Last Shot",
                    value=last_action,
                    inline=False,
                )
            embed.set_footer(
                text=(
                    "Target board: X = hit, o = miss, . = untried. "
                    "My Fleet privately shows S = your ship. No timeout."
                )
            )
        elif status == "finished" and winner_slot is not None:
            embed.add_field(
                name="Result",
                value=f"🏆 {self._slot_label(game, winner_slot)} sank the entire enemy fleet!",
                inline=False,
            )
            if bool(game.get("computer")):
                embed.set_footer(text="Computer games are practice and do not affect the leaderboard.")
        elif status == "resigned" and winner_slot is not None and resigned_slot is not None:
            embed.add_field(
                name="Result",
                value=(
                    f"🏳️ {self._slot_label(game, resigned_slot)} resigned. "
                    f"{self._slot_label(game, winner_slot)} wins!"
                ),
                inline=False,
            )
            if bool(game.get("computer")):
                embed.set_footer(text="Computer games are practice and do not affect the leaderboard.")
        elif status == "cancelled":
            cancelled_name = "Game starter"
            if cancelled_by:
                for slot in (1, 2):
                    if self._player_id(game, slot) == int(cancelled_by):
                        cancelled_name = self._slot_name(game, slot)
                        break
            embed.add_field(
                name="Result",
                value=f"🛑 Game cancelled by **{cancelled_name}**. No result recorded.",
                inline=False,
            )

        return embed

    def _fleet_embed(self, game: dict[str, Any], slot: int) -> discord.Embed:
        fleet = self._fleet(game, slot)
        opponent_shots = self._shots(game, self._other_slot(slot))
        sunk = sunk_ships(fleet, opponent_shots)

        embed = discord.Embed(
            title=f"🚢 {self._slot_name(game, slot)} — Your Fleet",
            description=f"```text\n{render_own_board(fleet, opponent_shots)}\n```",
        )
        status_lines = []
        for name, size in FLEET_SPEC:
            marker = "💀" if name in sunk else "✅"
            status_lines.append(f"{marker} **{name}** — {size} spaces")
        embed.add_field(
            name="Fleet Status",
            value="\n".join(status_lines),
            inline=False,
        )
        embed.set_footer(text="S = ship • X = hit • o = enemy miss • . = water")
        return embed

    def _placement_embed(
        self,
        game: dict[str, Any],
        slot: int,
        placement_view: FleetPlacementView | None = None,
    ) -> discord.Embed:
        fleet = self._fleet(game, slot)
        embed = discord.Embed(
            title=f"🚢 {self._slot_name(game, slot)} — Fleet Setup",
            description=(
                f"```text\n{render_own_board(fleet, set())}\n```\n"
                "Pick a ship, its starting square and direction, then press **Place Ship**. "
                "Ships cannot overlap or run off the board."
            ),
        )
        lines = [f"**{name}** — {size} spaces — `{', '.join(fleet[name])}`" for name, size in FLEET_SPEC]
        embed.add_field(name="Current Layout", value="\n".join(lines), inline=False)
        if placement_view is not None:
            size = dict(FLEET_SPEC)[placement_view.ship_name]
            start = f"{placement_view.col_label}{placement_view.row_number}"
            direction = "Horizontal →" if placement_view.orientation == "H" else "Vertical ↓"
            preview = _placement_cells(start, size, placement_view.orientation)
            preview_text = ", ".join(preview) if preview else "❌ Runs off the board"
            embed.add_field(
                name="Placement Preview",
                value=(
                    f"**{placement_view.ship_name}** from **{start}** — {direction}\n"
                    f"{preview_text}"
                ),
                inline=False,
            )
        embed.set_footer(text="Only you can see this setup screen. Press Ready when finished.")
        return embed

    def _target_picker_embed(
        self,
        game: dict[str, Any],
        shooter_slot: int,
        selected_row: int | None,
        selected_col: str | None,
    ) -> discord.Embed:
        target_slot = self._other_slot(shooter_slot)
        shots = self._shots(game, shooter_slot)
        target_fleet = self._fleet(game, target_slot)
        embed = discord.Embed(
            title=f"🎯 {self._slot_name(game, shooter_slot)} — Choose Your Shot",
            description=(
                f"```text\n{render_target_board(target_fleet, shots)}\n```\n"
                "Choose a **row first**. The second menu then shows the columns you **haven't fired at** in that row."
            ),
        )
        if selected_row is None:
            selected = "No target selected yet."
        elif selected_col is None:
            remaining = [
                f"{col}{selected_row}"
                for col in COL_LABELS
                if f"{col}{selected_row}" not in shots
            ]
            selected = (
                f"Row **{selected_row}** selected. Available: "
                + (", ".join(remaining) if remaining else "none")
            )
        else:
            selected = f"Ready to fire at **{selected_col}{selected_row}**."
        embed.add_field(name="Target", value=selected, inline=False)
        embed.set_footer(text="X = hit • o = miss • . = available target")
        return embed

    async def _resolve_message(
        self,
        interaction: discord.Interaction,
        game: dict[str, Any],
    ) -> discord.Message | None:
        message_id = int(game.get("message_id") or 0)
        if interaction.message is not None and interaction.message.id == message_id:
            return interaction.message

        channel = interaction.channel
        if channel is None and interaction.channel_id:
            channel = self.bot.get_channel(interaction.channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(interaction.channel_id)
                except Exception:
                    channel = None

        if channel is None or not hasattr(channel, "fetch_message") or not message_id:
            return None

        try:
            return await channel.fetch_message(message_id)  # type: ignore[attr-defined]
        except Exception:
            return None

    async def _edit_message(
        self,
        interaction: discord.Interaction,
        game: dict[str, Any],
        *,
        status: str = "active",
        winner_slot: int | None = None,
        resigned_slot: int | None = None,
        cancelled_by: int | None = None,
    ) -> None:
        message = await self._resolve_message(interaction, game)
        if message is None:
            return

        if status == "active" and str(game.get("phase") or "active") == "setup":
            view: discord.ui.View = BattleshipsSetupView(self)
        else:
            view = BattleshipsView(self, disabled=status != "active")

        try:
            await message.edit(
                embed=self._build_embed(
                    game,
                    status=status,
                    winner_slot=winner_slot,
                    resigned_slot=resigned_slot,
                    cancelled_by=cancelled_by,
                ),
                view=view,
            )
        except Exception as exc:
            warn(f"Battleships message edit failed: {exc!r}")

    def _record_result(
        self,
        guild_id: int,
        game: dict[str, Any],
        winner_slot: int,
    ) -> None:
        if bool(game.get("computer")):
            return

        p1_id = self._player_id(game, 1)
        p2_id = self._player_id(game, 2)
        winner_id = self._player_id(game, winner_slot)
        game_id = str(game.get("game_id") or "").strip()
        if not p1_id or not p2_id or not winner_id or not game_id:
            return

        try:
            record_head_to_head_result(
                guild_id,
                "battleships",
                p1_id,
                p2_id,
                winner_id=winner_id,
                result_id=f"battleships:{game_id}",
            )
        except Exception as exc:
            warn(f"Battleships stats record failed: {exc!r}")

    async def _start(
        self,
        interaction: discord.Interaction,
        *,
        p2_id: int,
        p2_name: str,
        computer: bool,
    ) -> None:
        guild = interaction.guild
        channel_id = interaction.channel_id
        if guild is None or channel_id is None:
            await interaction.followup.send(
                "❌ Battleships must be started in a server channel.",
                ephemeral=True,
            )
            return

        if not self.allowed(guild.id, channel_id):
            await interaction.followup.send(
                "❌ Battleships is not enabled in this channel.",
                ephemeral=True,
            )
            return

        async with self._lock_for(guild.id, channel_id):
            existing = self._get_game(guild.id, channel_id)
            if existing:
                message_id = int(existing.get("message_id") or 0)
                jump = (
                    f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"
                    if message_id
                    else ""
                )
                text = "A Battleships game is already running in this channel."
                if jump:
                    text += f" [Open it]({jump})"
                await interaction.followup.send(text, ephemeral=True)
                return

            p1_name = getattr(interaction.user, "display_name", None) or interaction.user.name
            game: dict[str, Any] = {
                "game_id": uuid.uuid4().hex,
                "guild_id": guild.id,
                "channel_id": channel_id,
                "message_id": 0,
                "p1_id": interaction.user.id,
                "p1_name": str(p1_name),
                "p2_id": int(p2_id),
                "p2_name": "Computer" if computer else str(p2_name),
                "computer": bool(computer),
                "phase": "setup",
                "ready": {"1": False, "2": bool(computer)},
                "turn": 1,
                "fleets": {
                    "1": generate_fleet(),
                    "2": generate_fleet(),
                },
                "shots": {"1": [], "2": []},
                "last_action": "",
                "created_at": _utc_now(),
            }

            message = await interaction.followup.send(
                embed=self._build_embed(game),
                view=BattleshipsSetupView(self),
                ephemeral=False,
                wait=True,
            )
            game["message_id"] = int(message.id)
            self._set_game(guild.id, channel_id, game)

    async def start_game(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
    ) -> None:
        if opponent.bot or opponent.id == interaction.user.id:
            await interaction.followup.send(
                "❌ Pick a real opponent, or use Computer mode.",
                ephemeral=True,
            )
            return
        await self._start(
            interaction,
            p2_id=opponent.id,
            p2_name=opponent.display_name,
            computer=False,
        )

    async def start_computer_game(self, interaction: discord.Interaction) -> None:
        bot_user = self.bot.user or interaction.client.user
        if bot_user is None:
            await interaction.followup.send(
                "❌ Computer mode is unavailable until the bot is fully connected.",
                ephemeral=True,
            )
            return
        await self._start(
            interaction,
            p2_id=int(bot_user.id),
            p2_name="Computer",
            computer=True,
        )

    async def open_fleet_setup(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.response.send_message(
                "That Battleships setup is no longer available.",
                ephemeral=True,
            )
            return

        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if not game or int(game.get("message_id") or 0) != interaction.message.id:
            await interaction.response.send_message(
                "That is not the current Battleships game in this channel.",
                ephemeral=True,
            )
            return

        if str(game.get("phase") or "active") != "setup":
            await interaction.response.send_message(
                "The game has already started, so fleet positions are locked.",
                ephemeral=True,
            )
            return

        slot = self._slot_for_user(game, interaction.user.id)
        if slot is None:
            await interaction.response.send_message(
                "You are not playing this Battleships game.",
                ephemeral=True,
            )
            return

        if self._is_ready(game, slot):
            await interaction.response.send_message(
                "✅ Your fleet is already locked in and ready.",
                ephemeral=True,
            )
            return

        view = FleetPlacementView(
            self,
            source_message_id=interaction.message.id,
            slot=slot,
        )
        await interaction.response.send_message(
            embed=self._placement_embed(game, slot, view),
            view=view,
            ephemeral=True,
        )

    async def randomize_from_public_setup(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.response.send_message("That setup is no longer available.", ephemeral=True)
            return
        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if not game or int(game.get("message_id") or 0) != interaction.message.id:
            await interaction.response.send_message("That is not the current Battleships game.", ephemeral=True)
            return
        slot = self._slot_for_user(game, interaction.user.id)
        if slot is None:
            await interaction.response.send_message("You are not playing this game.", ephemeral=True)
            return
        if str(game.get("phase") or "active") != "setup" or self._is_ready(game, slot):
            await interaction.response.send_message("Your fleet is already locked.", ephemeral=True)
            return

        async with self._lock_for(interaction.guild_id, interaction.channel_id):
            game = self._get_game(interaction.guild_id, interaction.channel_id)
            if not game:
                await interaction.response.send_message("That setup is no longer available.", ephemeral=True)
                return
            self._set_fleet(game, slot, generate_fleet())
            self._set_game(interaction.guild_id, interaction.channel_id, game)
        await interaction.response.send_message(
            content="🎲 New random fleet generated.",
            embed=self._fleet_embed(game, slot),
            ephemeral=True,
        )

    async def randomize_fleet(
        self,
        interaction: discord.Interaction,
        *,
        source_message_id: int,
        slot: int,
        placement_view: FleetPlacementView,
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("That setup is no longer available.", ephemeral=True)
            return

        async with self._lock_for(interaction.guild_id, interaction.channel_id):
            game = self._get_game(interaction.guild_id, interaction.channel_id)
            if not game or int(game.get("message_id") or 0) != int(source_message_id):
                await interaction.response.send_message("That setup is no longer available.", ephemeral=True)
                return
            if self._slot_for_user(game, interaction.user.id) != slot:
                await interaction.response.send_message("That isn't your fleet.", ephemeral=True)
                return
            if str(game.get("phase") or "active") != "setup" or self._is_ready(game, slot):
                await interaction.response.send_message("Your fleet is already locked.", ephemeral=True)
                return
            self._set_fleet(game, slot, generate_fleet())
            self._set_game(interaction.guild_id, interaction.channel_id, game)

        await placement_view.refresh(interaction, notice="🎲 Fleet randomised.")

    async def place_ship(
        self,
        interaction: discord.Interaction,
        *,
        source_message_id: int,
        slot: int,
        ship_name: str,
        start_coord: str,
        orientation: str,
        placement_view: FleetPlacementView,
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("That setup is no longer available.", ephemeral=True)
            return

        size_map = dict(FLEET_SPEC)
        if ship_name not in size_map:
            await interaction.response.send_message("Unknown ship.", ephemeral=True)
            return

        new_cells = _placement_cells(start_coord, size_map[ship_name], orientation)
        if new_cells is None:
            await placement_view.refresh(
                interaction,
                notice="❌ That ship would run off the board. Pick another start square or direction.",
            )
            return

        async with self._lock_for(interaction.guild_id, interaction.channel_id):
            game = self._get_game(interaction.guild_id, interaction.channel_id)
            if not game or int(game.get("message_id") or 0) != int(source_message_id):
                await interaction.response.send_message("That setup is no longer available.", ephemeral=True)
                return
            if self._slot_for_user(game, interaction.user.id) != slot:
                await interaction.response.send_message("That isn't your fleet.", ephemeral=True)
                return
            if str(game.get("phase") or "active") != "setup" or self._is_ready(game, slot):
                await interaction.response.send_message("Your fleet is already locked.", ephemeral=True)
                return

            fleet = self._fleet(game, slot)
            occupied_elsewhere = {
                cell
                for name, cells in fleet.items()
                if name != ship_name
                for cell in cells
            }
            overlap = sorted(set(new_cells) & occupied_elsewhere, key=coord_to_rc)
            if overlap:
                await placement_view.refresh(
                    interaction,
                    notice=(
                        "❌ That placement overlaps another ship at "
                        + ", ".join(f"**{coord}**" for coord in overlap)
                        + "."
                    ),
                )
                return

            fleet[ship_name] = new_cells
            self._set_fleet(game, slot, fleet)
            self._set_game(interaction.guild_id, interaction.channel_id, game)

        await placement_view.refresh(
            interaction,
            notice=f"✅ **{ship_name}** placed at {', '.join(new_cells)}.",
        )

    async def mark_ready_from_public_setup(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.response.send_message("That setup is no longer available.", ephemeral=True)
            return
        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if not game or int(game.get("message_id") or 0) != interaction.message.id:
            await interaction.response.send_message("That is not the current Battleships game.", ephemeral=True)
            return
        slot = self._slot_for_user(game, interaction.user.id)
        if slot is None:
            await interaction.response.send_message("You are not playing this game.", ephemeral=True)
            return
        await self.mark_ready(
            interaction,
            source_message_id=interaction.message.id,
            slot=slot,
            placement_view=None,
        )

    async def mark_ready(
        self,
        interaction: discord.Interaction,
        *,
        source_message_id: int,
        slot: int,
        placement_view: FleetPlacementView | None,
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            if interaction.response.is_done():
                await interaction.followup.send("That setup is no longer available.", ephemeral=True)
            else:
                await interaction.response.send_message("That setup is no longer available.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        await interaction.response.defer()

        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != int(source_message_id):
                await interaction.followup.send("That setup is no longer available.", ephemeral=True)
                return
            if self._slot_for_user(game, interaction.user.id) != slot:
                await interaction.followup.send("That isn't your fleet.", ephemeral=True)
                return
            if str(game.get("phase") or "active") != "setup":
                await interaction.followup.send("The game has already started.", ephemeral=True)
                return

            self._set_ready(game, slot, True)
            if self._is_ready(game, 1) and self._is_ready(game, 2):
                game["phase"] = "active"
                game["turn"] = 1
                game["last_action"] = ""
            self._set_game(guild_id, channel_id, game)
            await self._edit_message(interaction, game)

        if placement_view is not None:
            await interaction.edit_original_response(
                content="✅ Fleet locked in. " + (
                    "Battle started!" if str(game.get("phase")) == "active" else "Waiting for the other player."
                ),
                embed=self._fleet_embed(game, slot),
                view=None,
            )
        else:
            await interaction.followup.send(
                "✅ Fleet locked in. " + (
                    "Battle started!" if str(game.get("phase")) == "active" else "Waiting for the other player."
                ),
                ephemeral=True,
            )

    async def open_target_picker(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.response.send_message(
                "That Battleships game is no longer available.",
                ephemeral=True,
            )
            return

        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if not game or int(game.get("message_id") or 0) != interaction.message.id:
            await interaction.response.send_message(
                "That is not the current Battleships game in this channel.",
                ephemeral=True,
            )
            return

        if str(game.get("phase") or "active") != "active":
            await interaction.response.send_message(
                "Both fleets need to be ready before firing starts.",
                ephemeral=True,
            )
            return

        slot = self._slot_for_user(game, interaction.user.id)
        if slot is None:
            await interaction.response.send_message(
                "You are not playing this Battleships game.",
                ephemeral=True,
            )
            return

        if int(game.get("turn") or 1) != slot:
            await interaction.response.send_message("⏳ Not your turn.", ephemeral=True)
            return

        shots = self._shots(game, slot)
        view = TargetingView(
            self,
            source_message_id=interaction.message.id,
            shooter_slot=slot,
            shots=shots,
        )
        await interaction.response.send_message(
            embed=self._target_picker_embed(game, slot, None, None),
            view=view,
            ephemeral=True,
        )

    async def show_fleet(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "That Battleships game is no longer available.",
                ephemeral=True,
            )
            return

        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if (
            not game
            or interaction.message is None
            or int(game.get("message_id") or 0) != interaction.message.id
        ):
            await interaction.response.send_message(
                "That is not the current Battleships game in this channel.",
                ephemeral=True,
            )
            return

        slot = self._slot_for_user(game, interaction.user.id)
        if slot is None:
            await interaction.response.send_message(
                "You are not playing this Battleships game.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=self._fleet_embed(game, slot),
            ephemeral=True,
        )

    async def handle_fire(
        self,
        interaction: discord.Interaction,
        raw_coordinate: str,
        *,
        source_message_id: int,
        close_picker: bool = False,
    ) -> None:
        await interaction.response.defer()

        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.followup.send(
                "That Battleships game is no longer available.",
                ephemeral=True,
            )
            return

        coord = normalise_coordinate(raw_coordinate)
        if coord is None:
            await interaction.followup.send(
                "❌ Use a coordinate from **A1 to J10** — for example `B7`.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        confirmation = ""
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != int(source_message_id):
                await interaction.followup.send(
                    "That is not the current Battleships game in this channel.",
                    ephemeral=True,
                )
                return

            if str(game.get("phase") or "active") != "active":
                await interaction.followup.send(
                    "Both fleets need to be ready before firing starts.",
                    ephemeral=True,
                )
                return

            slot = self._slot_for_user(game, interaction.user.id)
            if slot is None:
                await interaction.followup.send(
                    "You are not playing this Battleships game.",
                    ephemeral=True,
                )
                return

            if int(game.get("turn") or 1) != slot:
                await interaction.followup.send(
                    "⏳ Not your turn.",
                    ephemeral=True,
                )
                return

            target_slot = self._other_slot(slot)
            shots = self._shots(game, slot)
            if coord in shots:
                await interaction.followup.send(
                    f"❌ You already fired at **{coord}**.",
                    ephemeral=True,
                )
                return

            target_fleet = self._fleet(game, target_slot)
            before_sunk = sunk_ships(target_fleet, shots)
            shots.add(coord)
            self._set_shots(game, slot, shots)

            hit_ship = ship_for_coordinate(target_fleet, coord)
            after_sunk = sunk_ships(target_fleet, shots)
            newly_sunk = sorted(after_sunk - before_sunk)
            action = _shot_result_text(
                self._slot_label(game, slot),
                coord,
                hit=hit_ship is not None,
                sunk_ship=newly_sunk[0] if newly_sunk else None,
            )
            confirmation = f"🎯 Fired at **{coord}** — " + ("💥 HIT!" if hit_ship else "🌊 miss.")

            if fleet_destroyed(target_fleet, shots):
                game["last_action"] = action
                self._remove_game(guild_id, channel_id)
                self._record_result(guild_id, game, slot)
                await self._edit_message(
                    interaction,
                    game,
                    status="finished",
                    winner_slot=slot,
                )
                if close_picker:
                    await interaction.edit_original_response(content=confirmation, embed=None, view=None)
                return

            if bool(game.get("computer")):
                human_fleet = self._fleet(game, 1)
                bot_shots = self._shots(game, 2)
                bot_coord = choose_computer_shot(human_fleet, bot_shots)
                bot_before_sunk = sunk_ships(human_fleet, bot_shots)
                bot_shots.add(bot_coord)
                self._set_shots(game, 2, bot_shots)

                bot_hit_ship = ship_for_coordinate(human_fleet, bot_coord)
                bot_after_sunk = sunk_ships(human_fleet, bot_shots)
                bot_newly_sunk = sorted(bot_after_sunk - bot_before_sunk)
                bot_action = _shot_result_text(
                    self._slot_label(game, 2),
                    bot_coord,
                    hit=bot_hit_ship is not None,
                    sunk_ship=bot_newly_sunk[0] if bot_newly_sunk else None,
                )
                game["last_action"] = f"{action}\n{bot_action}"

                if fleet_destroyed(human_fleet, bot_shots):
                    self._remove_game(guild_id, channel_id)
                    await self._edit_message(
                        interaction,
                        game,
                        status="finished",
                        winner_slot=2,
                    )
                    if close_picker:
                        await interaction.edit_original_response(content=confirmation, embed=None, view=None)
                    return

                game["turn"] = 1
                self._set_game(guild_id, channel_id, game)
                await self._edit_message(interaction, game)
                if close_picker:
                    await interaction.edit_original_response(content=confirmation, embed=None, view=None)
                return

            game["last_action"] = action
            game["turn"] = target_slot
            self._set_game(guild_id, channel_id, game)
            await self._edit_message(interaction, game)

        if close_picker:
            await interaction.edit_original_response(content=confirmation, embed=None, view=None)

    async def request_resign(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild_id is None
            or interaction.channel_id is None
            or interaction.message is None
        ):
            await interaction.response.send_message(
                "That Battleships game is no longer available.",
                ephemeral=True,
            )
            return

        game = self._get_game(interaction.guild_id, interaction.channel_id)
        if not game or int(game.get("message_id") or 0) != interaction.message.id:
            await interaction.response.send_message(
                "That is not the current Battleships game in this channel.",
                ephemeral=True,
            )
            return

        if str(game.get("phase") or "active") != "active":
            await interaction.response.send_message(
                "The battle hasn't started yet. The game starter can use Cancel instead.",
                ephemeral=True,
            )
            return

        resigned_slot = self._slot_for_user(game, interaction.user.id)
        if resigned_slot is None:
            await interaction.response.send_message(
                "You are not playing this Battleships game.",
                ephemeral=True,
            )
            return

        winner_slot = self._other_slot(resigned_slot)
        winner_name = self._slot_label(game, winner_slot)
        await interaction.response.send_message(
            f"🏳️ **Resign this Battleships game?**\n"
            f"This will give **{winner_name}** the win. Nothing happens unless you confirm.",
            view=ResignConfirmView(
                self,
                source_message_id=interaction.message.id,
                user_id=interaction.user.id,
            ),
            ephemeral=True,
        )

    async def resign(
        self,
        interaction: discord.Interaction,
        *,
        source_message_id: int | None = None,
    ) -> bool:
        await interaction.response.defer()

        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.followup.send(
                "That Battleships game is no longer available.",
                ephemeral=True,
            )
            return False

        message_id = int(source_message_id or (interaction.message.id if interaction.message else 0))
        if not message_id:
            await interaction.followup.send(
                "That Battleships game is no longer available.",
                ephemeral=True,
            )
            return False

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != message_id:
                await interaction.followup.send(
                    "That is not the current Battleships game in this channel.",
                    ephemeral=True,
                )
                return False

            if str(game.get("phase") or "active") != "active":
                await interaction.followup.send(
                    "The battle hasn't started yet. The game starter can use Cancel instead.",
                    ephemeral=True,
                )
                return False

            resigned_slot = self._slot_for_user(game, interaction.user.id)
            if resigned_slot is None:
                await interaction.followup.send(
                    "You are not playing this Battleships game.",
                    ephemeral=True,
                )
                return False

            winner_slot = self._other_slot(resigned_slot)
            self._remove_game(guild_id, channel_id)
            self._record_result(guild_id, game, winner_slot)
            await self._edit_message(
                interaction,
                game,
                status="resigned",
                winner_slot=winner_slot,
                resigned_slot=resigned_slot,
            )
            return True

    async def cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        if (
            interaction.guild_id is None
            or interaction.channel_id is None
            or interaction.message is None
        ):
            await interaction.followup.send(
                "That Battleships game is no longer available.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        async with self._lock_for(guild_id, channel_id):
            game = self._get_game(guild_id, channel_id)
            if not game or int(game.get("message_id") or 0) != interaction.message.id:
                await interaction.followup.send(
                    "That is not the current Battleships game in this channel.",
                    ephemeral=True,
                )
                return

            if interaction.user.id != self._player_id(game, 1):
                await interaction.followup.send(
                    "❌ Only the game starter can cancel.",
                    ephemeral=True,
                )
                return

            self._remove_game(guild_id, channel_id)
            await self._edit_message(
                interaction,
                game,
                status="cancelled",
                cancelled_by=interaction.user.id,
            )


class BattleshipsCog(commands.Cog):
    GAME_META = {
        "key": "battleships",
        "label": "Battleships",
        "kind": "head_to_head",
        "result_word": "win",
        "description": "Sink the enemy fleet on a 10×10 grid",
        "emoji": "🚢",
        "requires_opponent": True,
    }

    HELP_META = {
        "title": "Battleships",
        "summary": "Persistent Battleships for two players or one player vs Computer.",
        "details": (
            "Use /battleships and optionally choose an opponent. Leave the opponent blank "
            "for Computer mode, or choose Battleships from /games. Each player privately "
            "sets or randomises their fleet and presses Ready. During play, Fire opens a "
            "private target picker: choose a row, then an available column. Games have no "
            "timeout and survive normal Railway restarts."
        ),
    }

    def __init__(self, bot: commands.Bot, service: BattleshipsService) -> None:
        self.bot = bot
        self.service = service
        register_game(
            "battleships",
            label="Battleships",
            kind="head_to_head",
            result_word="win",
        )

    async def start_game(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
    ) -> None:
        await self.service.start_game(interaction, opponent)

    async def start_computer_game(self, interaction: discord.Interaction) -> None:
        await self.service.start_computer_game(interaction)

    @app_commands.command(name="battleships", description="Play Battleships")
    @app_commands.describe(
        opponent="Who to play against — leave blank to play the Computer"
    )
    async def battleships(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None = None,
    ) -> None:
        log_cmd("battleships", interaction)
        if not await ensure_deferred(interaction, ephemeral=False):
            return

        if opponent is None:
            await self.start_computer_game(interaction)
        else:
            await self.start_game(interaction, opponent)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "settings"):
        bot.settings = SettingsManager(bot.hot_config)

    service = BattleshipsService(bot)
    bot.add_view(BattleshipsView(service))
    bot.add_view(BattleshipsSetupView(service))

    cog = BattleshipsCog(bot, service)
    bind_public_cog(cog, bot, include_admin=True)
    await bot.add_cog(cog)
