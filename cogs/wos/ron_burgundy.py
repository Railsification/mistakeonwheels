# cogs/wos/ron_burgundy.py
from __future__ import annotations
__version__ = '1.5.2'
"""HotBot-side Ron control; the Raspberry Pi initiates every connection.

Ron is configured independently in every HotBot guild. Existing Pi clients
that omit guild_id continue to work; their observations fan out to configured
guilds, each with independent alerts, jobs and council feature channels.

v1.5.0: no guild is "primary" for Ron any more. WoS Alliance Chat readings and
Pi capabilities fan out to every configured guild, /ron chat_send and
/ron chat_recent work in every configured guild, and one shared tablet job queue
is de-duplicated across guilds. Adds simple `!ron ...` prefix commands for
everyday use and Help All controls (Discord and an officer-armed WoS chat form).
v1.5.2: Help All from WoS Alliance Chat is OPEN by default (any R4/R5 can type
`!ron helpall on|off` any time). OCR cannot prove a sender's rank, so `!ron helpall lock`
restricts it to Discord officers and `unlock` reopens it. `arm [min]` still opens a locked window.
v1.5.1: `!ron dashboard` and `/ron dashboard` privately give officers the Pi dashboard link
(kept in memory only, never saved to disk).
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
JOBS = {'calendar', 'mail', 'task_list', 'chat', 'help', 'notice', 'personal', 'chat_send'}
# Jobs that carry their own payload and are never offered as a plain /ron check.
CONTROL_JOBS = {'help_control'}
ALL_JOBS = JOBS | CONTROL_JOBS
HELPALL_ARM_MAX_MINUTES = 24 * 60
HELPALL_ARM_DEFAULT_MINUTES = 30
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
         'event_baseline_at': None, 'plans': {}, 'plan_editors': [], 'proposals': {},
         'chat_messages': {}, 'chat_command_seen': {}, 'bridge_capabilities': {},
         'helpall': {'chat_locked': False, 'armed_until': None, 'armed_by': '', 'desired': None,
                     'updated_utc': None, 'updated_by': ''}}


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
 if data.get('notice_type') == 'chat_mirror':
  value=str(data.get('text') or '').strip()
  if not value:return None
  return '**WoS Alliance Chat (OCR)**\n'+discord.utils.escape_mentions(value[:350])
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


CHAT_COMMAND = re.compile(r'^!ron\s+(help|status|events|bear|fortress|calendar|mail|task)\s*[.!]?$',re.I)
# Help All control from WoS chat. Anyone can read the state; changing it works only
# while a Discord officer has armed it (see helpall_armed()). OCR never proves WHO
# typed a line, so the authority comes from the Discord officer, not the chat name.
HELPALL_COMMAND = re.compile(r'^!ron\s+help\s*all\s+(on|off|status)\s*[.!]?$',re.I)
CHAT_TEXT = re.compile(r'^[A-Za-z0-9 .,!?@#:/()_+=-]{1,180}$')


def chat_reply(verb, b):
 """Only public, read-only responses; no player authority inferred from OCR."""
 if verb == 'help':
  return 'Ron commands: !ron help, status, events, bear, fortress, helpall status. Officers control scans in Discord.'
 if verb == 'status':
  stamp=parsed_utc(b.get('last_seen'))
  return 'Ron connected to Discord.' if stamp and datetime.now(timezone.utc)-stamp < timedelta(minutes=5) else 'Ron is reconnecting.'
 if verb == 'events':
  count=sum(bool(isinstance(e,dict) and e.get('time_evidence')=='detail_popup' and event_is_current(e))
            for e in b.get('events',{}).values())
  return (f'Ron has {count} recently confirmed event window(s); Discord /ron events has details.'
          if count else 'No verified event times right now; ask an officer to check Discord.')
 if verb in ('bear','fortress'):
  return 'Please check your alliance schedule or ask an R4 for confirmed times.'
 return 'For security, only Discord officers can request tablet scans.'


def helpall_armed(b, now=None):
    """True while a Discord officer has opened a temporary Help All chat window."""
    row = b.get('helpall') if isinstance(b, dict) else None
    until = parsed_utc(row.get('armed_until')) if isinstance(row, dict) else None
    return bool(until and until > (now or datetime.now(timezone.utc)))


def helpall_chat_allowed(b, now=None):
    """WoS chat may change Help All unless an officer locked it (and no window is open)."""
    row = b.get('helpall') if isinstance(b, dict) else None
    locked = bool(row.get('chat_locked')) if isinstance(row, dict) else False
    return (not locked) or helpall_armed(b, now)


_ASCII_FIXES = str.maketrans({
 '\u2018': None, '\u2019': None, '\u201c': None, '\u201d': None, '`': None,
 '\u2013': '-', '\u2014': '-', '\u2026': '...', '\u00a0': ' ',
 '\n': ' ', '\r': ' ', '\t': ' ',
})
CHAT_TEXT_HELP = ('Use 1-180 characters: letters, numbers, spaces and . , ! ? @ # : / ( ) _ + = - only '
                  '(apostrophes and quotes are removed automatically).')


def wos_chat_text(message):
    """Normalise a Discord message into what the tablet keyboard can type, or None."""
    if not isinstance(message, str):
        return None
    cleaned = ' '.join(message.translate(_ASCII_FIXES).split())
    return cleaned if CHAT_TEXT.fullmatch(cleaned) else None


def expire_stale_jobs(b, now=None):
    """Mark queued/running tablet-message jobs past their expiry as failed.

    Without this a message the Pi never collected stayed 'queued' for ever and
    blocked every later /ron chat_send ("already queued").  Returns True if changed.
    """
    now = now or datetime.now(timezone.utc)
    changed = False
    for job in b.get('jobs', []):
        if (isinstance(job, dict) and job.get('kind') in ('chat_send', 'help_control')
                and job.get('state') in ('queued', 'running')):
            expiry = parsed_utc(job.get('expires_utc'))
            if expiry is not None and expiry <= now:
                job['state'] = 'failed'
                job['detail'] = 'Expired before the Pi completed it'
                job['updated_utc'] = utcnow()
                changed = True
    return changed


def routine_chat_reminder(item, now=None):
 """Provide one safe, expiring Alliance Chat copy of the daily Stamina alert.

 No arbitrary Pi/OCR text is ever echoed to game chat. Discord's independently
 configured Ron feature channels continue to receive their normal alert.
 """
 if not isinstance(item, dict) or item.get('kind') != 'notification':
  return None
 data=item.get('data')
 if not isinstance(data, dict) or any((
     data.get('title') != 'Stamina',
     data.get('purpose') != 'daily_stamina',
     data.get('rule_source') != 'daily routine',
     data.get('action_required') is not True,
 )):
  return None
 deadline=parsed_utc(data.get('deadline_utc'))
 now=now or datetime.now(timezone.utc)
 if deadline is None or now.tzinfo is None:
  return None
 if (deadline.hour,deadline.minute,deadline.second,deadline.microsecond) != (0,0,0,0):
  return None
 now=now.astimezone(timezone.utc)
 if not timedelta(0) < deadline-now <= timedelta(hours=1):
  return None
 # The alert belongs to the UTC day that ends at this reset.
 if data.get('event_date') != (deadline-timedelta(days=1)).date().isoformat():
  return None
 message='Stamina reset reminder: collect unclaimed Stamina and Intel rewards before 00:00 UTC.'
 return (message, deadline.isoformat()) if CHAT_TEXT.fullmatch(message) else None


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
  'how_to_play': 'Use /council feature_channel_add with feature ron; use /ron events, /ron alert, /ron check, and /ron schedule. Everyday shortcuts: !ron status, !ron say <message>, !ron helpall on|off.',
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
  self._dashboard_link = None  # (url, received_utc); memory only, it contains the Pi's key
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
    if expire_stale_jobs(b):
     self._save(b, gid)
    jobs = [
     {**{'id': j['id'], 'kind': j['kind'], 'guild_id': gid},
      **({'text': j['text'], 'expires_utc': j['expires_utc']}
         if j.get('kind')=='chat_send' else {}),
      **({'enabled': j['enabled'], 'expires_utc': j['expires_utc']}
         if j.get('kind')=='help_control' else {})}
    for j in b['jobs'] if isinstance(j, dict) and j.get('state') == 'queued'
     and isinstance(j.get('id'), str) and j.get('kind') in ALL_JOBS
     and (j.get('kind')!='chat_send' or
          (isinstance(j.get('text'),str) and isinstance(j.get('expires_utc'),str)))
     and (j.get('kind')!='help_control' or
          (type(j.get('enabled')) is bool and isinstance(j.get('expires_utc'),str)))]
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
  if kind not in ('event', 'task', 'mail', 'notification', 'reminder', 'status', 'research', 'plan_proposal', 'chat', 'capabilities') or not isinstance(data, dict):
   return None
  if ident in b['updated_ids']:
   return ident

  should_notify = False
  notice = data
  name = str(data.get('title') or kind)
  if kind == 'capabilities':
   capabilities={k:bool(data.get(k)) for k in
       ('chat_capture_enabled','chat_send_calibrated','monitor_enabled',
        'healer_enabled','healer_calibrated')}
   pressed=parsed_utc(data.get('healer_last_pressed_utc'))
   if pressed:capabilities['healer_last_pressed_utc']=pressed.isoformat()
   b['bridge_capabilities']=capabilities
  elif kind == 'chat':
   # The tablet's Alliance Chat is mirrored to every configured guild's own Ron
   # channels. Replies to in-game commands are decided once per reading, in
   # _chat_followups(), because there is only one tablet to type with.
   line=str(data.get('text') or '').strip()
   source=data.get('source')
   observed=parsed_utc(data.get('observed_utc'))
   if source!='alliance_chat' or not (4<=len(line)<=250 if line.startswith('!') else 9<=len(line)<=250) or observed is None:
    return None
   if abs((datetime.now(timezone.utc)-observed).total_seconds()) > 600:
    # Stale offline OCR must be acknowledged without ever being announced;
    # rejecting it would block the Pi's durable outbox forever.
    b['updated_ids'][ident]=utcnow()
    return ident
   b['chat_messages'][ident]={'text':line,'observed_utc':data['observed_utc']}
   if len(b['chat_messages'])>200:
    for old in list(b['chat_messages'])[:-150]:b['chat_messages'].pop(old,None)
   safe_line=discord.utils.escape_mentions(line.replace('`', chr(39)*1))
   for cid in dict.fromkeys(self.bot.settings.feature_channels(guild_id,'ron')):
    key=ident+':'+str(cid)
    b['deliveries'].setdefault(key,{'id':ident,'channel_id':int(cid),
     'title':'Alliance Chat','data':{'notice_type':'chat_mirror','text':safe_line},
     'state':'queued'})
  elif kind == 'event':
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
      if state in ('queued', 'running', 'processing', 'completed', 'failed', 'uncertain'):
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
    self._note_dashboard_link(item)
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
    was_new = item.get('kind') == 'chat' and any(
     item.get('id') not in states.setdefault(gid, self._read(gid))['updated_ids'] for gid in targets)
    for gid in targets:
     b = states.setdefault(gid, self._read(gid))
     result = self._apply_update(b, item, gid)
     if result is None:
      accepted = False
     else:
      changed.add(gid)
    if accepted and targets:
     ack.append(item['id'])
     if was_new:
      # One tablet, one reply: decided once across every configured guild.
      changed.update(self._chat_followups(item, states, guild_ids))
     # Send the same Stamina reminder to WoS Alliance Chat as well as every
     # Council-configured Ron Discord channel. There is one tablet, so the
     # in-game message is queued exactly once, in the first participating guild.
     reminder = routine_chat_reminder(item)
     participants = [gid for gid in targets
                     if states[gid]['alerts'].get('Stamina',False)
                     and self.bot.settings.feature_channels(gid,'ron')]
     if reminder is not None and participants:
      job_id='chat_stamina_'+hashlib.sha256(item['id'].encode('utf-8')).hexdigest()[:24]
      if not any(isinstance(j,dict) and j.get('id')==job_id
                 for gid in guild_ids for j in states.setdefault(gid, self._read(gid))['jobs']):
       home = states[participants[0]]
       message, expires = reminder
       home['jobs'].append({'id':job_id,'kind':'chat_send',
                           'text':message,'expires_utc':expires,
                           'created_utc':utcnow(),'state':'queued'})
       home['jobs']=home['jobs'][-250:]
       home['revision']+=1
       changed.add(participants[0])
       self._change.set()
       self._change=asyncio.Event()
   # All guild state (including queued deliveries) is durable before ack.
   for gid in changed:
    self._save(states[gid], gid)
  await self._deliver_pending()
  return web.json_response({'ack': ack})


 # ---- shared tablet helpers (one Pi, one tablet, every configured guild) ----

 def _job_home(self, states, guild_ids):
  """Guild whose queue carries a tablet job: first with Ron channels, else first."""
  for gid in guild_ids:
   if self.bot.settings.feature_channels(gid, 'ron'):
    return gid
  return guild_ids[0] if guild_ids else self.guild_id

 def _active_tablet_job(self, kind, states=None):
  """Any queued/running job of this kind in ANY guild (single shared tablet)."""
  for gid in self._guild_ids():
   b = (states or {}).get(gid) or self._read(gid)
   if any(isinstance(j, dict) and j.get('kind') == kind and j.get('state') in ('queued', 'running')
          for j in b['jobs']):
    return True
  return False

 def _capabilities_for(self, guild_ids):
  """Newest-known Pi capabilities; every guild receives the same report."""
  merged = {}
  for gid in guild_ids:
   merged.update({k: v for k, v in (self._read(gid).get('bridge_capabilities') or {}).items()})
  return merged

 def _append_chat_job(self, b, text, minutes=5, ident=None):
  b['jobs'].append({'id': ident or 'chat_' + secrets.token_hex(12), 'kind': 'chat_send',
                    'text': text, 'state': 'queued', 'created_utc': utcnow(),
                    'expires_utc': (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()})
  b['jobs'] = b['jobs'][-250:]
  b['revision'] = int(b['revision']) + 1
  self._change.set()
  self._change = asyncio.Event()

 def _append_help_job(self, b, enabled, source):
  b['jobs'].append({'id': 'help_' + secrets.token_hex(12), 'kind': 'help_control',
                    'enabled': bool(enabled), 'source': source, 'state': 'queued',
                    'created_utc': utcnow(),
                    'expires_utc': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()})
  b['jobs'] = b['jobs'][-250:]
  state = b.setdefault('helpall', baseline()['helpall'])
  state['desired'] = bool(enabled)
  state['updated_utc'] = utcnow()
  state['updated_by'] = str(source)[:80]
  b['revision'] = int(b['revision']) + 1
  self._change.set()
  self._change = asyncio.Event()

 def _chat_followups(self, item, states, guild_ids):
  """One decision per WoS chat reading: read-only replies and Help All control.

  Returns the set of guild IDs whose state changed.  The reply job is queued once,
  in a single guild, because every guild shares the same Pi tablet.
  """
  changed = set()
  data = item.get('data') if isinstance(item.get('data'), dict) else {}
  line = str(data.get('text') or '').strip()
  observed = parsed_utc(data.get('observed_utc'))
  now = datetime.now(timezone.utc)
  if (data.get('source') != 'alliance_chat' or observed is None
      or abs((now - observed).total_seconds()) > 600 or not line.startswith('!')):
   return changed
  home_gid = self._job_home(states, guild_ids)
  home = states.setdefault(home_gid, self._read(home_gid))

  def seen_recently(key, minutes):
   stamps = [parsed_utc((states.setdefault(g, self._read(g)).get('chat_command_seen') or {}).get(key))
             for g in guild_ids]
   latest = max((x for x in stamps if x), default=None)
   return latest is not None and now - latest < timedelta(minutes=minutes)

  def remember(key):
   seen = home.setdefault('chat_command_seen', {})
   seen[key] = utcnow()
   if len(seen) > 30:
    for old in list(seen)[:-20]:
     seen.pop(old, None)
   changed.add(home_gid)

  def say(text):
   caps = self._capabilities_for(guild_ids)
   if caps and not caps.get('chat_send_calibrated'):
    return  # Never queue replies the tablet cannot type.
   if CHAT_TEXT.fullmatch(text):
    self._append_chat_job(home, text)
    changed.add(home_gid)

  found = CHAT_COMMAND.fullmatch(line)
  if found:
   verb = found.group(1).casefold()
   if not seen_recently(verb, 10):
    remember(verb)
    say(chat_reply(verb, home))
   return changed
  found = HELPALL_COMMAND.fullmatch(line)
  if not found:
   return changed
  action = found.group(1).casefold()
  caps = self._capabilities_for(guild_ids)
  if action == 'status':
   if not seen_recently('helpall_status', 2):
    remember('helpall_status')
    say('Help All is ' + ('ON' if caps.get('healer_enabled') else 'OFF')
        + '. Officers can change it from Discord.')
   return changed
  armed = all(helpall_chat_allowed(states.setdefault(g, self._read(g)), now) for g in guild_ids)
  if not armed:
   if not seen_recently('helpall_denied', 10):
    remember('helpall_denied')
    say('Help All chat control is locked by a Discord officer.')
   return changed
  key = 'helpall_' + action
  if seen_recently(key, 1) or self._active_tablet_job('help_control', states):
   return changed
  remember(key)
  self._append_help_job(home, action == 'on', 'wos_chat')
  say('Help All ' + action.upper() + ' requested.')
  return changed

 DASHBOARD_URL = re.compile(r'^http://(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}/\?token=[A-Za-z0-9_-]{10,80}$')

 def _note_dashboard_link(self, item):
  """Remember the Pi's LAN dashboard link in memory only (it contains the Pi's key)."""
  data = item.get('data') if isinstance(item, dict) else None
  if (isinstance(item, dict) and item.get('kind') == 'capabilities' and isinstance(data, dict)
      and isinstance(data.get('dashboard_url'), str) and self.DASHBOARD_URL.fullmatch(data['dashboard_url'])):
   self._dashboard_link = (data['dashboard_url'], utcnow())

 def _dashboard_message(self):
  if not self._dashboard_link:
   return ('The Pi has not reported its dashboard address yet. It does this every couple of minutes '
           'once Pi v2.9.1+ is running and connected; try again shortly.')
  url, seen = self._dashboard_link
  return (f'**Ron dashboard** (last confirmed {seen[:16].replace("T", " ")} UTC)\n{url}\n'
          'Open it on a device on the same home network as the Pi. The link contains the Pi key, '
          'so do not share it.')

 async def _queue_chat_message(self, guild_id, message):
  """Queue one Alliance Chat message for the shared tablet; returns user feedback."""
  text = wos_chat_text(message)
  if text is None:
   return CHAT_TEXT_HELP
  async with self._lock:
   b = self._read(guild_id)
   caps = self._capabilities_for(self._guild_ids())
   if caps and not caps.get('chat_send_calibrated'):
    return 'WoS chat Send is not calibrated on the Pi yet. No message queued.'
   for gid in self._guild_ids():
    other = self._read(gid)
    if expire_stale_jobs(other):
     self._save(other, gid)
    if gid == guild_id:
     b = other
   if self._active_tablet_job('chat_send'):
    return 'A WoS chat message is already waiting for the tablet. Try again in a minute, or check status.'
   self._append_chat_job(b, text)
   self._signal(b, guild_id)
  return 'Queued for WoS Alliance Chat: ' + discord.utils.escape_mentions(text) + '\nThis is NOT confirmation it was sent; check status.'

 async def _queue_check(self, guild_id, kind):
  async with self._lock:
   b = self._read(guild_id)
   active = next((j for j in b['jobs'] if isinstance(j, dict) and
                  j.get('kind') == kind and j.get('state') in ('queued', 'running', 'processing')), None)
   if active:
    return 'Already queued'
   b['jobs'].append({'id': 'check_' + secrets.token_hex(12), 'kind': kind,
                     'state': 'queued', 'created_utc': utcnow()})
   self._signal(b, guild_id)
  return 'Queued'

 async def _set_helpall(self, guild_id, enabled, author_id):
  async with self._lock:
   b = self._read(guild_id)
   for gid in self._guild_ids():
    other = self._read(gid)
    if expire_stale_jobs(other):
     self._save(other, gid)
    if gid == guild_id:
     b = other
   if self._active_tablet_job('help_control'):
    return 'A Help All change is already waiting for the Pi.'
   self._append_help_job(b, enabled, 'discord:' + str(author_id))
   self._signal(b, guild_id)
  return (f'Help All **{"ON" if enabled else "OFF"}** requested. The Pi applies it within a minute; '
          'use `!ron status` to confirm. Turning it ON works best while the Pi can reach the Help page '
          '(see status).')

 def _status_lines(self, guild_id):
  b = self._read(guild_id)
  jobs = [j for j in b['jobs'] if isinstance(j, dict) and j.get('kind') in ALL_JOBS]
  pending = sum(j.get('state') in ('queued', 'running', 'processing') for j in jobs)
  caps = self._capabilities_for(self._guild_ids())
  delivery_errors = sum(x.get('state') in ('failed', 'uncertain')
                        for x in b['deliveries'].values() if isinstance(x, dict))
  lines = [f"Pi last contact: {b['last_seen'] or 'Never'}",
           f"Active/pending checks: {pending}",
           f"Saved calendar records (including history): {len(b['events'])}"]
  if jobs:
   lines.append('Recent checks:')
   for job in jobs[-5:][::-1]:
    name = str(job.get('kind', '?')).replace('_', ' ').title()
    detail = discord.utils.escape_mentions(str(job.get('detail') or '')[:180])
    lines.append(f"- {name}: **{job.get('state', '?')}**" + (f' — {detail}' if detail else ''))
  chat_state = ('Ready for calibrated sends' if caps.get('chat_send_calibrated')
                else 'Needs chat input/Send calibration')
  lines.append(f'Chat reading: {"enabled" if caps.get("chat_capture_enabled") else "not yet confirmed"}; sending: {chat_state}')
  healer = 'ON' if caps.get('healer_enabled') else 'OFF'
  healer_ready = 'calibrated' if caps.get('healer_calibrated') else 'no Help icon calibration (Help page must be open or Alliance/Help buttons taught)'
  lines.append(f'Alliance Help All: {healer} ({healer_ready})')
  if caps.get('healer_last_pressed_utc'):
   lines.append(f'Last Help All tap: {caps["healer_last_pressed_utc"]}')
  lines.append('WoS chat Help All control: ' + ('open to anyone typing in Alliance Chat'
               if helpall_chat_allowed(b) else 'LOCKED (unlock with !ron helpall unlock)'))
  lines.append(f'Failed/uncertain notification deliveries: {delivery_errors}')
  return lines

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
  await interaction.response.send_message(
   '\n'.join(self._status_lines(interaction.guild_id))[:1900], ephemeral=True)

 @ron.command(name='dashboard', description='Privately get the link to the Ron Pi dashboard (setup, switches, calibration)')
 async def dashboard_cmd(self, interaction: discord.Interaction):
  if not await self._manager(interaction):
   return
  await interaction.response.send_message(self._dashboard_message(), ephemeral=True)

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

 @ron.command(name='chat_send',description='Send one approved message to WoS Alliance Chat')
 async def chat_send_cmd(self,interaction:discord.Interaction,message:str):
  if not await self._manager(interaction):return
  # Works in every configured HotBot server; the single tablet queue is shared.
  msg=await self._queue_chat_message(interaction.guild_id,message)
  await interaction.response.send_message(msg[:1900],ephemeral=True)

 @ron.command(name='diagnose',description='Queue Chat, Task List, Mail, Calendar and Help checks')
 async def diagnose_cmd(self,interaction:discord.Interaction):
  if not await self._manager(interaction):return
  queued=[];existing=[]
  async with self._lock:
   b=self._read(interaction.guild_id)
   for kind in ('chat','task_list','mail','calendar','help'):
    if any(j.get('kind')==kind and j.get('state') in ('queued','running','processing')
           for j in b['jobs'] if isinstance(j,dict)):
     existing.append(kind)
    else:
     b['jobs'].append({'id':'check_'+secrets.token_hex(12),'kind':kind,
                       'state':'queued','created_utc':utcnow()})
     queued.append(kind)
   if queued:self._signal(b,interaction.guild_id)
  await interaction.response.send_message(
    f"Queued together: {', '.join(queued) or 'none'}. "
    f"Already running: {', '.join(existing) or 'none'}. "
    'The Pi executes one tablet action at a time; /ron status will show results and failures.',ephemeral=True)

 @ron.command(name='chat_recent',description='Review recent OCR chat readings privately')
 async def chat_recent_cmd(self,interaction:discord.Interaction):
  if not await self._manager(interaction):return
  b=self._read(interaction.guild_id)
  rows=list(b['chat_messages'].values())[-12:]
  result='\n'.join(discord.utils.escape_mentions(str(x.get('text',''))[:220]) for x in rows)
  await interaction.response.send_message(result[:1900] or 'No recent recognised WoS chat yet.',ephemeral=True)

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


 # ---- simple Discord !commands (same permission as the /ron slash commands) ----

 PREFIX_CHECKS = {'calendar': 'calendar', 'mail': 'mail', 'task': 'task_list', 'tasks': 'task_list',
                  'task_list': 'task_list', 'chat': 'chat', 'help': 'help', 'notice': 'notice',
                  'personal': 'personal'}
 PREFIX_HELP = ('**Ron commands** (Manage Server permission, any configured HotBot server)\n'
                '`!ron dashboard` — private link to the Pi dashboard: switches, setup checklist, calibration\n'
                '`!ron status` — Pi health, recent checks, Help All state\n'
                '`!ron say <message>` — post a message in WoS Alliance Chat\n'
                '`!ron chat` — DM you the latest WoS chat readings\n'
                '`!ron check <calendar|mail|task|chat|help|notice>` — queue one scan\n'
                '`!ron diagnose` — queue Chat, Task List, Mail, Calendar and Help checks\n'
                '`!ron helpall on|off|status` — control Alliance Help All on the Pi\n'
                'In WoS Alliance Chat, R4/R5 can type `!ron helpall on|off` any time.\n'
                '`!ron helpall lock` / `unlock` — block or allow that chat control\n'
                'Slash versions: `/ron status`, `/ron chat_send`, `/ron check`, `/ron events` …')

 async def _prefix_manager(self, ctx):
  member = ctx.author
  if (ctx.guild is None or ctx.guild.id not in self._guild_ids()
      or not isinstance(member, discord.Member)
      or not (member.guild_permissions.manage_guild or member.guild_permissions.administrator)):
   await ctx.send('Manage Server permission is required in a configured HotBot server.',
                  allowed_mentions=discord.AllowedMentions.none())
   return False
  return True

 @commands.group(name='ron', invoke_without_command=True)
 @commands.guild_only()
 async def ron_prefix(self, ctx: commands.Context):
  if await self._prefix_manager(ctx):
   await ctx.send(self.PREFIX_HELP, allowed_mentions=discord.AllowedMentions.none())

 @ron_prefix.command(name='commands', aliases=['help'])
 async def ron_prefix_commands(self, ctx: commands.Context):
  if await self._prefix_manager(ctx):
   await ctx.send(self.PREFIX_HELP, allowed_mentions=discord.AllowedMentions.none())

 @ron_prefix.command(name='dashboard', aliases=['web', 'setup'])
 async def ron_prefix_dashboard(self, ctx: commands.Context):
  if not await self._prefix_manager(ctx):
   return
  try:
   await ctx.author.send(self._dashboard_message())
   await ctx.send('Sent the Ron dashboard link to your DMs.')
  except (discord.Forbidden, discord.HTTPException):
   await ctx.send('I could not DM you. Enable DMs from this server, or use `/ron dashboard`.')

 @ron_prefix.command(name='status')
 async def ron_prefix_status(self, ctx: commands.Context):
  if await self._prefix_manager(ctx):
   await ctx.send('\n'.join(self._status_lines(ctx.guild.id))[:1900],
                  allowed_mentions=discord.AllowedMentions.none())

 @ron_prefix.command(name='say', aliases=['send'])
 async def ron_prefix_say(self, ctx: commands.Context, *, message: str = ''):
  if not await self._prefix_manager(ctx):
   return
  if not message.strip():
   await ctx.send('Usage: `!ron say <message for WoS Alliance Chat>`')
   return
  await ctx.send((await self._queue_chat_message(ctx.guild.id, message))[:1900],
                 allowed_mentions=discord.AllowedMentions.none())

 @ron_prefix.command(name='chat', aliases=['recent'])
 async def ron_prefix_chat(self, ctx: commands.Context):
  if not await self._prefix_manager(ctx):
   return
  rows = list(self._read(ctx.guild.id)['chat_messages'].values())[-12:]
  text = '\n'.join(discord.utils.escape_mentions(str(x.get('text', ''))[:220]) for x in rows)
  try:
   # Alliance chat is private to the alliance, so it goes to the officer's DMs.
   await ctx.author.send((text or 'No recent recognised WoS chat yet.')[:1900])
   await ctx.send('Sent the latest WoS chat readings to your DMs.')
  except (discord.Forbidden, discord.HTTPException):
   await ctx.send('I could not DM you. Enable DMs from this server, or use `/ron chat_recent`.')

 @ron_prefix.command(name='check', aliases=['scan'])
 async def ron_prefix_check(self, ctx: commands.Context, kind: str = ''):
  if not await self._prefix_manager(ctx):
   return
  job = self.PREFIX_CHECKS.get(kind.strip().casefold())
  if job is None:
   await ctx.send('Usage: `!ron check calendar|mail|task|chat|help|notice`')
   return
  result = await self._queue_check(ctx.guild.id, job)
  await ctx.send(f'{result}: {job}. Use `!ron status` for progress.')

 @ron_prefix.command(name='diagnose')
 async def ron_prefix_diagnose(self, ctx: commands.Context):
  if not await self._prefix_manager(ctx):
   return
  done = [await self._queue_check(ctx.guild.id, k) for k in ('chat', 'task_list', 'mail', 'calendar', 'help')]
  await ctx.send(f"Diagnosis requested ({', '.join(done)}). The Pi runs one tablet action at a time; "
                 'use `!ron status` for results.')

 @ron_prefix.group(name='helpall', invoke_without_command=True)
 async def ron_prefix_helpall(self, ctx: commands.Context):
  if await self._prefix_manager(ctx):
   await ctx.send('Usage: `!ron helpall on|off|status|lock|unlock`')

 @ron_prefix_helpall.command(name='on')
 async def ron_helpall_on(self, ctx: commands.Context):
  if await self._prefix_manager(ctx):
   await ctx.send(await self._set_helpall(ctx.guild.id, True, ctx.author.id),
                  allowed_mentions=discord.AllowedMentions.none())

 @ron_prefix_helpall.command(name='off')
 async def ron_helpall_off(self, ctx: commands.Context):
  if await self._prefix_manager(ctx):
   await ctx.send(await self._set_helpall(ctx.guild.id, False, ctx.author.id),
                  allowed_mentions=discord.AllowedMentions.none())

 @ron_prefix_helpall.command(name='status')
 async def ron_helpall_status(self, ctx: commands.Context):
  if not await self._prefix_manager(ctx):
   return
  b = self._read(ctx.guild.id)
  caps = self._capabilities_for(self._guild_ids())
  armed = 'open (R4/R5 can type !ron helpall on/off)' if helpall_chat_allowed(b) else 'LOCKED (!ron helpall unlock)'
  await ctx.send(f"Help All is **{'ON' if caps.get('healer_enabled') else 'OFF'}** on the Pi "
                 f"({'calibrated' if caps.get('healer_calibrated') else 'no Help icon calibration'}). "
                 f"WoS chat control: {armed}. Last tap: {caps.get('healer_last_pressed_utc') or 'never'}.")

 @ron_prefix_helpall.command(name='arm')
 async def ron_helpall_arm(self, ctx: commands.Context, minutes: int = HELPALL_ARM_DEFAULT_MINUTES):
  if not await self._prefix_manager(ctx):
   return
  minutes = max(1, min(HELPALL_ARM_MAX_MINUTES, int(minutes)))
  async with self._lock:
   b = self._read(ctx.guild.id)
   row = b.setdefault('helpall', baseline()['helpall'])
   row['armed_until'] = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
   row['armed_by'] = str(ctx.author.id)
   self._signal(b, ctx.guild.id)
  await ctx.send(f'WoS chat Help All control is **open for {minutes} minute(s)**. Alliance members can type '
                 '`!ron helpall on` or `!ron helpall off` in Alliance Chat until then. '
                 'Disarm early with `!ron helpall disarm`.')

 @ron_prefix_helpall.command(name='disarm')
 async def ron_helpall_disarm(self, ctx: commands.Context):
  if not await self._prefix_manager(ctx):
   return
  await self._helpall_lock(ctx, True)

 async def _helpall_lock(self, ctx, locked):
  async with self._lock:
   b = self._read(ctx.guild.id)
   row = b.setdefault('helpall', baseline()['helpall'])
   row['chat_locked'] = bool(locked)
   row['armed_until'] = None
   self._signal(b, ctx.guild.id)
  await ctx.send('WoS chat Help All control is **locked**: only Discord commands can change it. `!ron helpall unlock` reopens it.'
                 if locked else 'WoS chat Help All control is **open**: R4/R5 can type `!ron helpall on` or `off` in Alliance Chat any time.')

 @ron_prefix_helpall.command(name='lock')
 async def ron_helpall_lock(self, ctx: commands.Context):
  if await self._prefix_manager(ctx):
   await self._helpall_lock(ctx, True)

 @ron_prefix_helpall.command(name='unlock')
 async def ron_helpall_unlock(self, ctx: commands.Context):
  if await self._prefix_manager(ctx):
   await self._helpall_lock(ctx, False)


async def setup(bot: commands.Bot):
 await bot.add_cog(RonBurgundyCog(bot))
