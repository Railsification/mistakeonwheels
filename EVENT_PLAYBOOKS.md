# Ron Burgundy — Event modules (v2.7.3)

85 modular event/sub-event profiles, compiled from the official Century Games
Foundry/Canyon/Trading Station help and Whiteout Survival Wiki / WSCO event
indices and individual guides. Facts verified from sources are distinguished
from unknown and variable timings; no third-party API is required.

## Extend with one file — no core rewrite

Place a trusted new Python file in either:

- `tools/ron_burgundy/events/<slug>.py` (built-in GitHub-managed release), or
- `/etc/ron-burgundy/events/<slug>.py` (local persistent custom event).

Each file must keep its path comment, `__version__`, and an `EVENT` dictionary.
Example:

```python
# tools/ron_burgundy/events/sample_event.py
from __future__ import annotations
__version__ = '1.0.0'
EVENT = {
    'name': 'Sample Event',
    'aliases': ['Event Alias'],
    'kind': 'event',
    'category': 'event',
    'summary': 'The documented game mechanics.',
    'action': 'Claim the event milestone rewards.',
    'source': 'https://the-exact-event-guide.example/',
    'advance_notice': True,
    'default_alert': True,
    'lead_days': 14,
    'days': None,  # set ONLY when reliably documented
    'final_reset_warning': True, # requires a trusted end time or duration
    'carryover': '',
}
```

`event_rules.py` discovers new files automatically; `event_planner.py`
reads their metadata. For unusual rules, a file may also declare
`build_notices(record, now) -> list[notification]` (documented by tests).
No changes are required in Discord slash commands, scanner, or other events.

### Evidence rules

- Recognised Calendar bars with a **nonclipped start** and >=0.85 name
  confidence may forecast upcoming dates and, when duration is published,
  a **rule-derived** reset deadline. Unsupported or clipped bars cannot.
- Exact start/end UTC time from a recognised event detail popup takes priority.
- A seven-day Foundry/Canyon Calendar range is NOT a seven-day battle.
- Calendar never invents per-legion chosen battle hours.
- Existing manual alert opt-outs remain effective; reminders are one-shot.
- Unknown future events are not silently assigned an invented duration.

## Built-in event profiles

