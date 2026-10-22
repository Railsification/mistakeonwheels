# cogs/wos/ron_burgundy.py
from __future__ import annotations
__version__ = '1.1.0'
"""HotBot-side Ron control; the Raspberry Pi initiates every connection.

No game OCR or tablet navigation is performed on Railway. Data is per-guild and
uses core.storage, and all app commands require Manage Server or Administrator.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime,timezone
from typing import Any

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from core.command_scope import bind_public_cog
from core.storage import load_guild_json,save_guild_json,primary_data_guild_id
from core.logger import info,err

DATA='ron_control.json'
JOBS={'calendar','mail','task_list','chat','help'}
DEFAULT_SCHEDULE={
 'calendar':{'enabled':True,'cadence':'daily','utc_hour':0,'utc_minute':5},
 'mail':{'enabled':True,'cadence':'hourly','utc_minute':0},
 'task_list':{'enabled':True,'cadence':'trigger'},
 'chat':{'enabled':True,'cadence':'periodic','interval_seconds':120},
 'help':{'enabled':False,'cadence':'periodic','interval_seconds':600},
}
DEFAULT_ALERTS={'Foundry Battle':True,'Canyon Clash':True,'Bear Hunt':True,
                'Fortress Battles':True,'Crazy Joe':True,'Alliance Championship':True,
                "Dragonborn's Truck":False,'Frostwind Journey':False,
                'Frostdragon Tyrant':False,
                "Dragonborn's Truck — spend reminder":True,
                'Task List':True,'Alliance Mail':False,'System Mail':False}

def utcnow():return datetime.now(timezone.utc).isoformat()

def baseline():
 return {'schema':1,'revision':1,'alerts':dict(DEFAULT_ALERTS),
         'schedules':json.loads(json.dumps(DEFAULT_SCHEDULE)), 'rules':{}, 'jobs':[],
         'events':{},'receipts':{},'updated_ids':{},'research':{},
         'channel_id':0,'last_seen':None,'deliveries':{}}

def normalize(raw):
 b=baseline()
 if isinstance(raw,dict):
  for k in b:
   if k in raw and isinstance(raw[k],type(b[k])):b[k]=raw[k]
 return b

def safe_title(s):return isinstance(s,str) and 1<=len(s.strip())<=110 and '\n' not in s

def can_manage(interaction):
 u=interaction.user
 return isinstance(u,discord.Member) and (u.guild_permissions.manage_guild or u.guild_permissions.administrator)

class RonBurgundyCog(commands.Cog):
 ron=app_commands.Group(name='ron',description='Ron Burgundy game monitoring and alerts')
 HELP_META={'title':'Ron Burgundy monitoring','summary':'View detected WoS events, control alerts and queue Pi checks.',
 'goal':'Keep WoS schedule and Discord announcements in sync.',
 'how_to_play':'Use /council feature_channel_add with feature ron; use /ron events, /ron alert, /ron check, and /ron schedule.',
 'rules':'Only server managers may use these commands. Pi only pushes changed facts.',
 'details':'No Discord command accesses the tablet directly; the Pi collects queued requests.'}
 def __init__(self,bot):
  self.bot=bot
  self.secret=os.getenv('RON_BRIDGE_SECRET','').strip()
  self.guild_id=primary_data_guild_id(bot)
  self.port=int(os.getenv('PORT','0') or 0)
  self._runner=None
  self._lock=asyncio.Lock()
  self._change=asyncio.Event()
  bind_public_cog(self,bot)
  # Crash/restart midway through Discord send: cannot know if it was received.
  b=self._read()
  changed=False
  for record in b['deliveries'].values():
   if record.get('state')=='sending':
    record['state']='uncertain';changed=True
  if changed:self._save(b)
 async def cog_load(self):
  if not self.secret or not self.guild_id or not self.port:
   info('Ron bridge inactive: configure RON_BRIDGE_SECRET, guild and Railway PORT');return
  app=web.Application(client_max_size=96*1024)
  app.add_routes([web.get('/ron/health',self.health),web.get('/ron/control',self.control),
                  web.post('/ron/sync',self.sync),web.post('/ron/research',self.research)])
  runner=web.AppRunner(app,access_log=None)
  await runner.setup()
  try:await web.TCPSite(runner,host='0.0.0.0',port=self.port).start()
  except Exception:
   await runner.cleanup();raise
  self._runner=runner
  info('Ron bridge enabled on Railway PORT')
 async def cog_unload(self):
  if self._runner:await self._runner.cleanup();self._runner=None
 def _read(self):return normalize(load_guild_json(self.guild_id,DATA,baseline()))
 def _save(self,b):save_guild_json(self.guild_id,DATA,b)
 def _signal(self,b):
  b['revision']=int(b['revision'])+1
  self._save(b)
  self._change.set();self._change=asyncio.Event()
 def _auth(self,request):
  header=request.headers.get('Authorization','')
  if not self.secret or not header.startswith('Bearer ') or not hmac.compare_digest(header[7:],self.secret):
   raise web.HTTPUnauthorized()
 async def _manager(self,interaction):
  if interaction.guild_id!=self.guild_id or not can_manage(interaction):
   await interaction.response.send_message('Manage Server permission required in the configured Ron guild.',ephemeral=True)
   return False
  return True
 async def health(self,request):
  return web.json_response({'service':'ron-burgundy','version':__version__})
 async def control(self,request):
  self._auth(request)
  try:
   rev=int(request.query.get('revision','0'))
   wait=min(25,max(0,int(request.query.get('wait','0'))))
  except ValueError:raise web.HTTPBadRequest(text='Invalid query')
  async with self._lock:
   b=self._read()
   pending=any(j.get('state')=='queued' for j in b['jobs'])
   if rev==b['revision'] and not pending and wait:event=self._change
   else:event=None
  if event:
   try:await asyncio.wait_for(event.wait(),timeout=wait)
   except asyncio.TimeoutError:pass
  async with self._lock:
   b=self._read()
   try:
    stamp=datetime.fromisoformat(b['last_seen']) if b.get('last_seen') else None
   except ValueError:stamp=None
   if stamp is None or (datetime.now(timezone.utc)-stamp).total_seconds()>180:
    b['last_seen']=utcnow();self._save(b)
   return web.json_response({'revision':b['revision'], 'alerts':b['alerts'],
      'schedules':b['schedules'], 'rules':b['rules'], 'jobs':[{'id':j['id'],'kind':j['kind']}
      for j in b['jobs'] if j.get('state')=='queued'][:12]})
 async def sync(self,request):
  self._auth(request)
  try:payload=await request.json()
  except (ValueError,TypeError):raise web.HTTPBadRequest()
  updates=payload.get('updates') if isinstance(payload,dict) else None
  if not isinstance(updates,list) or len(updates)>12:raise web.HTTPBadRequest(text='Invalid updates')
  ack=[]
  async with self._lock:
   b=self._read()
   for item in updates:
    if not isinstance(item,dict):continue
    ident=item.get('id');kind=item.get('kind');data=item.get('data')
    if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',ident):continue
    if kind not in ('event','task','mail','notification','reminder','status','research') or not isinstance(data,dict):continue
    if ident in b['updated_ids']:
     ack.append(ident);continue
    should_notify=False
    name=str(data.get('title') or kind)
    if kind=='event':
     title=data.get('title','')
     if not safe_title(title):continue
     key=hashlib.sha256((title.casefold()+'|'+str(data.get('visible_start_date') or data.get('start_utc') or '')).encode()).hexdigest()[:32]
     b['events'][key]=data
     b['alerts'].setdefault(title,False)
     should_notify=bool(b['alerts'].get(title,False)) and data.get('status')!='needs_review'
    elif kind=='status':
     job_id=data.get('job_id')
     if isinstance(job_id,str):
      for j in b['jobs']:
       if j['id']==job_id:j['state']=str(data.get('status','failed'))
    elif kind in ('task','mail','reminder','notification'):
     should_notify=bool(b['alerts'].get(name,False))
    if should_notify:
     # Council feature channels are authoritative; do not use a second channel setting.
     for cid in dict.fromkeys(self.bot.settings.feature_channels(self.guild_id,'ron')):
      item_id=ident+':'+str(cid)
      b['deliveries'].setdefault(item_id,{'id':ident,'channel_id':int(cid),
                                         'title':name,'data':data,'state':'queued'})
    # Persist the event and pending Discord deliveries before acknowledging the Pi.
    b['updated_ids'][ident]=utcnow()
    ack.append(ident)
   if len(b['updated_ids'])>2500:
    for k in list(b['updated_ids'])[:-1800]:b['updated_ids'].pop(k,None)
   if len(b['events'])>350:
    for k in list(b['events'])[:-250]:b['events'].pop(k,None)
   b['jobs']=b['jobs'][-250:]
   if len(b['deliveries'])>2400:
    for k in list(b['deliveries']):
     if len(b['deliveries'])<=1800:break
     if b['deliveries'][k].get('state')=='delivered':b['deliveries'].pop(k,None)
   b['last_seen']=utcnow()
   self._save(b)
  # Do not send a Discord message while the state lock is held.
  await self._deliver_pending()
  return web.json_response({'ack':ack})
 async def _deliver_pending(self, limit=12):
  """Send durable queued messages with explicit delivery states.

  A process crash while sending is marked 'uncertain' rather than replayed
  automatically, because a duplicate alert would be worse than a visible
  uncertain delivery. /ron status reports these cases for manual review.
  """
  for _ in range(limit):
   async with self._lock:
    b=self._read()
    key=next((k for k,v in b['deliveries'].items() if v.get('state')=='queued'),None)
    if key is None:return
    item=b['deliveries'][key]
    item['state']='sending'
    item['attempts']=int(item.get('attempts',0))+1
    self._save(b)
   ok=await self._send_notification(item['title'],item['data'],item['channel_id'])
   async with self._lock:
    b=self._read()
    target=b['deliveries'].get(key)
    if target and target.get('state')=='sending':
     target['state']='delivered' if ok else 'failed'
     target['updated_utc']=utcnow()
     self._save(b)
 async def _send_notification(self,title,data,cid):
  safe=discord.utils.escape_mentions(str(title)[:110])
  if data.get('currency'):
   expiry=str(data.get('end_utc') or 'Unconfirmed')[:45]
   currency=discord.utils.escape_mentions(str(data['currency'])[:80])
   message=f'**WoS · {safe}**\nSpend {currency} before the shop closes: {expiry} UTC. One-hour warning.'
  elif data.get('kind') in ('mail','task'):
   summary=discord.utils.escape_mentions(str(data.get('summary',''))[:1300]).replace(chr(96)*3,chr(39)*3)
   message=f'**WoS · {safe} updated**\n```text\n{summary}\n```'
  else:
   start=data.get('start_utc') or data.get('visible_start_date') or 'Unconfirmed'
   end=data.get('end_utc') or data.get('visible_end_date') or 'Unconfirmed'
   message=f'**WoS · {safe}**\nStart: {str(start)[:40]} UTC\nEnd: {str(end)[:40]} UTC'
  try:
   channel=self.bot.get_channel(int(cid)) or await self.bot.fetch_channel(int(cid))
   if not isinstance(channel,(discord.TextChannel,discord.Thread)) or not channel.guild or channel.guild.id!=self.guild_id:
    return False
   member=channel.guild.me
   if member is not None and not channel.permissions_for(member).send_messages:
    return False
   await channel.send(message,allowed_mentions=discord.AllowedMentions.none())
   return True
  except (discord.Forbidden,discord.HTTPException,ValueError,TypeError) as exc:
   err(f'Ron notification could not post in council-configured channel: {type(exc).__name__}')
   return False
 async def _announce(self,title,data):
  """Compatibility entrypoint, now using only council channel assignments."""
  results=[]
  for cid in dict.fromkeys(self.bot.settings.feature_channels(self.guild_id,'ron')):
   results.append(await self._send_notification(title,data,cid))
  return all(results) if results else False
 async def research(self,request):
  """HotBot-only OpenAI web lookup for NEW event names; never trusted as a game date."""
  self._auth(request)
  data=await request.json()
  title=data.get('title','') if isinstance(data,dict) else ''
  if not safe_title(title):raise web.HTTPBadRequest(text='Invalid title')
  async with self._lock:
   b=self._read()
   cached=b['research'].get(title)
   if cached:return web.json_response({'result':cached,'cached':True})
  key=os.getenv('OPENAI_API_KEY','').strip()
  if not key:raise web.HTTPServiceUnavailable(text='OpenAI not configured')
  try:
   from openai import AsyncOpenAI
   client=AsyncOpenAI(api_key=key,timeout=36)
   response=await client.responses.create(model=os.getenv('RON_RESEARCH_MODEL','gpt-4.1-mini'),
      tools=[{'type':'web_search'}],store=False,
      input=('Research the Whiteout Survival event '+title+'. Use official Century Games help and reputable community wiki sources. '
             'Summarize confirmed event duration, associated currency/shop expiry and relevant notification timing, '
             'include source URLs. Mark unknown facts unknown. Do NOT invent dates for a specific game state.'),max_output_tokens=600)
   result=str(response.output_text)[:5000]
  except Exception as exc:
   err('Ron AI event research unavailable: '+type(exc).__name__)
   raise web.HTTPServiceUnavailable(text='Research temporarily unavailable') from exc
  async with self._lock:
   b=self._read();b['research'][title]=result
   self._save(b)
  return web.json_response({'result':result,'cached':False})

 @ron.command(name='status',description='View Pi health and pending Ron checks')
 async def status_cmd(self,interaction:discord.Interaction):
  if not await self._manager(interaction):return
  b=self._read()
  pending=sum(j['state']=='queued' for j in b['jobs'])
  delivery_errors=sum(x.get('state') in ('failed','uncertain') for x in b['deliveries'].values())
  await interaction.response.send_message(
   f"Pi last update: {b['last_seen'] or 'Never'}\nKnown events: {len(b['events'])}\nPending checks: {pending}\nFailed/uncertain notification deliveries: {delivery_errors}\nAlert channels (council feature ron): {', '.join(str(x) for x in self.bot.settings.feature_channels(self.guild_id,'ron')) or 'Not selected'}",ephemeral=True)
 @ron.command(name='events',description='View the event schedule last supplied by the Pi')
 async def events_cmd(self,interaction:discord.Interaction):
  if not await self._manager(interaction):return
  b=self._read()
  rows=sorted(b['events'].values(),key=lambda x:str(x.get('visible_start_date') or x.get('start_utc') or ''))[:30]
  lines=[f"{x.get('title','?')}: {x.get('start_utc') or x.get('visible_start_date') or '?'} → {x.get('end_utc') or x.get('visible_end_date') or '?'}" for x in rows]
  await interaction.response.send_message(('\n'.join(lines) or 'No events received yet.')[:1900],ephemeral=True)
 @ron.command(name='alerts',description='Show available event notifications')
 async def alerts_cmd(self,interaction:discord.Interaction):
  if not await self._manager(interaction):return
  b=self._read()
  lines=[f"{'ON' if val else 'OFF'} — {key}" for key,val in sorted(b['alerts'].items())]
  await interaction.response.send_message(('\n'.join(lines) or 'No alert types yet.')[:1900],ephemeral=True)
 @ron.command(name='alert',description='Enable or disable notifications for any detected event')
 @app_commands.describe(event='Exact event name from /ron alerts',enabled='True sends announcements when this event changes')
 async def alert_cmd(self,interaction:discord.Interaction,event:str,enabled:bool):
  if not await self._manager(interaction):return
  if not safe_title(event):
   await interaction.response.send_message('Invalid event name.',ephemeral=True);return
  async with self._lock:
   b=self._read();b['alerts'][event.strip()]=enabled;self._signal(b)
  await interaction.response.send_message(f"Notifications for **{discord.utils.escape_mentions(event)}**: {'ON' if enabled else 'OFF'}",ephemeral=True)
 @ron.command(name='rule',description='Set event duration/category without touching Pi scripts')
 @app_commands.choices(category=[app_commands.Choice(name=n.title(),value=n) for n in ('event','shop','alliance','phased')])
 async def rule_cmd(self,interaction:discord.Interaction,event:str,category:app_commands.Choice[str],days:app_commands.Range[int,1,30],currency:str=''):
  if not await self._manager(interaction):return
  if not safe_title(event) or len(currency)>80:
   await interaction.response.send_message('Invalid event name or currency.',ephemeral=True);return
  rule={'kind':category.value,'days':days}
  if currency.strip() and category.value=='shop':
   rule['spend_currency']=currency.strip()
   rule['reminder_hours_before_end']=1
  async with self._lock:
   b=self._read();b['rules'][event.strip()]=rule;self._signal(b)
  await interaction.response.send_message(f'Event rule updated for {discord.utils.escape_mentions(event)}. The Pi will receive it on its next connection.',ephemeral=True)
 @ron.command(name='info',description='Read cached researched information about an event')
 async def info_cmd(self,interaction:discord.Interaction,event:str):
  if not await self._manager(interaction):return
  b=self._read()
  text=b['research'].get(event)
  await interaction.response.send_message(
   discord.utils.escape_mentions(str(text or 'No research cached for this event yet.'))[:1900],
   ephemeral=True)
 @ron.command(name='interval',description='Set the interval for chat or Alliance Help checks')
 @app_commands.choices(kind=[app_commands.Choice(name='Chat',value='chat'),app_commands.Choice(name='Alliance Help',value='help')])
 async def interval_cmd(self,interaction:discord.Interaction,kind:app_commands.Choice[str],seconds:app_commands.Range[int,5,3600]):
  if not await self._manager(interaction):return
  async with self._lock:
   b=self._read()
   b['schedules'][kind.value]['interval_seconds']=seconds
   self._signal(b)
  await interaction.response.send_message(f'{kind.value} interval set to {seconds} seconds UTC-independent; Pi will sync.',ephemeral=True)
 @ron.command(name='channel',description='Compatibility: add a Ron channel through council settings')
 async def channel_cmd(self,interaction:discord.Interaction,channel:discord.TextChannel):
  if not await self._manager(interaction):return
  me=interaction.guild.me if interaction.guild else None
  if me is not None and not channel.permissions_for(me).send_messages:
   await interaction.response.send_message('HotBot cannot send messages in that channel.',ephemeral=True)
   return
  async with self._lock:
   self.bot.settings.add_feature_channel(self.guild_id,'ron',channel.id)
  await interaction.response.send_message(f'Ron feature **ron** added to {channel.mention}; council feature settings updated.',ephemeral=True)
 @ron.command(name='retry_failed',description='Retry failed Ron notifications after resolving channel permissions')
 async def retry_failed_cmd(self,interaction:discord.Interaction):
  if not await self._manager(interaction):return
  async with self._lock:
   b=self._read()
   count=0
   for record in b['deliveries'].values():
    if record.get('state')=='failed':
     record['state']='queued';count+=1
   self._save(b)
  await self._deliver_pending()
  await interaction.response.send_message(f'Retried {count} failed notifications. Uncertain deliveries require manual review to avoid duplicates.',ephemeral=True)
 @ron.command(name='check',description='Queue one check for the Pi, without opening the game on Railway')
 @app_commands.choices(kind=[app_commands.Choice(name=k.replace('_',' ').title(),value=k) for k in sorted(JOBS)])
 async def check_cmd(self,interaction:discord.Interaction,kind:app_commands.Choice[str]):
  if not await self._manager(interaction):return
  async with self._lock:
   b=self._read()
   active=next((j for j in b['jobs'] if j['kind']==kind.value and j['state']=='queued'),None)
   if active:result='Already queued'
   else:
    b['jobs'].append({'id':'check_'+secrets.token_hex(12),'kind':kind.value,'state':'queued','created_utc':utcnow()})
    self._signal(b);result='Queued'
  await interaction.response.send_message(f'{result}: {kind.value}. Check /ron status for progress.',ephemeral=True)
 @ron.command(name='schedule',description='Set the daily UTC check time and enable/disable a job')
 @app_commands.choices(kind=[app_commands.Choice(name=k.replace('_',' ').title(),value=k) for k in ('calendar','mail','task_list','chat','help')])
 async def schedule_cmd(self,interaction:discord.Interaction,kind:app_commands.Choice[str],enabled:bool,utc_hour:app_commands.Range[int,0,23]=0,utc_minute:app_commands.Range[int,0,59]=0):
  if not await self._manager(interaction):return
  async with self._lock:
   b=self._read();old=b['schedules'].get(kind.value,{})
   if old.get('cadence')=='daily':old.update(enabled=enabled,utc_hour=utc_hour,utc_minute=utc_minute)
   elif old.get('cadence')=='hourly':old.update(enabled=enabled,utc_minute=utc_minute)
   else:old['enabled']=enabled
   b['schedules'][kind.value]=old;self._signal(b)
  await interaction.response.send_message(f'{kind.value} schedule: {old}. Saved on HotBot; Pi will sync.',ephemeral=True)

async def setup(bot:commands.Bot):
 await bot.add_cog(RonBurgundyCog(bot))
