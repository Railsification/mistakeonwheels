# HotBot Update Manifest — 2026-08-20

Upload these replacement files to the exact paths shown:

- `cogs/hangman.py` — unversioned -> **v1.0.0**
  - Adds standard `GAME_META`.
  - Adds structured Goal / How to Play / Rules metadata.
  - Adds the required exact-path header.

- `cogs/rebus.py` — unversioned -> **v1.0.0**
  - Adds standard `GAME_META`.
  - Adds structured Goal / How to Play / Rules metadata.

- `cogs/games/games.py` — **v1.0.0 -> v1.1.0**
  - Adds a **How to Play** button to `/games`.
  - Adds complete current-game rules fallback for all shipped games.
  - Uses each cog's structured `HELP_META` first, so future games remain self-describing and auto-discovered.

Also included:

- `BOT_DEVELOPMENT_RULES.md` — consolidated permanent development checklist/source of truth.

This batch does **not** move the older root game cogs into `cogs/games/`; it only changes the two metadata cogs and the central games menu, as agreed for this smaller pass.