- **Alliance Championship** — `events/alliance_championship.py`; Register troops/formation before the deadline and confirm assigned lane. Source: https://wiki.wosforge.org/wiki/Events
- **Alliance Mobilization** — `events/alliance_mobilization.py`; Complete and claim alliance missions before reset; coordinate high-value mission refreshes. Source: https://wiki.wosforge.org/wiki/Events
- **Alliance Showdown** — `events/alliance_showdown.py`; Complete the current day’s point objectives before UTC reset; save resources for high-value stages. Source: https://wiki.wosforge.org/wiki/Events
- **Armament Competition** — `events/armament_competition.py`; Spend gear materials toward reachable milestone rewards before event reset. Source: https://wiki.wosforge.org/wiki/Events
- **Aurora Melody** — `events/aurora_melody.py`; Check current daily missions and claim completed event rewards. Source: https://www.whiteoutsurvival-community.com/tools/wiki/events.html
- **Bear Hunt** — `events/bear_hunt.py`; Check the alliance-selected Bear Hunt start, prepare rally heroes and free marching queues. Source: https://wiki.wosforge.org/wiki/Events
- **Beast Whisperer** — `events/beast_whisperer.py`; Claim beast hunt milestone rewards while the event is open. Source: https://www.whiteoutsurvival.wiki/events/
- **Brothers in Arms** — `events/brothers_in_arms.py`; Kill event active: shield or coordinate sanctioned PvP; check alliance instructions before attacking. Source: https://wiki.wosforge.org/wiki/Events
- **Canyon Clash** — `events/canyon_clash.py`; R4/R5: confirm both legion registrations and battle slots before sign-ups close. Source: https://centurygames.helpshift.com/hc/en/64-whiteout-survival/faq/7874-what-is-canyon-clash/
- **Champions' Eve** — `events/champions_eve.py`; Claim available preparation rewards before the event moves into its next stage. Source: https://www.whiteoutsurvival.wiki/events/
- **Championship Prediction** — `events/championship_prediction.py`; Submit eligible predictions before the prediction window closes. Source: https://www.whiteoutsurvival.wiki/events/
- **City Development** — `events/city_development.py`; Plan construction speedups/Fire Crystals for the point target. Source: https://www.whiteoutsurvival.wiki/events/
- **Crazy Joe** — `events/crazy_joe.py`; Recall troops and coordinate city/Headquarters reinforcements before alliance defence waves. Source: https://wiki.wosforge.org/wiki/Events
- **Crystal Lab** — `events/crystal_lab.py`; Check active crystal milestones and claim earned rewards. Source: https://www.whiteoutsurvival-community.com/tools/wiki/events.html
- **Crystal Reactivation** — `events/crystal_reactivation.py`; Complete the active crystal milestones and claim any end-of-event chests. Source: https://www.whiteoutsurvival.wiki/events/
- **Daybreak Expedition** — `events/daybreak_expedition.py`; Complete expedition objectives and collect available milestone rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **Daybreak Island** — `events/daybreak_island.py`; Complete island objectives and collect time-limited rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **Deadshot** — `events/deadshot.py`; Save or use tickets in the active reward period as preferred. Source: https://www.whiteoutsurvival.wiki/events/
- **Defeat Nearby Beasts** — `events/defeat_nearby_beasts.py`; Complete beast and rally targets to claim the daily/event milestones. Source: https://wiki.wosforge.org/wiki/Events
- **Develop New Tech** — `events/develop_new_tech.py`; Time research upgrades for the current points window. Source: https://www.whiteoutsurvival.wiki/events/
- **Dragonborn's Truck** — `events/dragonborn_s_truck.py`; Claim available truck rewards and check whether event currency expires before the shop closes. Source: https://www.whiteoutsurvival.wiki/events/
- **Dreamscape Memory** — `events/dreamscape_memory.py`; Check current missions and claim available completion rewards before the event closes. Source: https://www.whiteoutsurvival.wiki/events/
- **Exclusive Gear Forge** — `events/exclusive_gear_forge.py`; Review current gear rewards and decide whether to spend before event expiry. Source: https://www.whiteoutsurvival.wiki/events/
- **Fishing Tournament** — `events/fishing_tournament.py`; Complete your remaining fishing attempts and claim milestone rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **Flame and Fangs** — `events/flame_and_fangs.py`; Complete intel missions and spend relevant event currency after checking its expiry. Source: https://wiki.wosforge.org/wiki/Events
- **Fortress Battles** — `events/fortress_battles.py`; R4/R5: confirm fortress or stronghold registration and scheduled battle start; members check assignments. Source: https://wiki.wosforge.org/wiki/Events
- **Foundry Battle** — `events/foundry_battle.py`; R4/R5: finalise both legion rosters and time slots before registration closes. Members: confirm your assigned legion. Source: https://centurygames.helpshift.com/hc/en/64-whiteout-survival/faq/6576-foundry-schedule/
- **Frost Star** — `events/frost_star.py`; Check current exchange offers and official expiry policy. Source: https://www.whiteoutsurvival-community.com/tools/wiki/events.html
- **Frostdragon Tyrant** — `events/frostdragon_tyrant.py`; Confirm your state and legion sign-up, qualified roster and battle time. Source: https://wiki.wosforge.org/wiki/Events
- **Frostfire Mine** — `events/frostfire_mine.py`; Register and choose a battle time; set up mining heroes before entry. Source: https://wiki.wosforge.org/wiki/Events
- **Frostwind Journey** — `events/frostwind_journey.py`; Complete relevant milestones and claim rewards; confirm dates with the event screen. Source: https://www.whiteoutsurvival.wiki/events/
- **Frosty Fortune** — `events/frosty_fortune.py`; Complete daily tasks and open chests; claim expiring event-shop rewards as appropriate. Source: https://wiki.wosforge.org/wiki/Events
- **Gina's Revenge** — `events/gina_s_revenge.py`; Hunt the event beasts for required shards and claim rewards. Source: https://wiki.wosforge.org/wiki/Events
- **Grow Your Heroes** — `events/grow_your_heroes.py`; Use hero upgrades to reach available milestone scores. Source: https://www.whiteoutsurvival.wiki/events/
- **Hall of Chiefs** — `events/hall_of_chiefs.py`; Plan the active day’s scoring stage; spend only for reachable milestones. Source: https://wiki.wosforge.org/wiki/Events
- **Hall of Heroes** — `events/hall_of_heroes.py`; Check older-generation hero shard and widget shop offers before the window closes. Source: https://wiki.wosforge.org/wiki/Events
- **Hero Ascension Training** — `events/hero_ascension_training.py`; Spend enhancement items only if reachable rewards justify it. Source: https://www.whiteoutsurvival.wiki/events/
- **Hero Rally** — `events/hero_rally.py`; Check current available hero shards and claim time-limited event rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **Hero's Mission** — `events/hero_s_mission.py`; Complete Reaper hunts and claim event rewards. Source: https://wiki.wosforge.org/wiki/Events
- **Home & Beyond** — `events/home_beyond.py`; Finish event missions and claim earned rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **Icefire Warhymn** — `events/icefire_warhymn.py`; Check the battle schedule and follow your team roster assignments. Source: https://www.whiteoutsurvival.wiki/events/
- **Icefire Warhymn League** — `events/icefire_warhymn_league.py`; Check registration and match times; coordinate with your squad. Source: https://www.whiteoutsurvival.wiki/events/
- **Journey Treasures** — `events/journey_treasures.py`; Claim available rewards and verify shop expiry from the game. Source: https://www.whiteoutsurvival.wiki/events/
- **Journey of Light** — `events/journey_of_light.py`; Complete outstanding flights and claim journey milestones; unopened flight chests auto-open, and Pocket Watches carry over. Source: https://www.whiteoutsurvival.wiki/events/journey-of-light/
- **King of Icefield** — `events/king_of_icefield.py`; Check the current day’s scoring categories; claim milestone chests before reset. Source: https://wiki.wosforge.org/wiki/Events
- **Lucky Wheel** — `events/lucky_wheel.py`; Use saved gems during the wheel’s current window; verify spins and claim milestone rewards. Source: https://wiki.wosforge.org/wiki/Events
- **Mercenary Prestige** — `events/mercenary_prestige.py`; Finish your selected monster difficulty and claim all cleared stages before reset. Source: https://wiki.wosforge.org/wiki/Events
- **Mia's Fortune** — `events/mia_s_fortune.py`; Collect tokens and complete reward boards; verify token carryover before spending. Source: https://wiki.wosforge.org/wiki/Events
- **Officer Project** — `events/officer_project.py`; Train/promote troops and spend items only on the current scoring objectives. Source: https://wiki.wosforge.org/wiki/Events
- **Plan your City** — `events/plan_your_city.py`; Complete city planning milestones and claim available chests. Source: https://www.whiteoutsurvival.wiki/events/
- **Power Up** — `events/power_up.py`; Check power-gain milestones before committing materials. Source: https://www.whiteoutsurvival.wiki/events/
- **Return to Tundra** — `events/return_to_tundra.py`; Finish return missions and claim milestone rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **Romance Season** — `events/romance_season.py`; Finish limited-time stages and claim milestone rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **SVS Preparation** — `events/svs_preparation.py`; Check the current five-day SvS scoring stage and finish milestones before daily reset. Source: https://wiki.wosforge.org/wiki/Events
- **Shining City Pack** — `events/shining_city_pack.py`; Review the limited-time offer; no automatic deadline alert without confirmed expiration. Source: https://www.whiteoutsurvival.wiki/events/
- **Silver Shell Shop** — `events/silver_shell_shop.py`; Spend only if the item is worthwhile; verify whether shells carry over. Source: https://wiki.wosforge.org/wiki/Events
- **Snowbusters** — `events/snowbusters.py`; Use the daily event resources and clear remaining stages before the final reset. Source: https://www.whiteoutsurvival.wiki/events/
- **Stand of Arms** — `events/stand_of_arms.py`; Spend gear materials on milestones that matter. Source: https://www.whiteoutsurvival.wiki/events/
- **State Merger** — `events/state_merger.py`; Read official state-merger notices and deadlines; no inferred dates. Source: https://www.whiteoutsurvival.wiki/events/
- **State Transfer** — `events/state_transfer.py`; Check invitation and transfer requirements; use official transfer dates. Source: https://wiki.wosforge.org/wiki/Events
- **State of Power** — `events/state_of_power.py`; Check the current SvS stage and Castle Battle start; save materials for preparation scoring. Source: https://wiki.wosforge.org/wiki/Events
- **Sunfire Castle** — `events/sunfire_castle.py`; Prepare for today’s Castle Battle; confirm start time and formation with alliance leadership. Source: https://wiki.wosforge.org/wiki/Events
- **Supply Drop** — `events/supply_drop.py`; Collect available supply-drop rewards; verify event end in-game. Source: https://wiki.wosforge.org/wiki/Events
- **Symphony of Change** — `events/symphony_of_change.py`; Check unlock/preparation milestones; avoid wasting upgrade materials. Source: https://wiki.wosforge.org/wiki/Events
- **The Labyrinth** — `events/the_labyrinth.py`; Finish available labyrinth stages and collect progression rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **Treasure Hunter** — `events/treasure_hunter.py`; Finish desired ice floors, collect the daily Treasure Warrant pickaxes and claim available rewards. Source: https://www.whiteoutsurvival.wiki/events/treasure-hunter/
- **Treasure Search** — `events/treasure_search.py`; Use available searches within the Treasure Hunter window; pickaxes carry over. Source: https://www.whiteoutsurvival.wiki/events/treasure-hunter/
- **Treasure Warrant** — `events/treasure_warrant.py`; Collect daily pickaxes: Polar Terror rally, alliance contributions and troop training objectives. Source: https://www.whiteoutsurvival.wiki/events/treasure-hunter/
- **Trial Event** — `events/trial_event.py`; Complete trial stages and claim unlocked rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **Trusted Chief** — `events/trusted_chief.py`; Complete current objectives and claim milestone rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **Tundra Adventure** — `events/tundra_adventure.py`; Finish exploration missions and claim available rewards before the event closes. Source: https://www.whiteoutsurvival.wiki/events/
- **Tundra Album** — `events/tundra_album.py`; Collect available album pieces and redeem completed sets before event reset. Source: https://www.whiteoutsurvival.wiki/events/
- **Tundra Arms League** — `events/tundra_arms_league.py`; Check the voting, sign-up and battle phases. Register your legion before the closing reset. Source: https://centurygames.helpshift.com/hc/en/64-whiteout-survival/faq/8409-how-is-tundra-arms-league-held/
- **Tundra Games** — `events/tundra_games.py`; Use daily refreshes and claim seasonal rewards before the final reset. Source: https://www.whiteoutsurvival.wiki/events/
- **Tundra Star** — `events/tundra_star.py`; Compare limited-time offers and verify any closing shop deadline. Source: https://www.whiteoutsurvival.wiki/events/
- **Tundra Trade Route** — `events/tundra_trade_route.py`; Check convoy dispatches and raid attempts; use the event window to claim any available rewards. Source: https://www.whiteoutsurvival.wiki/events/
- **Tundra Trading Station** — `events/tundra_trading_station.py`; Prepare surplus shards and widgets to trade for vouchers; compare shop offers before spending. Source: https://centurygames.helpshift.com/hc/en/64-whiteout-survival/section/1271-tundra-trading-station/
- **Twin Star Adventure** — `events/twin_star_adventure.py`; Complete available event tasks and claim milestones. Source: https://www.whiteoutsurvival-community.com/tools/wiki/events.html
- **Vault of Enigma** — `events/vault_of_enigma.py`; Purchase the eligible daily chest to complete the five-day sequence; claim the final selectable reward. Source: https://www.whiteoutsurvival.wiki/events/vault-of-enigma/
- **Vision of Dawn** — `events/vision_of_dawn.py`; Finish and claim the day’s mission track before reset. Source: https://www.whiteoutsurvival.wiki/events/vision-of-dawn/
- **Wander Theater** — `events/wander_theater.py`; Complete current event stages and claim remaining rewards. Source: https://www.whiteoutsurvival-community.com/tools/wiki/events.html
- **Wild Brawl** — `events/wild_brawl.py`; Enter available matchmaking windows and use remaining daily matches. Source: https://www.whiteoutsurvival.wiki/events/wild-brawl/
- **Winter Siege** — `events/winter_siege.py`; Check the battle period and alliance instructions before joining. Source: https://www.whiteoutsurvival-community.com/tools/wiki/events.html
- **Wish Station** — `events/wish_station.py`; Check event draws and use currency before reset only if its expiry is confirmed. Source: https://www.whiteoutsurvival-community.com/tools/wiki/events.html
- **Working Overtime** — `events/working_overtime.py`; Finish available missions and claim remaining milestone rewards. Source: https://www.whiteoutsurvival.wiki/events/


