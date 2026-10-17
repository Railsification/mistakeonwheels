---
applyTo: "**/*.py"
---
# HotBot Python agent rules

Always read `BOT_DEVELOPMENT_RULES.md` and `AGENTS.md` before editing.
Read the latest GitHub source and relevant callers; do not use an old attachment
as the base of a replacement file. Increment `__version__` in every changed
versioned Python file. On new/changed cog modules include `__version__`.
Make `# <exact/repo/path.py>` the first line of each replacement Python file.
Do not remove or rename existing public functions, slash commands, storage
keys, views, state or integrations without explicit instruction.

Test syntax, dependencies, command registration, affected interaction behaviour,
restart persistence and stored-state compatibility where possible. If some checks
cannot be run, state the limitation and do not claim full verification.
Deliver complete files and a ZIP with exact paths, never unrequested fragments.
