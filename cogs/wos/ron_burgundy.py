# cogs/wos/ron_burgundy.py
from __future__ import annotations
__version__ = '1.3.2'
"""HotBot-side Ron control; the Raspberry Pi initiates every connection.

Ron is configured independently in every HotBot guild. Existing Pi clients
that omit guild_id continue to work; their observations fan out to configured
guilds, each with independent alerts, jobs and council feature channels.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.storage import (configured_guild_ids, load_guild_json,
                          save_guild_json, primary_data_guild_id)
from core.logger import info, err

DATA = 'ron_control.json'
JOBS = {'calendar', 'mail', 'task_list', 'chat', 'help', 'notice', 'personal'}
DEFAULT_SCHEDULE = {
 'calendar': {'enabled': True, 'cadence': 'daily', 'utc_hour': 0, 'utc_minute': 5},
 'mail': {'enabled': True, 'cadence': 'hourly', 'utc_minute': 0},
 'task_list': {'enabled': True, 'cadence': 'hybrid', 'utc_hours': [1,7,13,19], 'utc_minute': 15},
 'chat': {'enabled': True, 'cadence': 'periodic', 'interval_seconds': 120},
 'help': {'enabled': False, 'cadence': 'periodic', 'interval_seconds': 600},
 'notice': {'enabled': True, 'cadence': 'every_other_hour', 'utc_minute': 20},
 'personal': {'enabled': False, 'cadence': 'trigger'},
}
DEFAULT_ALERTS = {
 'Foundry Battle': True, 'Canyon Clash': True, 'Bear Hunt': True,
 'Fortress Battles': True, 'Crazy Joe': True, 'Alliance Championship': True,
 "Dragonborn's Truck": False, 'Frostwind Journey': False,
 'Frostdragon Tyrant': True, 'Tundra Arms League': True, 'Sunfire Castle': True,
 "Dragonborn's Truck — spend reminder": True,
 'Task List': False, 'Alliance Mail': False, 'System Mail': False,
 'Arena': True, 'Stamina': True,
 'Treasure Hunter': True, 'Tundra Trading Station': True,
 'Brothers in Arms': True, 'Mercenary Prestige': True,
}


def utcnow():
 return datetime.now(timezone.utc).isoformat()


def baseline():
 return {'schema': 1, 'revision': 1, 'alerts': dict(DEFAULT_ALERTS),
         'schedules': json.loads(json.dumps(DEFAULT_SCHEDULE)), 'rules': {}, 'jobs': [],
         'events': {}, 'receipts': {}, 'updated_ids': {}, 'research': {},
         'channel_id': 0, 'last_seen': None, 'deliveries': {},
         'event_baseline_at': None, 'plans': {}, 'plan_editors': [], 'proposals': {}}


def normalize(raw):
 b = baseline()
 if isinstance(raw, dict):
  for k in b:
   if k not in raw:
    continue
   # last_seen begins as None but becomes an ISO-8601 string after Pi polls.
   # Comparing only against the baseline type discarded the persisted timestamp.
   if k in ('last_seen', 'event_baseline_at'):
    if raw[k] is None or isinstance(raw[k], str):
     b[k] = raw[k]
   elif isinstance(raw[k], type(b[k])):
    b[k] = raw[k]
 # Preserve explicit per-server choices, but pick up newly added alert types.
 b['alerts'] = {**DEFAULT_ALERTS, **b['alerts']}
 # Legacy guilds saved task_list as trigger-only. Keep their enabled toggle,
 # but migrate the scan cadence to include periodic checks.
 saved_task = b['schedules'].get('task_list')
 if isinstance(saved_task, dict):
  task = {**DEFAULT_SCHEDULE['task_list'], **saved_task}
  if task.get('cadence') == 'trigger':
   task['cadence'] = 'hybrid'
  b['schedules']['task_list'] = task
 return b


def safe_title(s):
 return isinstance(s, str) and 1 <= len(s.strip()) <= 110 and '\n' not in s


def parsed_utc(value):
 if not isinstance(value, str) or not value.strip():
  return None
 try:
  date = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
  return date.astimezone(timezone.utc) if date.tzinfo else None
 except ValueError:
  return None


def event_is_current(data, now=None):
 """Never announce an expired or stale scan as a current event."""
 # Pre-v2.5.3 cached event dates do not prove what time a game popup showed.
 if data.get('time_evidence') != 'detail_popup' or data.get('status') != 'times_from_popup':
  return False
 now = now or datetime.now(timezone.utc)
 observed = parsed_utc(data.get('observed_utc'))
 if observed is None or not (-timedelta(minutes=5) <= now-observed <= timedelta(hours=6)):
  return False
 end = parsed_utc(data.get('end_utc'))
 if end is not None:
  return end > now
 visible_end = data.get('visible_end_date')
 if isinstance(visible_end, str):
  try:
   return datetime.fromisoformat(visible_end[:10]).date() >= now.date()
  except ValueError:
   pass
 start = parsed_utc(data.get('start_utc'))
 if start is not None:
  return start > now
 visible_start = data.get('visible_start_date')
 if isinstance(visible_start, str):
  try:
   return datetime.fromisoformat(visible_start[:10]).date() >= now.date()
  except ValueError:
   pass
 return False


# A calendar popup for these activities describes an event cycle or a
# registration window. The actual battle/session times come from the Task List
# or the alliance-selected slot, never the Calendar start/end timestamp.
BATTLE_SLOT_EVENTS = frozenset({
 'foundry battle', 'canyon clash', 'tundra arms league',
 'frostdragon tyrant', 'sunfire castle', 'fortress battles',
 'alliance championship',
})


def is_battle_slot_event(title):
 return str(title).casefold().strip() in BATTLE_SLOT_EVENTS


def event_changes(previous, data):
 fields = ('start_utc', 'end_utc', 'visible_start_date', 'visible_end_date')
 return [field for field in fields if previous.get(field) != data.get(field)]


def alert_text(title, data):
 """None means intentionally silent, not a delivery failure."""
 safe = discord.utils.escape_mentions(str(title)[:110])
 if data.get('currency'):
  if data.get('expiry_confirmed') is not True:
   return None
  expire = parsed_utc(data.get('end_utc'))
  if expire is None or expire <= datetime.now(timezone.utc):
   return None
  currency = discord.utils.escape_mentions(str(data['currency'])[:80])
  return f'**WoS · {safe}**\nSpend remaining **{currency}** before <t:{int(expire.timestamp())}:F>. This is a confirmed final-spending reminder near closing time.'
 if data.get('notice_type') == 'urgent_text':
  summary = str(data.get('summary') or '').strip().replace(chr(96)*3, chr(39)*3)
  if not summary:
   return None
  short = discord.utils.escape_mentions(' '.join(summary.split())[:1300])
  deadline = parsed_utc(data.get('deadline_utc'))
  when = f'\nDeadline <t:{int(deadline.timestamp())}:F>' if deadline else ''
  return f'**WoS · {safe}: action required**\n{short}{when}'
 if data.get('notice_type') not in ('new_event', 'schedule_changed'):
  return None
 # Even a one-hour Calendar popup is not evidence of an alliance-selected
 # legion/battle slot. Task List reminders arrive as separate urgent notices.
 if is_battle_slot_event(title):
  return None
 if data.get('status') != 'times_from_popup' or not event_is_current(data):
  return None
 # A seven-day calendar cycle is NOT a week-long Foundry/Canyon/Castle
 # battle. Only announce a verified single-session slot, never a long
 # event-banner span. Phase and deadline notices come from the Pi plugins.
 start_window = parsed_utc(data.get('start_utc'))
 end_window = parsed_utc(data.get('end_utc'))
 if start_window and end_window and end_window-start_window > timedelta(hours=4):
  return None
 start = parsed_utc(data.get('start_utc'))
 # Rule-derived durations are not confirmed end times and must never
 # appear as precise Discord deadlines.
 end = parsed_utc(data.get('end_utc')) if data.get('duration_source') != 'rule' else None
 if start is None and end is None:
  return None
 change = data['notice_type'] == 'schedule_changed'
 header = f'**WoS · {safe} {"schedule changed" if change else "scheduled"}**'
 fields = set(data.get('changed_fields', []))
 details = []
 if start and (not change or 'start_utc' in fields):
  details.append(f'Starts <t:{int(start.timestamp())}:F>')
 if end and (not change or 'end_utc' in fields):
  details.append(f'Ends <t:{int(end.timestamp())}:F>')
 if not details:
  return None
 return header + '\n' + '\n'.join(details)


PLAN_EVENTS = {'bia': 'Brothers in Arms', 'svs': 'SvS Preparation'}
PLAN_DATE = re.compile(r'^20\d{2}-\d{2}-\d{2}$')


def validate_alliance_plan(code, start_day, plan_day, instructions):
 '''Require explicit UTC event start and day number, not a guessed schedule.'''
 if code not in PLAN_EVENTS:
  raise ValueError('Event must be bia or svs')
 try:
  parsed = datetime.strptime(start_day, '%Y-%m-%d').date()
 except (TypeError, ValueError):
  raise ValueError('Start must be YYYY-MM-DD (UTC)') from None
 if parsed.year < 2025 or parsed.year > 2035:
  raise ValueError('Start date outside supported range')
 days = 2 if code == 'bia' else 5
 if type(plan_day) is not int or not 1 <= plan_day <= days:
  raise ValueError(f'{code} day must be 1–{days}')
 if not isinstance(instructions,str) or not 8 <= len(instructions.strip()) <= 950:
  raise ValueError('Instructions must be 8–950 characters')
 return start_day, str(plan_day), instructions.strip()


def can_manage(interaction):
 u = interaction.user
 return isinstance(u, discord.Member) and (
  u.guild_permissions.manage_guild or u.guild_permissions.administrator)


class RonBurgundyCog(commands.Cog):
 ron = app_commands.Group(name='ron', description='Ron Burgundy game monitoring and alerts')
 HELP_META = {
  'title': 'Ron Burgundy monitoring',
  'summary': 'View detected WoS events, control alerts and queue Pi checks.',
  'goal': 'Keep WoS schedule and Discord announcements in sync.',
  'how_to_play': 'Use /council feature_channel_add with feature ron; use /ron events, /ron alert, /ron check, and /ron schedule.',
  'rules': 'Server managers manage their own server. The Pi only pushes changed facts.',
  'details': 'No Discord command accesses the tablet directly; the Pi collects queued requests.',
 }

 def __init__(self, bot):
  self.bot = bot
  self.secret = os.getenv('RON_BRIDGE_SECRET', '').strip()
  # Kept for callers and Pi clients configured before multi-guild support.
  self.guild_id = primary_data_guild_id(bot)
  self.port = int(os.getenv('PORT', '0') or 0)
  self._runner = None
  self._lock = asyncio.Lock()
  self._research_lock = asyncio.Lock()
  self._change = asyncio.Event()
  bind_public_cog(self, bot)
  # Mark interrupted sends uncertain; never replay them automatically.
  for guild_id in self._guild_ids():
   b = self._read(guild_id)
   changed = False
   for record in b['deliveries'].values():
    if isinstance(record, dict) and record.get('state') == 'sending':
     record['state'] = 'uncertain'
     changed = True
   if changed:
    self._save(b, guild_id)

 def _guild_ids(self):
  """Only HotBot's configured Discord guilds can own Ron data/routes."""
  ids = configured_guild_ids(self.bot)
  if self.guild_id and self.guild_id not in ids:
   ids.insert(0, self.guild_id)
  return list(dict.fromkeys(ids))

 def _read(self, guild_id=None):
  return normalize(load_guild_json(
   int(guild_id or self.guild_id), DATA, baseline()))

 def _save(self, b, guild_id=None):
  save_guild_json(int(guild_id or self.guild_id), DATA, b)

 def _signal(self, b, guild_id=None):
  b['revision'] = int(b['revision']) + 1
  self._save(b, guild_id)
  self._change.set()
  self._change = asyncio.Event()

 async def cog_load(self):
  if not self.secret or not self._guild_ids() or not self.port:
   info('Ron bridge inactive: configure RON_BRIDGE_SECRET, guild IDs and Railway PORT')
   return
  app = web.Application(client_max_size=96*1024)
  app.add_routes([
   web.get('/ron/health', self.health),
   web.get('/ron/control', self.control),
   web.post('/ron/sync', self.sync),
   web.post('/ron/research', self.research),
  ])
  runner = web.AppRunner(app, access_log=None)
  await runner.setup()
  try:
   await web.TCPSite(runner, host='0.0.0.0', port=self.port).start()
  except Exception:
   await runner.cleanup()
   raise
  self._runner = runner
  info('Ron bridge enabled on Railway PORT (multi-guild)')

 async def cog_unload(self):
  if self._runner:
   await self._runner.cleanup()
   self._runner = None

 def _auth(self, request):
  header = request.headers.get('Authorization', '')
  if not self.secret or not header.startswith('Bearer ') or not hmac.compare_digest(
   header[7:], self.secret):
   raise web.HTTPUnauthorized()

 async def _manager(self, interaction):
  if interaction.guild_id not in self._guild_ids() or not can_manage(interaction):
   await interaction.response.send_message(
    'Manage Server permission is required in this configured HotBot server.',
    ephemeral=True)
   return False
  return True

 async def health(self, request):
  return web.json_response({'service': 'ron-burgundy', 'version': __version__})

 def _control_revision(self, target_guild=None):
  """Legacy polling revision changes when ANY configured guild changes."""
  if target_guild is not None:
   return self._read(target_guild)['revision']
  gids = self._guild_ids()
  if len(gids) == 1:
   return self._read(gids[0])['revision']
  revisions = [(gid, self._read(gid)['revision']) for gid in gids]
  digest = hashlib.sha256(json.dumps(revisions, separators=(',', ':')).encode()).hexdigest()
  return int(digest[:14], 16)

 def _requested_guild(self, request, source=None):
  """Optional explicitly targeted guild for newer Pi clients.

  A missing guild stays in legacy mode: readings fan out to configured guilds.
  """
  raw = (source.get('guild_id') if isinstance(source, dict) else None)
  if raw is None:
   raw = request.query.get('guild_id')
  if raw is None:
   return None
  try:
   gid = int(raw)
  except (ValueError, TypeError):
   raise web.HTTPBadRequest(text='Invalid guild_id')
  if gid not in self._guild_ids():
   raise web.HTTPBadRequest(text='Unknown guild_id')
  return gid

 async def control(self, request):
  self._auth(request)
  target_guild = self._requested_guild(request)
  try:
   rev = int(request.query.get('revision', '0'))
   wait = min(25, max(0, int(request.query.get('wait', '0'))))
  except ValueError:
   raise web.HTTPBadRequest(text='Invalid query')

  async with self._lock:
   primary = self._read(target_guild)
   pending = any(
    j.get('state') == 'queued'
    for gid in ([target_guild] if target_guild is not None else self._guild_ids())
    for j in self._read(gid)['jobs'] if isinstance(j, dict))
   event = self._change if rev == self._control_revision(target_guild) and not pending and wait else None
  if event:
   try:
    await asyncio.wait_for(event.wait(), timeout=wait)
   except asyncio.TimeoutError:
    pass

  async with self._lock:
   gids = [target_guild] if target_guild is not None else self._guild_ids()
   guilds = {}
   all_jobs = []
   now = datetime.now(timezone.utc)
   for gid in gids:
    b = self._read(gid)
    try:
     stamp = datetime.fromisoformat(b['last_seen']) if b.get('last_seen') else None
    except (TypeError, ValueError):
     stamp = None
    if stamp is None or (now - stamp).total_seconds() > 180:
     b['last_seen'] = utcnow()
     self._save(b, gid)
    jobs = [
     {'id': j['id'], 'kind': j['kind'], 'guild_id': gid}
     for j in b['jobs'] if isinstance(j, dict) and j.get('state') == 'queued'
     and isinstance(j.get('id'), str) and j.get('kind') in JOBS]
    all_jobs.extend(jobs)
    guilds[str(gid)] = {
     'revision': b['revision'], 'alerts': b['alerts'],
     'schedules': b['schedules'], 'rules': b['rules'], 'jobs': jobs[:12],
     'plans': b['plans']}
   # Keep the original top-level protocol for pre-existing Pi bridge clients.
   primary = self._read(target_guild)
   return web.json_response({
    'revision': self._control_revision(target_guild), 'alerts': primary['alerts'],
    'schedules': primary['schedules'], 'rules': primary['rules'],
    'plans': primary['plans'], 'jobs': all_jobs[:12], 'guilds': guilds,
   })

 def _apply_update(self, b, item, guild_id):
  """Apply and persist one observation for one guild; return accepted ID."""
  ident = item.get('id')
  kind = item.get('kind')
  data = item.get('data')
  if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}', ident):
   return None
  if kind not in ('event', 'task', 'mail', 'notification', 'reminder', 'status', 'research', 'plan_proposal') or not isinstance(data, dict):
   return None
  if ident in b['updated_ids']:
   return ident

  should_notify = False
  notice = data
  name = str(data.get('title') or kind)
  if kind == 'event':
   title = data.get('title', '')
   if not safe_title(title):
    return None
   key = hashlib.sha256((title.casefold() + '|' + str(
    data.get('visible_start_date') or data.get('start_utc') or '')).encode()).hexdigest()[:32]
   previous = b['events'].get(key)
   # A newly connected Pi sends its existing calendar history in batches.
   # Baseline that history silently for 30 minutes (durable across restarts).
   if b.get('event_baseline_at') is None:
    b['event_baseline_at'] = utcnow()
   seeded = parsed_utc(b.get('event_baseline_at'))
   baseline_period = seeded is None or datetime.now(timezone.utc)-seeded < timedelta(minutes=30)
   previous_verified = (isinstance(previous, dict)
                        and previous.get('status') == 'times_from_popup'
                        and previous.get('time_evidence') == 'detail_popup')
   changes = event_changes(previous, data) if previous_verified else []
   b['events'][key] = data
   b['alerts'].setdefault(title, bool(data.get('default_alert', False)))
   is_verified = (data.get('status') == 'times_from_popup'
                  and data.get('time_evidence') == 'detail_popup')
   # Upgrading an old calendar guess into a confirmed popup is NOT a
   # game schedule change. Preserve the cache but never send a fake alert.
   meaningful = previous is None or (previous_verified and bool(changes))
   # The baseline is a cache load, not a new alliance announcement.
   # A genuine correction between two verified popups need not wait.
   confirmed_correction = (previous_verified
                           and any(f in changes for f in ('start_utc', 'end_utc')))
   should_notify = (bool(b['alerts'].get(title, False))
                    and (not baseline_period or confirmed_correction)
                    and meaningful and is_verified and event_is_current(data))
   if should_notify:
    notice = {**data, 'notice_type': 'schedule_changed' if previous else 'new_event',
              'changed_fields': changes}
  elif kind == 'plan_proposal':
   # In-game OCR is untrusted, even when it appears to name an officer.
   # Store it privately; only an authorised Discord user can approve it.
   event = data.get('event')
   summary = str(data.get('text') or '').strip()
   source = str(data.get('source') or '')
   if event not in ('bia', 'svs') or len(summary) < 20 or len(summary) > 500 or source not in (
       'alliance_mail', 'alliance_notice', 'personal_message', 'chat'):
    return None
   b['proposals'][ident] = {'event':event,'text':summary,'source':source,
                            'observed_utc':utcnow()}
   if len(b['proposals']) > 40:
    for oldkey in list(b['proposals'])[:-35]:
     b['proposals'].pop(oldkey, None)
  elif kind == 'research':
   guide = str(data.get('summary') or '').strip()
   if safe_title(name) and 20 <= len(guide) <= 4700:
    link = str(data.get('source') or '').strip()
    if link.startswith('https://') and len(link) <= 350:
     guide += '\nSource: ' + link
    b['research'][name] = guide[:5000]
  elif kind == 'status':
   job_id = data.get('job_id')
   if isinstance(job_id, str):
    for job in b['jobs']:
     if isinstance(job, dict) and job.get('id') == job_id:
      state = str(data.get('status', 'failed'))
      if state in ('queued', 'running', 'processing', 'completed', 'failed'):
       job['state'] = state
       job['detail'] = str(data.get('detail') or '')[:260]
       job['updated_utc'] = utcnow()
  elif kind == 'reminder':
   should_notify = (bool(b['alerts'].get(name, False)) and bool(data.get('currency'))
                    and data.get('expiry_confirmed') is True)
  elif kind in ('task', 'mail', 'notification'):
   if kind == 'notification' and data.get('event_rule') is True:
    b['alerts'].setdefault(name, True)
   # OCR changes and routine tasks are retained privately. Explicit urgent
   # notifications must be structured before they can be announced.
   should_notify = bool(b['alerts'].get(name, False)) and data.get('action_required') is True
   if should_notify:
    notice = {**data, 'notice_type': 'urgent_text'}

  if should_notify:
   for cid in dict.fromkeys(self.bot.settings.feature_channels(guild_id, 'ron')):
    item_id = ident + ':' + str(cid)
    b['deliveries'].setdefault(item_id, {
     'id': ident, 'channel_id': int(cid), 'title': name,
     'data': notice, 'state': 'queued'})
  b['updated_ids'][ident] = utcnow()
  if len(b['updated_ids']) > 2500:
   for k in list(b['updated_ids'])[:-1800]:
    b['updated_ids'].pop(k, None)
  if len(b['events']) > 350:
   for k in list(b['events'])[:-250]:
    b['events'].pop(k, None)
  b['jobs'] = b['jobs'][-250:]
  if len(b['deliveries']) > 2400:
   for k in list(b['deliveries']):
    if len(b['deliveries']) <= 1800:
     break
    if b['deliveries'][k].get('state') == 'delivered':
     b['deliveries'].pop(k, None)
  b['last_seen'] = utcnow()
  return ident

 async def sync(self, request):
  self._auth(request)
  try:
   payload = await request.json()
  except (ValueError, TypeError):
   raise web.HTTPBadRequest()
  updates = payload.get('updates') if isinstance(payload, dict) else None
  if not isinstance(updates, list) or len(updates) > 12:
   raise web.HTTPBadRequest(text='Invalid updates')
  target_guild = self._requested_guild(request, payload)
  ack = []
  async with self._lock:
   guild_ids = self._guild_ids()
   states = {}
   changed = set()
   for item in updates:
    if not isinstance(item, dict):
     continue
    item_target = self._requested_guild(request, item) if 'guild_id' in item else target_guild
    # Status receipts from legacy Pi clients are routed by job ID when possible.
    if item_target is not None:
     targets = [item_target]
    elif item.get('kind') == 'status':
     job_id = item.get('data', {}).get('job_id') if isinstance(item.get('data'), dict) else None
     targets = [gid for gid in guild_ids if any(
      isinstance(j, dict) and j.get('id') == job_id
      for j in states.setdefault(gid, self._read(gid))['jobs'])]
     if not targets:
      targets = [self.guild_id]
    else:
     targets = guild_ids
    accepted = True
    for gid in targets:
     b = states.setdefault(gid, self._read(gid))
     result = self._apply_update(b, item, gid)
     if result is None:
      accepted = False
     else:
      changed.add(gid)
    if accepted and targets:
     ack.append(item['id'])
   # All guild state (including queued deliveries) is durable before ack.
   for gid in changed:
    self._save(states[gid], gid)
  await self._deliver_pending()
  return web.json_response({'ack': ack})

 async def _deliver_pending(self, limit=12):
  """Resume unsent notifications across guilds; do not replay uncertain sends."""
  for _ in range(limit):
   async with self._lock:
    selected = None
    for gid in self._guild_ids():
     b = self._read(gid)
     key = next((k for k, v in b['deliveries'].items()
                 if isinstance(v, dict) and v.get('state') == 'queued'), None)
     if key is not None:
      item = b['deliveries'][key]
      item['state'] = 'sending'
      item['attempts'] = int(item.get('attempts', 0)) + 1
      self._save(b, gid)
      selected = (gid, key, item)
      break
    if selected is None:
     return
   gid, key, item = selected
   # Old queued calendar dumps and stale alerts are deliberately suppressed.
   if alert_text(item['title'], item['data']) is None:
    async with self._lock:
     b = self._read(gid)
     record = b['deliveries'].get(key)
     if record and record.get('state') == 'sending':
      record['state'] = 'suppressed'
      record['updated_utc'] = utcnow()
      self._save(b, gid)
    continue
   ok = await self._send_notification(item['title'], item['data'], item['channel_id'], gid)
   async with self._lock:
    b = self._read(gid)
    target = b['deliveries'].get(key)
    if target and target.get('state') == 'sending':
     target['state'] = 'delivered' if ok else 'failed'
     target['updated_utc'] = utcnow()
     self._save(b, gid)

 async def _send_notification(self, title, data, cid, guild_id=None):
  message = alert_text(title, data)
  if message is None:
   return True  # Legacy compatibility; suppressed notifications are not errors.
  try:
   channel = self.bot.get_channel(int(cid)) or await self.bot.fetch_channel(int(cid))
   expected_guild = int(guild_id or self.guild_id)
   if (not isinstance(channel, (discord.TextChannel, discord.Thread))
       or not channel.guild or channel.guild.id != expected_guild
       or cid not in self.bot.settings.feature_channels(expected_guild, 'ron')):
    return False
   member = channel.guild.me
   if member is not None and not channel.permissions_for(member).send_messages:
    return False
   await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
   return True
  except (discord.Forbidden, discord.HTTPException, ValueError, TypeError) as exc:
   err(f'Ron notification could not post in council-configured channel: {type(exc).__name__}')
   return False

 async def _announce(self, title, data, guild_id=None):
  """Compatibility entrypoint, now using only council channel assignments."""
  gid = int(guild_id or self.guild_id)
  results = []
  for cid in dict.fromkeys(self.bot.settings.feature_channels(gid, 'ron')):
   results.append(await self._send_notification(title, data, cid, gid))
  return all(results) if results else False

 async def _research_guide(self, title, target_guild=None):
  """Cache one sourced guide per event name, reusable by Pi and /ron info.

  Multi-guild legacy Pi requests fill all configured guild caches. Never
  invent a live alliance schedule from generic public documentation.
  """
  if not safe_title(title):
   raise web.HTTPBadRequest(text='Invalid event name')
  guilds = [target_guild] if target_guild is not None else self._guild_ids()
  async with self._research_lock:
   async with self._lock:
    cached = next((self._read(gid)['research'].get(title) for gid in guilds
                   if self._read(gid)['research'].get(title)), None)
    if cached:
     # Older installations cached only the primary guild. Safely share the
     # same public event guide with other configured HotBot guilds.
     for gid in guilds:
      b=self._read(gid)
      if title not in b['research']:
       b['research'][title]=cached
       self._save(b,gid)
     return cached, True
   key = os.getenv('OPENAI_API_KEY', '').strip()
   if not key:
    raise web.HTTPServiceUnavailable(text='Event research unavailable: OPENAI_API_KEY not configured on Railway')
   try:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, timeout=36)
    response = await client.responses.create(
     model=os.getenv('RON_RESEARCH_MODEL', 'gpt-4.1-mini'),
     tools=[{'type': 'web_search'}], store=False,
     input=('Research the Whiteout Survival event ' + title + '. '
            'Write a concise, practical guide for alliance members and leaders: '
            'what it is, what players actually need to do, relevant sign-up and preparation, '
            'event phases, and any currency or shop that needs spending before expiry. '
            'Use official Century Games support and reputable WoS community guides. '
            'Give source URLs. Distinguish confirmed information from uncertainty. '
            'Never invent current or upcoming in-game event dates, times, or a shop expiry.'),
     max_output_tokens=650)
    result = str(response.output_text).strip()[:5000]
    if not result:
     raise ValueError('Empty event research response')
   except Exception as exc:
    err('Ron event research unavailable: ' + type(exc).__name__)
    raise web.HTTPServiceUnavailable(text='Event research temporarily unavailable') from exc
   async with self._lock:
    for gid in guilds:
     b=self._read(gid)
     b['research'][title]=result
     self._save(b,gid)
   return result, False

 async def research(self, request):
  """Authenticated Pi route; never returns invented game-state dates."""
  self._auth(request)
  target_guild = self._requested_guild(request)
  try:
   data = await request.json()
  except (ValueError, TypeError):
   raise web.HTTPBadRequest()
  title = data.get('title', '') if isinstance(data, dict) else ''
  result, cached = await self._research_guide(title, target_guild)
  return web.json_response({'result': result, 'cached': cached})

 def _write_plan(self, b, code, start_day, plan_day, instructions, author_id, guild_id=None):
  start_day, day, content = validate_alliance_plan(code,start_day,plan_day,instructions)
  entry = b['plans'].get(code)
  if not isinstance(entry,dict) or entry.get('start_date') != start_day:
   entry={'start_date':start_day,'days':{}}
  entry.setdefault('days',{})[day]=content
  entry['updated_by']=str(author_id)
  entry['updated_utc']=utcnow()
  b['plans'][code]=entry
  # A late same-day correction must reach members; preloaded plans are delivered
  # by the Pi near day rollover. Persist queued Discord sends before response.
  today=datetime.now(timezone.utc)
  target=datetime.combine(datetime.strptime(start_day,'%Y-%m-%d').date()
      + timedelta(days=plan_day-1),datetime.min.time(),timezone.utc)
  if target+timedelta(hours=1) <= today < target+timedelta(days=1):
   notice_id='planmanual_'+hashlib.sha256((code+'|'+start_day+'|'+day+'|'+content).encode()).hexdigest()[:32]
   title=PLAN_EVENTS[code]
   payload={'title':title,'summary':f'{title} day {day} — updated alliance instructions: {content}',
            'notice_type':'urgent_text','action_required':True,'source':'approved_discord_plan'}
   for cid in dict.fromkeys(self.bot.settings.feature_channels(int(guild_id), 'ron')) if guild_id else ():
    b['deliveries'].setdefault(notice_id+':'+str(cid),{
     'id':notice_id,'channel_id':int(cid),'title':title,'data':payload,'state':'queued'})
  return PLAN_EVENTS[code],day

 def _plan_display(self,b):
  lines=[]
  for code,title in PLAN_EVENTS.items():
   row=b['plans'].get(code, {})
   if not isinstance(row,dict) or not row.get('days'):
    lines.append(f'{title}: no approved plan')
    continue
   lines.append(f'**{title}**, starts {row.get("start_date", "?")} UTC')
   for number, content in sorted(row.get('days',{}).items()):
    lines.append(f'Day {number}: {discord.utils.escape_mentions(str(content))[:240]}')
  return '\n'.join(lines)[:1900]

 @ron.command(name='plan_set',description='Preload an approved BiA or SvS alliance plan for a UTC date')
 @app_commands.choices(event=[app_commands.Choice(name='Brothers in Arms',value='bia'),
                        app_commands.Choice(name='SvS Preparation',value='svs')])
 async def plan_set_cmd(self,interaction:discord.Interaction,event:app_commands.Choice[str],
                        start_date:str,day:int,instructions:str):
  if not await self._manager(interaction):return
  try:
   validate_alliance_plan(event.value,start_date,day,instructions)
  except ValueError as exc:
   await interaction.response.send_message(str(exc),ephemeral=True)
   return
  async with self._lock:
   b=self._read(interaction.guild_id)
   title,day_num=self._write_plan(b,event.value,start_date,day,instructions,interaction.user.id,interaction.guild_id)
   self._signal(b,interaction.guild_id)
  await self._deliver_pending()
  await interaction.response.send_message(
   f'Saved **{title} day {day_num}** starting {start_date} UTC; Pi will receive the approved plan.',ephemeral=True)

 @ron.command(name='plan',description='View approved alliance BiA and SvS day-by-day plans')
 async def plan_cmd(self,interaction:discord.Interaction):
  if not await self._manager(interaction):return
  await interaction.response.send_message(self._plan_display(self._read(interaction.guild_id)),ephemeral=True)

 @ron.command(name='plan_editor',description='Allow or revoke a Discord member sending private Ron plans')
 async def plan_editor_cmd(self,interaction:discord.Interaction,user:discord.User,allow:bool):
  if not await self._manager(interaction):return
  if user.bot:
   await interaction.response.send_message('Bot accounts cannot be approved plan editors.',ephemeral=True)
   return
  async with self._lock:
   b=self._read(interaction.guild_id)
   editors=set(int(n) for n in b['plan_editors'] if str(n).isdigit())
   if allow:editors.add(user.id)
   else:editors.discard(user.id)
   b['plan_editors']=sorted(editors)
   self._signal(b,interaction.guild_id)
  await interaction.response.send_message(f'Private plan editing {"enabled" if allow else "revoked"} for {user.mention}.',ephemeral=True)

 @ron.command(name='plan_pending',description='View unverified alliance message/notice plan suggestions')
 async def plan_pending_cmd(self,interaction:discord.Interaction):
  if not await self._manager(interaction):return
  proposals=list(self._read(interaction.guild_id)['proposals'].items())[-6:]
  content='\n'.join(f'`{ident}` {item.get("event")}: {discord.utils.escape_mentions(item.get("text", "")[:170])}'
                     for ident,item in proposals)
  await interaction.response.send_message(content[:1900] or 'No pending OCR plan suggestions.',ephemeral=True)

 @ron.command(name='plan_approve',description='Approve an OCR suggestion as an alliance plan day')
 async def plan_approve_cmd(self,interaction:discord.Interaction,proposal_id:str,start_date:str,day:int):
  if not await self._manager(interaction):return
  async with self._lock:
   b=self._read(interaction.guild_id)
   proposal=b['proposals'].get(proposal_id)
   if not proposal:
    message='Unknown proposal ID.'
   else:
    try:
     title,daynum=self._write_plan(b,proposal['event'],start_date,day,proposal['text'],interaction.user.id,interaction.guild_id)
    except ValueError as exc:
     message=str(exc)
    else:
     del b['proposals'][proposal_id]
     self._signal(b,interaction.guild_id)
     message=f'Approved {title} day {daynum} from an OCR suggestion.'
  await self._deliver_pending()
  await interaction.response.send_message(message,ephemeral=True)

 @commands.Cog.listener('on_message')
 async def ron_private_plan(self,message:discord.Message):
  """Authorised private message format: ron plan bia | YYYY-MM-DD | 1 | Instructions."""
  if not isinstance(message.channel,discord.DMChannel) or message.author.bot:return
  raw=(message.content or '').strip()
  if not raw.lower().startswith('ron plan '):return
  parts=[chunk.strip() for chunk in raw[9:].split('|',3)]
  if len(parts)!=4:
   await message.channel.send('Format: ron plan bia | YYYY-MM-DD | 1 | Instructions (or svs).')
   return
  event,date_text,day_str,instructions=parts
  try:
   parsed_day=int(day_str)
   validate_alliance_plan(event.lower(),date_text,parsed_day,instructions)
  except ValueError as exc:
   await message.channel.send(str(exc))
   return
  eligible=[]
  for gid in self._guild_ids():
   if not self.bot.settings.feature_channels(gid,'ron'):continue
   b=self._read(gid)
   approved=message.author.id in b['plan_editors']
   if not approved:
    guild=self.bot.get_guild(gid)
    member=guild.get_member(message.author.id) if guild else None
    if member is None and guild:
     try:member=await guild.fetch_member(message.author.id)
     except (discord.Forbidden,discord.NotFound,discord.HTTPException):pass
    approved=bool(member and (member.guild_permissions.manage_guild or member.guild_permissions.administrator))
   if approved:eligible.append(gid)
  if len(eligible)!=1:
   await message.channel.send('Not authorised, or more than one Ron server matches. Use /ron plan_set in the intended server.')
   return
  gid=eligible[0]
  async with self._lock:
   b=self._read(gid)
   title,number=self._write_plan(b,event.lower(),date_text,parsed_day,instructions,message.author.id,gid)
   self._signal(b,gid)
  await self._deliver_pending()
  await message.channel.send(f'Saved {title} day {number} starting {date_text} UTC.')

 @ron.command(name='status', description='View Pi health and recent Ron check outcomes')
 async def status_cmd(self, interaction: discord.Interaction):
  if not await self._manager(interaction):
   return
  gid = interaction.guild_id
  b = self._read(gid)
  jobs = [j for j in b['jobs'] if isinstance(j, dict) and j.get('kind') in JOBS]
  pending = sum(j.get('state') in ('queued', 'running', 'processing') for j in jobs)
  delivery_errors = sum(x.get('state') in ('failed', 'uncertain')
                        for x in b['deliveries'].values() if isinstance(x, dict))
  lines = [f"Pi last contact: {b['last_seen'] or 'Never'}",
           f"Active/pending checks: {pending}",
           f"Saved calendar records (including history): {len(b['events'])}"]
  if jobs:
   lines.append('Recent checks:')
   for job in jobs[-5:][::-1]:
    kind = str(job.get('kind', '?')).replace('_', ' ').title()
    status = str(job.get('state', '?'))
    detail = discord.utils.escape_mentions(str(job.get('detail') or '')[:180])
    lines.append(f'- {kind}: **{status}**' + (f' — {detail}' if detail else ''))
  lines.append(f'Failed/uncertain notification deliveries: {delivery_errors}')
  await interaction.response.send_message('\n'.join(lines)[:1900], ephemeral=True)

 @ron.command(name='events', description='Show verified WoS events; request fresh scan if needed')
 async def events_cmd(self, interaction: discord.Interaction):
  if not await self._manager(interaction):
   return
  b = self._read(interaction.guild_id)
  now = datetime.now(timezone.utc)
  # One accurate occurrence per name is more helpful than a week of duplicate
  # OCR bars. Old calendar guesses remain in storage but never become a date.
  by_name = {}
  calendar_cycles = set()
  unverified = 0
  for event in b['events'].values():
   if not isinstance(event, dict) or not safe_title(event.get('title')):
    continue
   start = parsed_utc(event.get('start_utc'))
   end = parsed_utc(event.get('end_utc'))
   seen = parsed_utc(event.get('observed_utc'))
   if (event.get('time_evidence') != 'detail_popup'
       or event.get('status') != 'times_from_popup'
       or event.get('duration_source') == 'rule'
       or start is None or end is None or start >= end or seen is None):
    unverified += 1
    continue
   if end <= now or seen > now + timedelta(minutes=5):
    continue
   # Do not treat a historical import as a current game-state verification.
   # Active events require a recent observation; future starts stay eligible
   # for a week so the schedule does not disappear between daily checks.
   age = now - seen
   if age > (timedelta(hours=72) if start <= now else timedelta(days=7)):
    unverified += 1
    continue
   name = str(event['title']).strip()
   # Calendar cycles must never be presented as verified battle starts.
   # Legitimate multi-day event dates are useful as an approximate calendar
   # window, but extreme windows are likely OCR/title association errors.
   if is_battle_slot_event(name):
    calendar_cycles.add(name)
    continue
   if end-start > timedelta(days=14):
    unverified += 1
    continue
   key = name.casefold()
   is_active = start <= now
   # Active first; otherwise the next upcoming occurrence. Most recently
   # verified observation wins when two captures refer to the same event.
   priority = (0 if is_active else 1,
               -seen.timestamp() if is_active else start.timestamp(),
               -seen.timestamp())
   previous = by_name.get(key)
   if previous is None or priority < previous[0]:
    by_name[key] = (priority, name, start, end, is_active)
  rows = sorted(by_name.values(), key=lambda row: row[0])[:20]
  lines = []
  for _, name, start, end, active in rows:
   safe = discord.utils.escape_mentions(name)
   if end-start > timedelta(hours=4):
    # A multi-day calendar popup only establishes a displayed date RANGE,
    # not a precise activity start, deadline or battle hour.
    lines.append(f'**{safe}** — Calendar window: '
                 f'<t:{int(start.timestamp())}:D>–<t:{int(end.timestamp())}:D>; '
                 'exact event timing not confirmed')
   elif active:
    lines.append(f'**{safe}** — ACTIVE; verified end <t:{int(end.timestamp())}:F>')
   else:
    lines.append(f'**{safe}** — verified start <t:{int(start.timestamp())}:F>')
  if calendar_cycles:
   names = ', '.join(discord.utils.escape_mentions(n) for n in sorted(calendar_cycles))
   lines.append('Battle/registration cycles seen in Calendar: ' + names
                + '. Actual times must come from Task List or alliance schedules.')
  if not lines:
   # Avoid a rapid and endless check-loop when calibration or OCR is failing.
   async with self._lock:
    latest = self._read(interaction.guild_id)
    calendar_jobs = [j for j in latest['jobs'] if isinstance(j, dict)
                     and j.get('kind') == 'calendar']
    last = calendar_jobs[-1] if calendar_jobs else None
    waiting = bool(last and last.get('state') in ('queued', 'running', 'processing'))
    stamped = parsed_utc(last.get('created_utc')) if last else None
    recent = bool(stamped and now - stamped < timedelta(minutes=30))
    if waiting:
     message = 'Calendar check is still queued or processing. /ron status shows progress.'
    elif recent:
     reason = discord.utils.escape_mentions(str(last.get('detail') or 'No verified game times were produced')[:260])
     message = ('No verified current event times. Last calendar check: '
                f"{last.get('state','unknown')} — {reason}. "
                'Use /ron check Calendar to retry when the tablet is ready.')
    else:
     latest['jobs'].append({'id':'check_'+secrets.token_hex(12),
                            'kind':'calendar', 'state':'queued', 'created_utc':utcnow()})
     self._signal(latest, interaction.guild_id)
     message = ('No verified current event times. Requested one calendar check. '
                'Use /ron status to see its result or failure reason.')
  else:
   message = '\n'.join(lines)
  await interaction.response.send_message(message[:1900], ephemeral=True)

 @ron.command(name='alerts', description='Show available event notifications')
 async def alerts_cmd(self, interaction: discord.Interaction):
  if not await self._manager(interaction):
   return
  b = self._read(interaction.guild_id)
  lines = [f"{'ON' if val else 'OFF'} — {key}" for key, val in sorted(b['alerts'].items())]
  await interaction.response.send_message(('\n'.join(lines) or 'No alert types yet.')[:1900], ephemeral=True)

 @ron.command(name='alert', description='Enable or disable notifications for any detected event')
 @app_commands.describe(event='Exact event name from /ron alerts', enabled='True sends announcements when this event changes')
 async def alert_cmd(self, interaction: discord.Interaction, event: str, enabled: bool):
  if not await self._manager(interaction):
   return
  if not safe_title(event):
   await interaction.response.send_message('Invalid event name.', ephemeral=True)
   return
  async with self._lock:
   b = self._read(interaction.guild_id)
   b['alerts'][event.strip()] = enabled
   self._signal(b, interaction.guild_id)
  await interaction.response.send_message(
   f"Notifications for **{discord.utils.escape_mentions(event)}**: {'ON' if enabled else 'OFF'}", ephemeral=True)

 @ron.command(name='rule', description='Set event duration/category without touching Pi scripts')
 @app_commands.choices(category=[app_commands.Choice(name=n.title(), value=n)
                                 for n in ('event', 'shop', 'alliance', 'phased')])
 async def rule_cmd(self, interaction: discord.Interaction, event: str,
                    category: app_commands.Choice[str], days: app_commands.Range[int, 1, 30],
                    currency: str=''):
  if not await self._manager(interaction):
   return
  if not safe_title(event) or len(currency) > 80:
   await interaction.response.send_message('Invalid event name or currency.', ephemeral=True)
   return
  rule = {'kind': category.value, 'days': days}
  if currency.strip() and category.value == 'shop':
   rule['spend_currency'] = currency.strip()
   rule['reminder_hours_before_end'] = 1
  async with self._lock:
   b = self._read(interaction.guild_id)
   b['rules'][event.strip()] = rule
   self._signal(b, interaction.guild_id)
  await interaction.response.send_message(
   f'Event rule updated for {discord.utils.escape_mentions(event)}. The Pi will receive it on its next connection.',
   ephemeral=True)

 @ron.command(name='info', description='Read or research a sourced WoS event guide')
 async def info_cmd(self, interaction: discord.Interaction, event: str):
  if not await self._manager(interaction):
   return
  if not safe_title(event):
   await interaction.response.send_message('Invalid event name.', ephemeral=True)
   return
  b = self._read(interaction.guild_id)
  result = b['research'].get(event)
  if result:
   await interaction.response.send_message(
    discord.utils.escape_mentions(str(result))[:1900], ephemeral=True)
   return
  # Web research may exceed Discord's initial response deadline.
  await interaction.response.defer(thinking=True, ephemeral=True)
  try:
   result, _ = await self._research_guide(event, interaction.guild_id)
   reply = discord.utils.escape_mentions(str(result))[:1900]
  except web.HTTPServiceUnavailable:
   reply = 'Research is unavailable. Check the OpenAI API configuration on Railway.'
  await interaction.followup.send(reply, ephemeral=True)

 @ron.command(name='interval', description='Set the interval for chat or Alliance Help checks')
 @app_commands.choices(kind=[app_commands.Choice(name='Chat', value='chat'),
                             app_commands.Choice(name='Alliance Help', value='help')])
 async def interval_cmd(self, interaction: discord.Interaction,
                        kind: app_commands.Choice[str], seconds: app_commands.Range[int, 5, 3600]):
  if not await self._manager(interaction):
   return
  async with self._lock:
   b = self._read(interaction.guild_id)
   b['schedules'][kind.value]['interval_seconds'] = seconds
   self._signal(b, interaction.guild_id)
  await interaction.response.send_message(
   f'{kind.value} interval set to {seconds} seconds UTC-independent; Pi will sync.', ephemeral=True)

 @ron.command(name='channel', description='Compatibility: add a Ron channel through council settings')
 async def channel_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel):
  if not await self._manager(interaction):
   return
  if channel.guild.id != interaction.guild_id:
   await interaction.response.send_message('Channel must belong to this server.', ephemeral=True)
   return
  me = interaction.guild.me if interaction.guild else None
  if me is not None and not channel.permissions_for(me).send_messages:
   await interaction.response.send_message('HotBot cannot send messages in that channel.', ephemeral=True)
   return
  async with self._lock:
   self.bot.settings.add_feature_channel(interaction.guild_id, 'ron', channel.id)
  await interaction.response.send_message(
   f'Ron feature **ron** added to {channel.mention}; council feature settings updated.',
   ephemeral=True)

 @ron.command(name='retry_failed', description='Retry failed Ron notifications after resolving channel permissions')
 async def retry_failed_cmd(self, interaction: discord.Interaction):
  if not await self._manager(interaction):
   return
  gid = interaction.guild_id
  async with self._lock:
   b = self._read(gid)
   count = 0
   for record in b['deliveries'].values():
    if isinstance(record, dict) and record.get('state') == 'failed':
     record['state'] = 'queued'
     count += 1
   self._save(b, gid)
  await self._deliver_pending()
  await interaction.response.send_message(
   f'Retried {count} failed notifications. Uncertain deliveries require manual review to avoid duplicates.',
   ephemeral=True)

 @ron.command(name='check', description='Queue one check for the Pi, without opening the game on Railway')
 @app_commands.choices(kind=[app_commands.Choice(name=k.replace('_', ' ').title(), value=k)
                             for k in sorted(JOBS)])
 async def check_cmd(self, interaction: discord.Interaction, kind: app_commands.Choice[str]):
  if not await self._manager(interaction):
   return
  gid = interaction.guild_id
  async with self._lock:
   b = self._read(gid)
   active = next((j for j in b['jobs'] if isinstance(j, dict) and
                  j.get('kind') == kind.value and j.get('state') in ('queued','running','processing')), None)
   if active:
    result = 'Already queued'
   else:
    b['jobs'].append({'id': 'check_' + secrets.token_hex(12), 'kind': kind.value,
                      'state': 'queued', 'created_utc': utcnow()})
    self._signal(b, gid)
    result = 'Queued'
  await interaction.response.send_message(
   f'{result}: {kind.value}. Check /ron status for progress.', ephemeral=True)

 @ron.command(name='schedule', description='Set the daily UTC check time and enable/disable a job')
 @app_commands.choices(kind=[app_commands.Choice(name=k.replace('_', ' ').title(), value=k)
                             for k in ('calendar', 'mail', 'task_list', 'chat', 'help', 'notice', 'personal')])
 async def schedule_cmd(self, interaction: discord.Interaction,
                        kind: app_commands.Choice[str], enabled: bool,
                        utc_hour: app_commands.Range[int, 0, 23]=0,
                        utc_minute: app_commands.Range[int, 0, 59]=0):
  if not await self._manager(interaction):
   return
  gid = interaction.guild_id
  async with self._lock:
   b = self._read(gid)
   old = b['schedules'].get(kind.value, {})
   if old.get('cadence') == 'daily':
    old.update(enabled=enabled, utc_hour=utc_hour, utc_minute=utc_minute)
   elif old.get('cadence') == 'hourly':
    old.update(enabled=enabled, utc_minute=utc_minute)
   else:
    old['enabled'] = enabled
   b['schedules'][kind.value] = old
   self._signal(b, gid)
  await interaction.response.send_message(
   f'{kind.value} schedule: {old}. Saved on HotBot; Pi will sync.', ephemeral=True)


async def setup(bot: commands.Bot):
 await bot.add_cog(RonBurgundyCog(bot))