## v2.7.0: Event sources and alliance rules

**Task List**: `event_intelligence.task_battle_slots()` extracts an explicitly
labelled UTC date and match hour. Foundry, Canyon Clash and Tundra Arms League
have separate Legion 1/2 slots; Frostdragon and Fortress use a match slot when
confirmed. Old calendar bars NEVER become a battle hour. OCR ambiguous/missing
hours stay unconfirmed. A recognised new battle slot is announced once, and a
repeatable +/-2-minute jitter produces a last call near 15 minutes before it.

**Event-specific phases**: Foundry 2d voting + 2d registration + 2d matchmaking;
Canyon Clash Sat-Sun voting/Mon-Tue registration/Wed matchmaking; Tundra Arms
League Mon-Tue voting/Wed-Fri registration/Sat matchmaking followed by
elimination/championship stages; Frostdragon Mon-Tue sign-up/Wed-Thu dispatch
and its separately published battle schedule. Each module carries its own
`phase_model`; a phase cutoff is only calculated from a trusted calendar
anchor that matches its official weekday.
Sources: https://centurygames.helpshift.com/hc/en/64-whiteout-survival/faq/6576-foundry-schedule/
https://centurygames.helpshift.com/hc/en/64-whiteout-survival/faq/8409-how-is-tundra-arms-league-held/
https://centurygames.helpshift.com/hc/en/64-whiteout-survival/faq/8420-what-is-frostdragon-tyrant/

