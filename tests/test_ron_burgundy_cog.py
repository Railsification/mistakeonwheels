# tests/test_ron_burgundy_cog.py
from __future__ import annotations

__version__ = '1.0.0'

"""Offline tests for cogs/wos/ron_burgundy.py (HotBot side of Ron Burgundy).

Run from the repository root:  python -m unittest discover -s tests -v

If discord.py / aiohttp / colorama are installed they are used. Otherwise small
stand-ins are installed so the cog's own logic (multi-guild fan-out, shared
tablet queue, Help All control, prefix commands) can still be exercised.
These are logic tests only; they do NOT prove behaviour on a live Discord server.
"""

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_stubs():
    try:
        import discord  # noqa: F401
        import aiohttp  # noqa: F401
        import colorama  # noqa: F401
        return
    except ImportError:
        pass

    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class Forbidden(Exception):
        pass

    class HTTPException(Exception):
        pass

    class Plain:
        def __init__(self, *a, **k):
            self.__dict__.update(k)

    class AllowedMentions:
        @staticmethod
        def none():
            return 'none'

    def escape_mentions(text):
        return str(text).replace('@', '@​')

    class Command:
        def __init__(self, callback, **kw):
            self.callback = callback
            self.kw = kw
            self.name = kw.get('name', callback.__name__)

        def command(self, **kw):
            return lambda f: Command(f, **kw)

        def group(self, **kw):
            return lambda f: Command(f, **kw)

    def group(**kw):
        return lambda f: Command(f, **kw)

    def guild_only():
        return lambda f: f

    class Cog:
        @classmethod
        def listener(cls, name=None):
            return lambda f: f

        def get_app_commands(self):
            return []

    class AppGroup:
        def __init__(self, name='', description=''):
            self.name = name

        def command(self, **kw):
            return lambda f: f

    class Choice:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, name='', value=''):
            self.name, self.value = name, value

    class RangeMeta:
        def __getitem__(self, item):
            return int

    def passthrough(*a, **k):
        return lambda f: f

    discord = mod('discord', Forbidden=Forbidden, HTTPException=HTTPException,
                  AllowedMentions=AllowedMentions, Interaction=Plain, Member=Plain,
                  TextChannel=Plain, Thread=Plain, DMChannel=Plain, Message=Plain,
                  User=Plain, Object=Plain, utils=types.SimpleNamespace(escape_mentions=escape_mentions))
    app_commands = mod('discord.app_commands', Group=AppGroup, Choice=Choice, Range=RangeMeta(),
                       command=passthrough, choices=passthrough, describe=passthrough)
    ext = mod('discord.ext')
    commands = mod('discord.ext.commands', Cog=Cog, Bot=Plain, Context=Plain, group=group,
                   guild_only=guild_only)
    discord.app_commands, discord.ext = app_commands, ext
    ext.commands = commands

    class Application:
        def __init__(self, **k):
            pass

        def add_routes(self, routes):
            pass

    class HTTPErr(Exception):
        def __init__(self, text=''):
            super().__init__(text)

    web = mod('aiohttp.web', Application=Application, json_response=lambda obj, **k: obj,
              get=lambda *a: None, post=lambda *a: None, AppRunner=Plain, TCPSite=Plain,
              HTTPUnauthorized=HTTPErr, HTTPBadRequest=HTTPErr, HTTPServiceUnavailable=HTTPErr)
    mod('aiohttp', web=web)
    mod('colorama', init=lambda **k: None, Fore=types.SimpleNamespace(GREEN='', YELLOW='', RED='', CYAN=''),
        Style=types.SimpleNamespace(RESET_ALL=''))


_install_stubs()
_TMP = tempfile.TemporaryDirectory()
os.chdir(_TMP.name)  # core.utils uses a relative data/ directory
sys.path.insert(0, str(ROOT))
from cogs.wos import ron_burgundy as ron  # noqa: E402


class FakeSettings:
    def __init__(self, channels):
        self.channels = channels

    def feature_channels(self, guild_id, feature):
        return list(self.channels.get(guild_id, [])) if feature == 'ron' else []


