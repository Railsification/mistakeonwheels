# HotBot — GitHub Copilot repository instructions

Repository: `Railsification/mistakeonwheels`.

Before changing any file, **read and follow** these two files, in order:

1. `BOT_DEVELOPMENT_RULES.md` — mandatory authoritative development rules.
2. `AGENTS.md` — mandatory repository-wide agent workflow, validation and delivery rules.

Never assume previously generated code is current. Read latest source, inspect callers,
command registration, permissions, storage, restart recovery, data compatibility and
related dependencies before modification.

Every modified versioned Python file **must increment `__version__`**. Every
complete Python replacement must begin with `# <exact/repo/path.py>`. Preserve
existing behaviour and public names unless explicitly asked to change them.

Run `python -m unittest discover -s tests -v`, syntax validation and the changed-
file version gate. Explain every untested path. Complete replacement files and a ZIP
with repository paths are required whenever delivering changes to the user.

The GitHub workflow is an additional safeguard; it does not prove live Discord
runtime behaviour or replace the authoritative rules.