**Custom alliance plan**: `ron plan_set` or an authorised DM saves the exact
alliance's day-by-day BiA and SvS orders (per Discord guild), with explicit
UTC event start date. OCR of Tanke messages/mail/Alliance Notice creates a
private proposal only; officers approve via `/ron plan_approve` because OCR
cannot authenticate the sender or determine the correct day on its own.

**Reset/last call**: ~15 minutes before daily 00:00 UTC reset (+/-2 minutes)
Ron can remind members to check Arena and collect remaining Stamina; the
message does not assume unclaimed resources exist. Event-shop expiries are
scheduled only with a confirmed real deadline; known carried-over currencies
are never described as expiring. Alert timing is reproducible across restarts.

**Source-controlled extensibility**: `tools/ron_burgundy/events/*.py` and the
persistent `/etc/ron-burgundy/events/*.py` are automatically enumerated by
`event_rules.py`. Event-specific `build_notices()` handlers may be added as
trusted modules without changing the main monitor or Discord cog.

**Note:** 85 profiles do not mean 85 fully automated event-specific screen
parsers. Many contain recognition and descriptive guidance only. OCR and
unconfirmed event rules remain safety-limited, not silently invented.


## Flame & Fangs (v2.7.3)
The dedicated `events/flame_and_fangs.py` module now generates a real
24-hour preparation notice, not just a generic calendar heads-up.
Using a reliably anchored UTC start date, the alert is due on the day before
start and contains:
- 00:00 UTC prior day: complete/claim normally.
- 08:00 UTC prior day: complete regular AND Agnes Intel but DO NOT claim either set of rewards.
- 16:00 UTC prior day: DO NOTHING. All preparation ends at 08:00 UTC.
- At event start 00:00 UTC: claim saved normal and Agnes Intel rewards.
- Agnes Intel missions are completed by 08:00 UTC and their rewards are left unclaimed until the event starts.
- Firebeast Intel = 10 Crystallite Cores, regular Intel = 1; days 1–2 earn,
  day 3 exchange only; unspent Cores convert to Meat.
It changes the immediate advice if identified after the 08:00/16:00 window.
The alert is deduplicated by the actual event start, across Pi restarts.
Sources: https://www.whiteoutsurvival.wiki/events/flame-and-fang/
https://wos2852.com/flame-and-fangs

## Routine scanning (v2.7.3)
Calendar once each UTC day; Task List four times each UTC day plus guarded
new to-do banner / taught task-button red-dot triggers. Each event retains
its own event-specific phase rules and reminders.
