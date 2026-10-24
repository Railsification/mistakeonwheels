RON BURGUNDY v2.7.3 — MODULAR EVENT INTELLIGENCE + ALLIANCE PLANS
=================================================================

THIS IS AN UPGRADE TO THE EXISTING MONITOR, NOT A NEW BOT. Previous
World Map idle, calendar OCR, Alliance Chat/Mail, task scanning, saved
calibration, event loader, remote bridge and restart persistence are kept.

EVENT SOURCES ARE PURPOSE-SPECIFIC
- Calendar: upcoming event DAY/CYCLE, including forecast of multi-day phases.
  It does NOT supply a Foundry/Canyon/Tundra League legion battle clock.
- Task List: explicit UTC match start per legion. The parser requires the
  event name, battle/legion context, UTC and a full date+hour. Unknown or
  ambiguous OCR is NOT announced as a confirmed slot.
- Event-specific Python modules: known voting, registration, matchmaking and
  currency/reset rules. Foundry, Canyon, Tundra Arms League and Frostdragon
  have different phase models, not copied week-long generic event dates.
- Alliance notices, direct officer messages and alliance mail/chat: potential
  private BiA and SvS directives. OCR extracts *untrusted suggestions* only.
  A manager approves any OCR suggestions before Ron publishes instructions.
- Approved per-guild instructions entered via Discord or authorised DMs:
  replace generic BiA/SvS advice; day 1..2 and day 1..5 respectively.

CALCULATED ALERTS
- Registration ends: one hour before the official UTC phase boundary, only
  if a confidently recognised calendar anchor agrees with the weekday rule.
- Confirmed task-list match: distinct Legion 1/2 alert on first discovery and
  last call about 15 minutes (stable +/-2 minutes) before each start.
- Final event/item shop close: about 15 minutes (+/-2) before a verified
  closing time or a rule-derived final UTC reset; carryover items are NOT
  misrepresented as expiring.
- Arena and Stamina: daily optional "check remaining" reminders about
  15 minutes (+/-2) before 00:00 UTC; no assumption the user has any left.
- Advance calendar heads-ups: initial reliable appearance; no invented
  exact battle hour. Existing deduplication persists across reboots.

RANDOMNESS
- Scheduling/reminder offsets are deterministic per event/day/purpose so
  restarts do not randomly shift the deadline or issue duplicates.
- Existing calibrated taps and pauses use bounded random position/wait jitter.
- Scanning remains read-only in WoS. Unknown navigation screens are not tapped
  blindly; calibrations and all saved state are retained.

DISCORD
- Existing /ron status, events, alerts, alert, rule, info, interval, channel,
  retry_failed, check and schedule remain available.
- /ron plan_set (managers): approve BiA/SvS instructions for a UTC start date
  and day index; /ron plan views saved per-guild instructions.
- /ron plan_editor (managers): authorise/revoke a specific user's DM access.
- /ron plan_pending and /ron plan_approve: inspect and approve OCR leads.
- Authorised DM format: ron plan bia | YYYY-MM-DD | 1 | <instructions>
  (or svs with day 1..5). DMs never implicitly choose among multiple guilds.
- /ron check: select Task List, Alliance Notice or Personal Message where the
  screen route has been calibrated. No uncalibrated tapping.

DYNAMIC EVENT MODULES
- The 85 built-in event modules remain in tools/ron_burgundy/events/*.py.
- New modules added to /etc/ron-burgundy/events/*.py load automatically and
  remain untouched by future application upgrades. A plugin may declare its
  own build_notices(record,now) without changing the main engine.
- Not every event has a verified fixed length; unknown timings remain unknown.

REMOTE GITHUB UPDATES
- Once the full tools/ron_burgundy/ and packaged GitHub release workflow are
  committed to GitHub, approved ron-pi-vX.Y.Z releases are built from source
  after tests and downloaded by the Pi on its three-hour timer (randomised
  delay up to 15 minutes). Release SHA256 and safe ZIP paths are checked.
- THIS PACKAGE CANNOT TURN ON RELEASE PUBLISHING BY ITSELF: the Pi code and
  GitHub workflow are not yet in the repo. They must be committed once.
- Existing /etc config and /var/lib SQLite persist across updates; current
  release plus one versioned rollback is kept. Obsolete Ron ZIPs are cleaned.

LIMITATIONS
- Screens must actually show the date/time; no public API is used. OCR may
  fail; unknown/uncalibrated screens result in a reported failed check.
- Personal-message and Alliance Notice scanning requires saved calibration.
- Only officer-approved OCR suggestions become alliance instructions.
- All offline tests are synthetic; live tablet, Pi reboot, and production
  Railway/Discord delivery still require verification after installation.

See INSTALL_AND_CONFIGURATION.txt, EVENT_PLAYBOOKS.md, FILES_AND_VERSIONS.txt.


v2.7.3 — CHECK CADENCE AND FLAME & FANGS PREPARATION
- Calendar: ONE automatic check per UTC day, at 00:05 UTC by default.
  A user can still explicitly request a one-off /ron check Calendar.
- Task List: FOUR automatic checks per UTC day, near 01:15, 07:15,
  13:15 and 19:15 UTC (each delayed by 0..150 stable seconds); no
  rapid replay of missed scans following downtime. Checks also trigger
  if the original 'To-do added' banner appears, or the red badge appears
  beside the CALIBRATED UTC clock/Task List control. We check ONLY that
  control's badge, never other red dots on unrelated game buttons.
  Persistently visible badges/banners do not cause repeated scans.
  Existing per-server enabled=False opt-outs are preserved.
- Flame & Fangs: the per-event flame_and_fangs.py module delivers a single
  24-hour-prep alert on the day before the confidently recognised event start.
  The full UTC sequence is included: 00:00 normal Intel claims, 08:00
  complete regular AND Agnes Intel but do not claim either set of rewards,
  16:00 DO NOTHING, then collect saved rewards once the event starts at
  the next 00:00 UTC reset. The full event guide separately explains
  Crystallite Core/Firebeast earning and the final spend-only day.
  If an event is first recognised late on the prep day, Ron still sends a
  relevant remaining-steps advisory rather than inventing missed Intel.
  No advance alert is sent from guessed or clipped calendar rows.
- Reminder identities and job run keys stay persistent across restarts.
- The red-dot detector needs an existing taught 'utc_clock' button in the
  Pi's saved calibration (or an optional red_dot_region in config.json).
  Actual badge shape/position on your tablet is not yet live verified.
Sources: https://www.whiteoutsurvival.wiki/events/flame-and-fang/
         https://wos2852.com/flame-and-fangs