def make_channel(cid, guild_id):
    from unittest import mock
    channel = mock.MagicMock(spec=ron.discord.TextChannel)
    channel.id, channel.sent = cid, []
    channel.guild = types.SimpleNamespace(id=guild_id, me=None)
    channel.permissions_for = lambda member: types.SimpleNamespace(send_messages=True)

    async def send(message, **kwargs):
        channel.sent.append(message)
    channel.send = send
    return channel


class FakeBot:
    def __init__(self, guild_channels):
        self.hot_config = {'all_guild_ids': list(guild_channels), 'admin_guild_id': next(iter(guild_channels))}
        self.settings = FakeSettings({g: [g * 10] for g in guild_channels})
        self.channels = {g * 10: make_channel(g * 10, g) for g in guild_channels}

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        return self.channels[cid]


class FakeRequest:
    def __init__(self, payload=None, query=None):
        self._payload = payload
        self.query = query or {}
        self.headers = {'Authorization': 'Bearer secret'}

    async def json(self):
        return self._payload


class FakeCtx:
    def __init__(self, guild_id, manager=True):
        from unittest import mock
        self.sent, self.dms = [], []
        self.guild = types.SimpleNamespace(id=guild_id)
        self.author = mock.MagicMock(spec=ron.discord.Member)
        self.author.guild_permissions = types.SimpleNamespace(manage_guild=manager, administrator=False)
        self.author.id = 777
        ctx = self

        async def author_send(text, **k):
            ctx.dms.append(text)
        self.author.send = author_send

    async def send(self, text, **kwargs):
        self.sent.append(text)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def body(response):
    """aiohttp's real json_response wraps JSON text; the stand-in returns the dict."""
    return json.loads(response.text) if hasattr(response, 'text') else response


