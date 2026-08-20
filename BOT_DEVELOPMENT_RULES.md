# HotBot Development Rules

Spec version: 1.0.0
Last consolidated: 2026-08-20

This file is the source-of-truth checklist for future HotBot work. New work must preserve these rules unless a later explicit instruction supersedes one of them.

## 1. Restart persistence is mandatory

- ALL active games and active interactive bot state must survive normal bot/Railway restarts and deployments.
- The exact current interaction must continue after restart; do not silently reset or abandon it.
- Persist every piece of state needed to continue, including hidden state such as dice rolls, private choices, boards, turns, hints, votes, setup progress and pending workflow state.
- Save state before acknowledging an interaction when losing the new state during a restart would change the outcome.
- Re-register persistent Discord views/buttons/selects after startup using stable `custom_id` values and `timeout=None` where the interaction is intended to persist.
- Long-term data such as leaderboards/stats must also persist.
- An interaction may expire only when that specific interaction is intentionally designed to expire. Example: the private `/games` launcher menu may expire after its short selection window; a game started from it may not.

## 2. Game cog standard

Every game cog must expose standard metadata so `/games` can discover it without editing the central menu:

- `GAME_META["key"]`
- `GAME_META["label"]`
- `GAME_META["kind"]`
- `GAME_META["result_word"]`
- `GAME_META["description"]`
- `GAME_META["emoji"]`
- `GAME_META["requires_opponent"]`
- A callable `start_game` on the cog or its service.
- Two-player games that support solo play expose `start_computer_game`.

New games must auto-discover through the registry. Do not hardcode a new game into the `/games` menu just to make it appear.

## 3. Rules / How to Play are mandatory

Every game must provide user-facing help. Its `HELP_META` should contain:

- `title`
- `summary`
- `goal`
- `how_to_play`
- `rules`
- `details`

The help must explain, as relevant:

- Objective / how to win.
- What the player presses/selects/types.
- Turn order.
- Win, loss and draw conditions.
- Special rules and forced actions.
- Computer-mode behaviour.
- Whether Computer games affect the leaderboard.
- Important persistence/expiry behaviour.

`/games` must expose a **How to Play** control for the selected game. A new game should supply its own structured help metadata so it does not require a central menu edit.

## 4. Computer mode

- Two-player games should include a Computer/AI opponent mode where practical.
- `/games` should allow the player to choose another member or Computer for supported games.
- Computer games are practice unless explicitly designed otherwise and must not change PvP leaderboard/head-to-head results.

## 5. Stats and storage

- Persistent game data and stats are per-guild unless a feature is explicitly global.
- Use the shared `core.storage` helpers and the configured persistent data directory; do not rely on Railway's ephemeral filesystem for state that must survive deployments.
- Result recording must be idempotent so retries/restarts cannot double-award a win/solve.
- Existing leaderboards/stats must never be wiped by a restart or ordinary code update.

## 6. Game UX

- Prefer buttons/selects/modals over forcing users to type awkward coordinates or long command arguments.
- Show legal/available moves when practical.
- Do not reveal hidden choices/rolls before the game rules say they should be revealed.
- Irreversible actions such as resign/end/cancel should not happen accidentally; use confirmation where accidental activation would materially affect a live game.
- Keep started games in their existing message where practical instead of spamming replacement messages.

## 7. File layout and compatibility

- Game cogs belong under `cogs/games/` as the target structure. Existing root game cogs may be moved in a controlled cleanup, but do not leave duplicate root + `cogs/games/` copies loaded at the same time.
- The first line of every complete code file sent must be its exact repo path, for example `# cogs/games/dice.py`.
- Never rename or remove an existing public/shared function name merely to refactor it. Preserve compatibility with wrappers/aliases when required.
- Avoid creating two different registration/loading systems for the same feature class.

## 8. Versioning and delivery

- Every changed cog/file sent as a replacement must have an explicit `__version__` where applicable.
- Every time a versioned file is changed and sent, its version must increase. Never resend changed code under the same version.
- When a previously unversioned cog is first updated, add a baseline version.
- Every delivery must state `old version -> new version` for each changed file.
- Send complete replacement files, not partial snippets/diffs, unless a patch is explicitly requested.
- When multiple files are changed, provide a ZIP preserving exact repo paths plus individual files when practical.

## 9. Before declaring a game fix complete

For affected interactive games, verify at minimum:

1. Game starts normally.
2. State changes are saved.
3. Restart occurs mid-game.
4. Existing message controls work after restart.
5. Exact board/turn/hidden state is retained.
6. Game can finish normally after restart.
7. Result/stats record once only.
8. Computer mode still works when supported.
9. `/games` discovery and How to Play still work.

A code review that merely sees `timeout=None` is not enough to claim restart persistence.
