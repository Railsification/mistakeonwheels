HotBot — mandatory agent instructions
These instructions apply to the entire Railsification/mistakeonwheels repository. They apply to Codex and any other coding agent that reads this repository.
First action — before any code change
Read the root BOT_DEVELOPMENT_RULES.md in full. It is the authoritative checklist; this file supplements it and does not replace it.
Fetch/read the current relevant files from the repository. Do not assume an earlier conversation upload, local copy, or generated file is up-to-date.
Trace callers, command registrations, storage, startup recovery, persistent views, configuration, related cogs, and consequences for existing functionality.
State the intended change and affected files. Do not invent a new command when an existing admin command can be extended.
If rules conflict or cannot be satisfied, report the conflict/blocker before delivery; do not silently ignore a rule.
Non-negotiable implementation requirements
Preserve all existing working features, public command names, integrations, signatures and persistent data. Use compatibility aliases/wrappers when needed.
Implement the entire requested feature, including edge cases, failure paths and restarts. Do not drip-feed partial implementations.
For every modified versioned source file, increase its __version__. For a modified cog without one, add an initial __version__. Do not call a changed file "updated" at its old version.
For complete Python replacement files, line 1 must be the exact repo path comment, e.g. # cogs/community/suggestion_poll.py.
Before changing a Discord interaction, check that it properly registers, respects permissions, and provides accurate feedback on failure; never report success after a rejected operation.
Save every state change needed for restart persistence; avoid duplicate actions, result messages and @everyone notifications when refreshing or restarting.
Minimize code changes; do not rename functions or variables without a concrete reason.
Required validation before delivery
Run Python syntax checks on all changed Python files and report the result.
Run available relevant tests. Add or run focused tests for new logic, including negative paths. When interaction/state handling changes, explicitly check restart persistence, duplicate handling and permission behaviour.
Run python scripts/validate_hotbot_changes.py --base BASE_REF --head HEAD_REF when working with Git commits; GitHub Actions will also run this automatically.
Review the actual diff for unintended deletions, renamed commands, lost registrations, unpersisted state and unrelated edits.
Cross-check every instruction in BOT_DEVELOPMENT_RULES.md that applies to the changed files.
Distinguish syntax checked, unit tested, integration tested, and not tested. Do not invent or imply successful tests you did not run.
Mandatory delivery contract
Provide complete replacement files, not code fragments, unless a patch was explicitly requested.
Deliver files at their exact repository paths; for multiple files include a ZIP preserving the directories, and individual files when practical.
State old version → new version for every changed versioned file.
Include a concise list of changed files, behaviour changes, and actual verification results.
Explicitly state whether changes were committed, pushed, deployed, or are download-only.
Check each generated output path exists and is readable before posting its link.
Never say "done", "fixed", "tested", or "deployed" if that assertion is unsupported.
Hard stop
If any applicable item above or in BOT_DEVELOPMENT_RULES.md has not been satisfied, do not present the files as release-ready. Clearly label the blocker and finish the required checks first.