def stamp(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def chat_item(ident, text, minutes_ago=0):
    return {'id': ident, 'kind': 'chat',
            'data': {'text': text, 'source': 'alliance_chat', 'observed_utc': stamp(minutes_ago)}}


class RonCogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        for name in list(sys.modules):
            if name == 'core.utils' or name == 'core.storage':
                sys.modules.pop(name)
        os.environ['RON_BRIDGE_SECRET'] = 'secret'
        os.environ['PORT'] = '0'
        import importlib
        import core.utils, core.storage
        importlib.reload(core.utils)
        importlib.reload(core.storage)
        importlib.reload(ron)
        self.bot = FakeBot({111: None, 222: None})
        self.cog = ron.RonBurgundyCog(self.bot)

    def tearDown(self):
        os.chdir(_TMP.name)
        self.tmp.cleanup()

    def sync(self, *items):
        return body(run(self.cog.sync(FakeRequest({'updates': list(items)}))))

    def jobs(self, kind=None):
        out = []
        for gid in (111, 222):
            out += [j for j in self.cog._read(gid)['jobs'] if kind in (None, j.get('kind'))]
        return out

    # ---- primary-server limits removed -------------------------------------------------
    def test_chat_and_capabilities_reach_every_configured_guild(self):
        caps = {'id': 'caps_0000000001', 'kind': 'capabilities',
                'data': {'chat_capture_enabled': True, 'chat_send_calibrated': True,
                         'monitor_enabled': True, 'healer_enabled': False, 'healer_calibrated': False}}
        reply = self.sync(caps, chat_item('chat_00000000001', 'Rally at the fortress at 20 UTC'))
        self.assertEqual(sorted(reply['ack']), ['caps_0000000001', 'chat_00000000001'])
        for gid in (111, 222):
            b = self.cog._read(gid)
            self.assertTrue(b['bridge_capabilities']['chat_send_calibrated'])
            self.assertEqual(len(b['chat_messages']), 1)
            self.assertEqual(len([d for d in b['deliveries'].values() if d['title'] == 'Alliance Chat']), 1)
        self.assertEqual(self.bot.channels[1110].sent, ['**WoS Alliance Chat (OCR)**\nRally at the fortress at 20 UTC'])
        self.assertEqual(self.bot.channels[2220].sent, ['**WoS Alliance Chat (OCR)**\nRally at the fortress at 20 UTC'])

    def test_chat_send_works_in_a_non_primary_guild_and_is_shared(self):
        msg = run(self.cog._queue_chat_message(222, 'Hello alliance!'))
        self.assertIn('Queued', msg)
        self.assertEqual(len(self.jobs('chat_send')), 1)
        again = run(self.cog._queue_chat_message(111, 'Second message'))
        self.assertIn('already waiting', again)  # one tablet for every guild
        self.assertEqual(len(self.jobs('chat_send')), 1)

    def test_unsent_message_expires_instead_of_blocking_forever(self):
        run(self.cog._queue_chat_message(111, 'Hello'))
        b = self.cog._read(111)
        b['jobs'][-1]['expires_utc'] = stamp(1)
        self.cog._save(b, 111)
        self.assertIn('Queued', run(self.cog._queue_chat_message(222, 'Try again')))
        states = [j['state'] for j in self.jobs('chat_send')]
        self.assertEqual(sorted(states), ['failed', 'queued'])

    def test_discord_text_is_made_typeable_or_refused(self):
        self.assertEqual(ron.wos_chat_text('Don’t forget — rally!\nnow'), 'Dont forget - rally! now')
        self.assertIsNone(ron.wos_chat_text('emoji \U0001F600'))
        self.assertIsNone(ron.wos_chat_text('x' * 181))
        self.assertIn('Use 1-180', run(self.cog._queue_chat_message(111, 'bad \U0001F600')))
        self.assertEqual(self.jobs('chat_send'), [])

    def test_in_game_command_queues_one_reply_across_guilds(self):
        self.sync({'id': 'caps_0000000001', 'kind': 'capabilities',
                   'data': {'chat_send_calibrated': True}})
        self.sync(chat_item('chat_00000000002', '!ron status'))
        replies = self.jobs('chat_send')
        self.assertEqual(len(replies), 1)
        self.assertTrue(ron.CHAT_TEXT.fullmatch(replies[0]['text']))
        # a second reading of the same command within ten minutes is silent
        self.sync(chat_item('chat_00000000003', '!ron status'))
        self.assertEqual(len(self.jobs('chat_send')), 1)

    def test_no_reply_is_queued_when_tablet_cannot_send(self):
        self.sync({'id': 'caps_0000000001', 'kind': 'capabilities',
                   'data': {'chat_send_calibrated': False}})
        self.sync(chat_item('chat_00000000002', '!ron status'))
        self.assertEqual(self.jobs('chat_send'), [])

    def test_stale_chat_is_acknowledged_but_never_acted_on(self):
        self.sync({'id': 'caps_0000000001', 'kind': 'capabilities', 'data': {'chat_send_calibrated': True}})
        reply = self.sync(chat_item('chat_00000000009', '!ron status', minutes_ago=30))
        self.assertEqual(reply['ack'], ['chat_00000000009'])
        self.assertEqual(self.jobs('chat_send'), [])
        self.assertEqual(self.bot.channels[1110].sent, [])

    def test_stamina_reminder_is_typed_once_even_with_two_participating_guilds(self):
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        original = ron.routine_chat_reminder
        ron.routine_chat_reminder = lambda item, now=None: (
            'Stamina reset reminder: collect unclaimed Stamina and Intel rewards before 00:00 UTC.', expires)
        try:
            item = {'id': 'stamina_000000001', 'kind': 'notification', 'data': {
                'title': 'Stamina', 'summary': 'Stamina reminder', 'action_required': True}}
            self.sync(item)
            self.sync(item)
        finally:
            ron.routine_chat_reminder = original
        self.assertEqual(len(self.jobs('chat_send')), 1)

    # ---- Help All control --------------------------------------------------------------
    def test_wos_chat_can_change_help_all_by_default(self):
        self.sync({'id': 'caps_0000000001', 'kind': 'capabilities', 'data': {'chat_send_calibrated': True}})
        self.sync(chat_item('chat_00000000004', '!ron helpall on'))
        self.assertEqual(len(self.jobs('help_control')), 1)

    def test_wos_chat_cannot_change_help_all_while_locked_and_unlock_reopens(self):
        ctx = FakeCtx(111)
        run(self.cog.ron_helpall_lock.callback(self.cog, ctx))
        self.assertIn('locked', ctx.sent[0])
        self.sync({'id': 'caps_0000000001', 'kind': 'capabilities', 'data': {'chat_send_calibrated': True}})
        self.sync(chat_item('chat_00000000004', '!ron helpall on'))
        self.assertEqual(self.jobs('help_control'), [])
        self.assertIn('locked', self.jobs('chat_send')[0]['text'])
        run(self.cog.ron_helpall_unlock.callback(self.cog, ctx))
        self.assertTrue(ron.helpall_chat_allowed(self.cog._read(111)))

    def test_armed_wos_chat_command_queues_exactly_one_help_job(self):
        ctx = FakeCtx(111)
        run(self.cog.ron_helpall_lock.callback(self.cog, ctx))
        run(self.cog.ron_helpall_arm.callback(self.cog, ctx, 10))
        self.assertIn('open for', ctx.sent[-1])
        self.sync({'id': 'caps_0000000001', 'kind': 'capabilities', 'data': {'chat_send_calibrated': True}})
        self.sync(chat_item('chat_00000000005', '!ron help all on'))
        self.sync(chat_item('chat_00000000006', '!ron helpall on'))
        jobs = self.jobs('help_control')
        self.assertEqual(len(jobs), 1)
        self.assertIs(jobs[0]['enabled'], True)
        offered = body(run(self.cog.control(FakeRequest(query={'revision': '0'}))))['jobs']
        self.assertIn({'id': jobs[0]['id'], 'kind': 'help_control', 'guild_id': offered[0]['guild_id'],
                       'enabled': True, 'expires_utc': jobs[0]['expires_utc']}, offered)

    def test_arming_expires_and_disarm_locks_again(self):
        ctx = FakeCtx(222)
        run(self.cog.ron_helpall_lock.callback(self.cog, ctx))
        run(self.cog.ron_helpall_arm.callback(self.cog, ctx, 10))
        self.assertTrue(ron.helpall_armed(self.cog._read(222)))
        run(self.cog.ron_helpall_disarm.callback(self.cog, ctx))
        self.assertFalse(ron.helpall_armed(self.cog._read(222)))
        b = self.cog._read(222)
        b['helpall']['armed_until'] = stamp(1)
        self.assertFalse(ron.helpall_armed(b))

    def test_discord_command_toggles_help_all_and_pi_receipt_completes_it(self):
        ctx = FakeCtx(222)
        run(self.cog.ron_helpall_on.callback(self.cog, ctx))
        job = self.jobs('help_control')[0]
        self.assertEqual(job['state'], 'queued')
        self.sync({'id': 'receipt_000000001', 'kind': 'status',
                   'data': {'job_id': job['id'], 'status': 'completed', 'detail': 'Help All ON'}})
        self.assertEqual([j['state'] for j in self.jobs('help_control')], ['completed'])
        run(self.cog.ron_helpall_on.callback(self.cog, ctx))
        self.assertEqual(len(self.jobs('help_control')), 2)

    def test_wos_helpall_status_is_read_only_and_public(self):
        self.sync({'id': 'caps_0000000001', 'kind': 'capabilities',
                   'data': {'chat_send_calibrated': True, 'healer_enabled': True}})
        self.sync(chat_item('chat_00000000007', '!ron helpall status'))
        self.assertEqual(self.jobs('help_control'), [])
        self.assertIn('Help All is ON', self.jobs('chat_send')[0]['text'])

    # ---- prefix commands ---------------------------------------------------------------
    def test_prefix_commands_require_manage_server(self):
        ctx = FakeCtx(111, manager=False)
        run(self.cog.ron_prefix_say.callback(self.cog, ctx, message='hello'))
        self.assertIn('Manage Server', ctx.sent[0])
        self.assertEqual(self.jobs('chat_send'), [])
        outsider = FakeCtx(999)
        run(self.cog.ron_prefix_status.callback(self.cog, outsider))
        self.assertIn('configured HotBot server', outsider.sent[0])

    def test_prefix_say_check_status_and_chat(self):
        ctx = FakeCtx(222)
        run(self.cog.ron_prefix_say.callback(self.cog, ctx, message='Good luck everyone'))
        self.assertIn('Queued for WoS Alliance Chat', ctx.sent[-1])
        run(self.cog.ron_prefix_check.callback(self.cog, ctx, 'task'))
        self.assertTrue(ctx.sent[-1].startswith('Queued: task_list'))
        run(self.cog.ron_prefix_check.callback(self.cog, ctx, 'task'))
        self.assertTrue(ctx.sent[-1].startswith('Already queued'))
        run(self.cog.ron_prefix_check.callback(self.cog, ctx, 'nonsense'))
        self.assertIn('Usage', ctx.sent[-1])
        run(self.cog.ron_prefix_status.callback(self.cog, ctx))
        self.assertIn('Alliance Help All', ctx.sent[-1])
        run(self.cog.ron_prefix_chat.callback(self.cog, ctx))
        self.assertEqual(len(ctx.dms), 1)

    # ---- dashboard link ----------------------------------------------------------------
    def test_dashboard_link_is_private_validated_and_never_saved(self):
        ctx = FakeCtx(111)
        run(self.cog.ron_prefix_dashboard.callback(self.cog, ctx))
        self.assertIn('not reported', ctx.dms[0])
        good = 'http://192.168.1.100:8765/?token=abcDEF1234567890_-xyz'
        self.sync({'id': 'caps_0000000011', 'kind': 'capabilities', 'data': {'dashboard_url': good}})
        self.sync({'id': 'caps_0000000012', 'kind': 'capabilities',
                   'data': {'dashboard_url': 'http://evil.example/?token=abcDEF1234567890'}})
        ctx2 = FakeCtx(222)
        run(self.cog.ron_prefix_dashboard.callback(self.cog, ctx2))
        self.assertIn(good, ctx2.dms[0])
        self.assertNotIn(good, ctx2.sent[0])  # the channel only gets a notice, the link goes by DM
        for gid in (111, 222):
            self.assertNotIn('abcDEF1234567890', json.dumps(self.cog._read(gid)))
        run(self.cog.ron_prefix_dashboard.callback(self.cog, FakeCtx(111, manager=False)))
        restarted = ron.RonBurgundyCog(self.bot)
        self.assertIsNone(restarted._dashboard_link)

    # ---- restart persistence -----------------------------------------------------------
    def test_arming_and_queued_jobs_survive_a_restart(self):
        ctx = FakeCtx(111)
        run(self.cog.ron_helpall_arm.callback(self.cog, ctx, 30))
        run(self.cog.ron_prefix_say.callback(self.cog, ctx, message='Persist me'))
        restarted = ron.RonBurgundyCog(self.bot)
        self.assertTrue(ron.helpall_armed(restarted._read(111)))
        self.assertEqual([j['text'] for j in restarted._read(111)['jobs'] if j['kind'] == 'chat_send'], ['Persist me'])

    def test_old_saved_files_without_new_keys_still_load(self):
        old = ron.baseline()
        old.pop('helpall')
        ron.save_guild_json(111, ron.DATA, old)
        self.assertIn('helpall', self.cog._read(111))

    def test_existing_slash_and_job_names_are_preserved(self):
        for name in ('calendar', 'mail', 'task_list', 'chat', 'help', 'notice', 'personal', 'chat_send'):
            self.assertIn(name, ron.JOBS)
        for command in ('status_cmd', 'events_cmd', 'alerts_cmd', 'alert_cmd', 'rule_cmd', 'info_cmd',
                        'interval_cmd', 'channel_cmd', 'retry_failed_cmd', 'chat_send_cmd', 'diagnose_cmd',
                        'chat_recent_cmd', 'check_cmd', 'schedule_cmd', 'plan_set_cmd', 'plan_cmd',
                        'plan_editor_cmd', 'plan_pending_cmd', 'plan_approve_cmd'):
            self.assertTrue(hasattr(ron.RonBurgundyCog, command), command)


if __name__ == '__main__':
    unittest.main()
