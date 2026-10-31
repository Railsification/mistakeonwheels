# tests/test_bingo.py
from __future__ import annotations

__version__ = "1.0.0"

"""Offline tests for WoS Bingo (core/bingo_logic.py and cogs/games/bingo.py).

Run from the repository root:  python -m unittest discover -s tests -v

GitHub CI installs no packages, so small discord.py stand-ins are used when
discord.py is missing. These are logic/state tests with fake Discord objects;
they do NOT prove behaviour on a live Discord server.
"""

import asyncio
import os
import random
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_stubs():
    try:
        import colorama  # noqa: F401
    except ImportError:
        m = types.ModuleType("colorama")
        m.init = lambda **k: None
        m.Fore = types.SimpleNamespace(GREEN="", YELLOW="", RED="", CYAN="")
        m.Style = types.SimpleNamespace(RESET_ALL="")
        sys.modules["colorama"] = m
    try:
        import discord  # noqa: F401
        return
    except ImportError:
        pass

    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class Plain:
        def __init__(self, *a, **k):
            self.__dict__.update(k)

    class ButtonStyle:
        secondary, danger, success, primary = "secondary", "danger", "success", "primary"

    class Button(Plain):
        pass

    class DynamicItem:
        def __class_getitem__(cls, item):
            return cls

        def __init_subclass__(cls, template=None, **kw):
            cls.template = template

        def __init__(self, item):
            self.item = item

    class _ButtonDescriptor:
        def __init__(self, func, kwargs):
            self.func, self.kwargs = func, kwargs
            self.name = func.__name__

    def button(**kwargs):
        return lambda func: _ButtonDescriptor(func, kwargs)

    class View:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.children = []
            for klass in reversed(type(self).__mro__):
                for name, value in list(vars(klass).items()):
                    if isinstance(value, _ButtonDescriptor):
                        b = Button(**value.kwargs)
                        setattr(self, name, b)
                        self.children.append(b)

        def add_item(self, item):
            self.children.append(item)

    class AllowedMentions(Plain):
        @staticmethod
        def none():
            return AllowedMentions()

    class Colour:
        def __getattr__(self, name):
            return lambda: name

    class Embed:
        def __init__(self, title="", description="", colour=None):
            self.title, self.description, self.fields, self.footer = title, description, [], ""

        def add_field(self, name, value, inline=True):
            self.fields.append((name, value))

        def set_footer(self, text=""):
            self.footer = text

    class NotFound(Exception):
        pass

    class Member(Plain):
        pass

    class File(Plain):
        def __init__(self, fp, filename=""):
            self.fp, self.filename = fp, filename

    def passthrough(*a, **k):
        return lambda f: f

    class Cog:
        @classmethod
        def listener(cls, name=None):
            return lambda f: f

    class Choice(Plain):
        def __class_getitem__(cls, item):
            return cls

    ui = mod("discord.ui", DynamicItem=DynamicItem, Button=Button, View=View, button=button)
    discord = mod(
        "discord", ui=ui, ButtonStyle=ButtonStyle, AllowedMentions=AllowedMentions,
        Colour=Colour(), Embed=Embed, Interaction=Plain, Member=Member, Message=Plain,
        File=File, Attachment=Plain,
        NotFound=NotFound, HTTPException=Exception, Forbidden=Exception,
    )
    app_commands = mod(
        "discord.app_commands", command=passthrough, describe=passthrough,
        choices=passthrough, Choice=Choice,
    )
    ext = mod("discord.ext")
    commands = mod(
        "discord.ext.commands", Cog=Cog, Context=Plain, command=passthrough, Bot=Plain,
        guild_only=passthrough,
    )
    discord.app_commands, discord.ext = app_commands, ext
    ext.commands = commands


_install_stubs()

import discord  # noqa: E402

from core import bingo_logic as bl  # noqa: E402
import cogs.games.bingo as BINGO  # noqa: E402  (bind now; other test files swap stubs later)
import core.game_stats as GAME_STATS  # noqa: E402


# ---------------------------------------------------------------- pure rules


class BingoLogicTests(unittest.TestCase):
    def test_card_shape_and_free_centre(self):
        rng = random.Random(3)
        for _ in range(50):
            card = bl.build_card(len(bl.DEFAULT_WORDS), rng)
            self.assertEqual(len(card), 25)
            self.assertEqual(card[bl.FREE_POS], bl.FREE_WORD)
            others = [x for i, x in enumerate(card) if i != bl.FREE_POS]
            self.assertEqual(len(set(others)), 24)
            self.assertTrue(all(0 <= x < len(bl.DEFAULT_WORDS) for x in others))

    def test_default_words_are_unique_and_fit_buttons(self):
        words = bl.clean_words(bl.DEFAULT_WORDS)
        self.assertEqual(len(words), len(bl.DEFAULT_WORDS))
        self.assertGreaterEqual(len(words), 40)
        self.assertTrue(all(len(w) <= 78 for w in words))  # room for the ✅ prefix

    def test_too_few_words_rejected(self):
        with self.assertRaises(ValueError):
            bl.build_card(23)

    def test_twelve_lines(self):
        lines = bl.winning_lines()
        self.assertEqual(len(lines), 12)
        self.assertIn((0, 6, 12, 18, 24), lines)
        self.assertIn((4, 8, 12, 16, 20), lines)
        self.assertIn((2, 7, 12, 17, 22), lines)

    def test_line_win_uses_free_square(self):
        marked = bl.effective_marks([10, 11, 13, 14])  # middle row with FREE
        self.assertEqual(bl.completed_line(marked), (10, 11, 12, 13, 14))
        self.assertIsNone(bl.completed_line(bl.effective_marks([10, 11, 13])))
        self.assertEqual(bl.squares_to_go(bl.effective_marks([10, 11, 13])), 1)

    def test_full_card_needs_everything(self):
        almost = bl.effective_marks(range(24))
        self.assertIsNone(bl.completed_line(almost, bl.MODE_FULL))
        self.assertEqual(bl.squares_to_go(almost, bl.MODE_FULL), 1)
        self.assertIsNotNone(bl.completed_line(bl.effective_marks(range(25)), bl.MODE_FULL))

    def test_valid_marks_ignore_uncalled_words(self):
        card = list(range(12)) + [-1] + list(range(12, 24))
        marks = [0, 1, 2, 3, 4]
        self.assertIsNone(bl.completed_line(bl.valid_marks(card, marks, [0, 1, 2, 3])))
        self.assertIsNotNone(bl.completed_line(bl.valid_marks(card, marks, [0, 1, 2, 3, 4])))

    def test_pick_next_call_never_repeats(self):
        rng = random.Random(5)
        called = []
        for _ in range(52):
            idx = bl.pick_next_call(52, called, rng)
            self.assertNotIn(idx, called)
            called.append(idx)
        self.assertIsNone(bl.pick_next_call(52, called, rng))

    def test_interval_and_mode_normalising(self):
        self.assertEqual(bl.clamp_interval("3"), bl.MIN_INTERVAL)
        self.assertEqual(bl.clamp_interval(99999), bl.MAX_INTERVAL)
        self.assertEqual(bl.clamp_interval("junk"), bl.DEFAULT_INTERVAL)
        self.assertEqual(bl.normalise_mode("FULL"), bl.MODE_FULL)
        self.assertEqual(bl.normalise_mode(""), bl.MODE_LINE)

    def test_normalise_game_drops_bad_players_keeps_unknown_keys(self):
        good = bl.build_card(52, random.Random(1))
        raw = {
            "words": bl.DEFAULT_WORDS, "called": [1, 1, 999, "2"], "status": "weird",
            "players": {"5": {"card": good, "marks": [0, 99, 12]}, "x": {}, "6": {"card": [1, 2]}},
            "future_field": 42, "message_id": "77",
        }
        g = bl.normalise_game(raw)
        self.assertEqual(g["called"], [1, 2])
        self.assertEqual(g["status"], bl.STATUS_LOBBY)
        self.assertEqual(list(g["players"]), ["5"])
        self.assertEqual(g["players"]["5"]["marks"], [0])
        self.assertEqual(g["future_field"], 42)
        self.assertEqual(g["message_id"], 77)
        self.assertIsNone(bl.normalise_game({"words": ["a"]}))


# ---------------------------------------------------------------- service fakes


class FakeMessage:
    _next = 1000

    def __init__(self, content=None, embed=None, view=None):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.content, self.embed, self.view = content, embed, view
        self.deleted = False


class FakePartial:
    def __init__(self, channel, mid):
        self.channel, self.id = channel, mid

    async def edit(self, embed=None, view=None):
        msg = self.channel.messages.get(self.id)
        if msg is None or msg.deleted:
            raise discord.NotFound("gone")
        msg.embed, msg.view = embed, view

    async def delete(self):
        msg = self.channel.messages.get(self.id)
        if msg is None or msg.deleted:
            raise discord.NotFound("gone")
        msg.deleted = True


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.messages = {}
        self.sent = []

    async def send(self, content=None, embed=None, view=None, allowed_mentions=None):
        msg = FakeMessage(content, embed, view)
        self.messages[msg.id] = msg
        self.sent.append(msg)
        return msg

    def get_partial_message(self, mid):
        return FakePartial(self, mid)


class FakeSettings:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def is_game_allowed(self, gid, cid, game):
        return self.allowed and game == "bingo"

    def is_feature_allowed(self, gid, cid, feature):
        return self.allowed


class FakeBot:
    def __init__(self, channel):
        self.channel = channel
        self.settings = FakeSettings()
        self.hot_config = {"admin_role_names": ["R4"]}

    def get_channel(self, cid):
        return self.channel if cid == self.channel.id else None

    async def fetch_channel(self, cid):
        raise discord.NotFound("no")


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeResponse:
    def __init__(self):
        self.sent, self.edits, self.done = [], [], False

    async def send_message(self, content=None, embed=None, view=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed, "view": view, "ephemeral": ephemeral})
        self.done = True

    async def edit_message(self, content=None, embed=None, view=None):
        self.edits.append({"content": content, "embed": embed, "view": view})
        self.done = True

    async def defer(self, ephemeral=False, thinking=False):
        self.done = True

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, channel):
        self.channel = channel
        self.sent = []

    async def send(self, content=None, embed=None, view=None, ephemeral=False, wait=False):
        if ephemeral:
            self.sent.append(content)
            return None
        return await self.channel.send(content, embed=embed, view=view)


class FakeInteraction:
    def __init__(self, channel, user, message=None, guild_id=1):
        self.guild_id, self.channel_id = guild_id, channel.id
        self.user = user
        self.message = message
        self.response = FakeResponse()
        self.followup = FakeFollowup(channel)


class BingoServiceTests(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)  # core.storage uses the relative data/ folder
        os.makedirs("data", exist_ok=True)
        self.bingo, self.gs = BINGO, GAME_STATS
        self.loop = asyncio.new_event_loop()
        self.channel = FakeChannel(50)
        self.bot = FakeBot(self.channel)
        self.service = BINGO.BingoService(self.bot)
        self.host = FakeUser(10)
        self.p2 = FakeUser(20)

    def tearDown(self):
        self.loop.close()
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def r(self, coro):
        return self.loop.run_until_complete(coro)

    def start(self, mode="line"):
        inter = FakeInteraction(self.channel, self.host)
        self.r(self.service.start_game(inter, mode=mode))
        game = self.service._get_game(1, 50)
        self.assertIsNotNone(game)
        return game, self.channel.messages[game["message_id"]]

    def join(self, user, main):
        inter = FakeInteraction(self.channel, user, message=main)
        self.r(self.service.show_card(inter))
        return inter

    def test_full_game_restart_and_single_stats_record(self):
        game, main = self.start()
        self.assertEqual(game["status"], bl.STATUS_LOBBY)
        self.assertEqual(len(main.view.children), 6)

        j = self.join(self.p2, main)
        card_view = j.response.sent[0]["view"]
        self.assertEqual(len(card_view.children), 25)
        self.assertTrue(all(len(b.item.custom_id) <= 100 for b in card_view.children))
        card = self.service._get_game(1, 50)["players"]["20"]["card"]

        # Rejoining returns the same card, not a new one.
        j2 = self.join(self.p2, main)
        self.assertEqual(self.service._get_game(1, 50)["players"]["20"]["card"], card)
        self.assertEqual(len(j2.response.sent), 1)

        # Non-host cannot control calling.
        txt = self.r(self.service.action_call(1, 50, self.p2))
        self.assertIn("Only the host", txt)

        # Marking an uncalled word is refused.
        mark = FakeInteraction(self.channel, self.p2)
        self.r(self.service.mark_square(mark, game["message_id"], 10))
        self.assertIn("hasn't been called", mark.response.sent[0]["content"])

        # Host starts auto-calling: one immediate call, ticker posted.
        txt = self.r(self.service.action_auto(1, 50, self.host, "resume"))
        self.assertIn("Auto-calling", txt)
        g = self.service._get_game(1, 50)
        self.assertEqual(g["status"], bl.STATUS_RUNNING)
        self.assertEqual(len(g["called"]), 1)
        first_ticker = g["call_message_id"]
        self.assertTrue(first_ticker)

        # ---- simulated restart: brand-new service reading only from disk ----
        self.service = self.bingo.BingoService(self.bot)
        self.service.load_active_index()
        self.assertIn((1, 50), self.service._active)
        g = self.service._get_game(1, 50)
        self.assertEqual(g["players"]["20"]["card"], card)
        self.assertEqual(g["status"], bl.STATUS_RUNNING)

        # Ticker calls when due; previous ticker message gets deleted.
        g["next_call_at"] = 0
        self.service._set_game(1, 50, g)
        self.r(self.service.tick())
        g = self.service._get_game(1, 50)
        self.assertEqual(len(g["called"]), 2)
        self.assertTrue(self.channel.messages[first_ticker].deleted)
        # Not due yet -> no extra call.
        self.r(self.service.tick())
        self.assertEqual(len(self.service._get_game(1, 50)["called"]), 2)

        # Early claim is rejected with a count.
        claim = FakeInteraction(self.channel, self.p2, message=main)
        self.r(self.service.claim(claim))
        self.assertIn("Not yet", claim.response.sent[0]["content"])

        # Call the middle row's words by hand (host), mark them (old card buttons work).
        row = [10, 11, 13, 14]
        g = self.service._get_game(1, 50)
        g["called"] = list(dict.fromkeys(g["called"] + [card[p] for p in row]))
        self.service._set_game(1, 50, g)
        for pos in row:
            m = FakeInteraction(self.channel, self.p2)
            self.r(self.service.mark_square(m, g["message_id"], pos))
            self.assertEqual(len(m.response.edits), 1)

        # Unmark then re-mark works.
        m = FakeInteraction(self.channel, self.p2)
        self.r(self.service.mark_square(m, g["message_id"], 10))
        self.assertNotIn(10, self.service._get_game(1, 50)["players"]["20"]["marks"])
        self.r(self.service.mark_square(FakeInteraction(self.channel, self.p2), g["message_id"], 10))

        # Someone else's (non-joined) press on a card button is refused.
        other = FakeInteraction(self.channel, FakeUser(99))
        self.r(self.service.mark_square(other, g["message_id"], 10))
        self.assertIn("isn't your card", other.response.sent[0]["content"])

        # Valid claim wins, records once, game removed, controls removed.
        claim = FakeInteraction(self.channel, self.p2, message=main)
        self.r(self.service.claim(claim))
        self.assertIn("BINGO confirmed", claim.response.sent[0]["content"])
        self.assertIsNone(self.service._get_game(1, 50))
        self.assertIsNone(main.view)
        stats = self.gs.get_player_stats(1, 20)
        self.assertEqual(stats["games"]["bingo"]["wins"], 1)
        self.assertTrue(any("BINGO!" in (m.content or "") for m in self.channel.sent))

        # A second claim / repeat record cannot double-award.
        claim2 = FakeInteraction(self.channel, self.p2, message=main)
        self.r(self.service.claim(claim2))
        self.assertIn("already finished", claim2.response.sent[0]["content"])
        self.assertFalse(self.gs.record_solo_result(
            1, "bingo", 20, event_id=f"bingo:1:{g['message_id']}", label="Bingo"))
        self.assertEqual(self.gs.get_player_stats(1, 20)["games"]["bingo"]["wins"], 1)

        # New game allowed afterwards; old card buttons now point at an old game.
        game2, _main2 = self.start()
        old = FakeInteraction(self.channel, self.p2)
        self.r(self.service.mark_square(old, g["message_id"], 11))
        self.assertIn("older game", old.response.sent[0]["content"])

    def test_interrupted_finish_is_recovered_once(self):
        game, main = self.start()
        self.join(self.p2, main)
        g = self.service._get_game(1, 50)
        g["status"] = bl.STATUS_WON
        g["winner_id"] = 20
        self.service._set_game(1, 50, g)  # "crash" before recording
        svc = self.bingo.BingoService(self.bot)
        svc.load_active_index()
        self.r(svc.tick())
        self.r(svc.tick())
        self.assertIsNone(svc._get_game(1, 50))
        self.assertEqual(self.gs.get_player_stats(1, 20)["games"]["bingo"]["wins"], 1)
        wins_posts = [m for m in self.channel.sent if "BINGO!" in (m.content or "")]
        self.assertEqual(len(wins_posts), 1)

    def test_one_game_per_channel_and_channel_gate(self):
        self.start()
        inter = FakeInteraction(self.channel, self.p2)
        self.r(self.service.start_game(inter))
        self.assertIn("already running", inter.followup.sent[0])
        self.bot.settings.allowed = False
        self.assertIn("games channel", self.service.start_issue(1, 51))

    def test_end_speed_pause_and_exhaust(self):
        game, main = self.start("full")
        self.assertEqual(game["mode"], bl.MODE_FULL)
        self.assertIn("Use `!bingo speed", self.r(self.service.action_speed(1, 50, self.host, "fast")))
        self.assertIn("20s", self.r(self.service.action_speed(1, 50, self.host, "20")))
        self.assertIn("already paused", self.r(self.service.action_auto(1, 50, self.host, "pause")))
        # Manual call from the lobby leaves auto-calling off.
        self.r(self.service.action_call(1, 50, self.host))
        self.assertEqual(self.service._get_game(1, 50)["status"], bl.STATUS_PAUSED)
        # Exhaust every word -> no winner, game removed.
        for _ in range(60):
            if self.service._get_game(1, 50) is None:
                break
            self.r(self.service.action_call(1, 50, self.host))
        self.assertIsNone(self.service._get_game(1, 50))
        self.assertTrue(any("nobody claimed" in (m.content or "") for m in self.channel.sent))

        # Ending: non-host refused, host allowed.
        self.start()
        self.assertIn("Only the host", self.r(self.service.action_end(1, 50, self.p2)))
        self.assertIn("ended", self.r(self.service.action_end(1, 50, self.host)))
        self.assertIsNone(self.service._get_game(1, 50))

    def test_deleted_game_message_ends_game(self):
        game, main = self.start()
        main.deleted = True
        self.r(self.service.action_call(1, 50, self.host))
        self.assertIsNone(self.service._get_game(1, 50))

    def test_cog_metadata(self):
        meta = self.bingo.BingoCog.GAME_META
        for key in ("key", "label", "kind", "result_word", "description", "emoji", "requires_opponent"):
            self.assertIn(key, meta)
        self.assertEqual(meta["key"], "bingo")
        self.assertFalse(meta["requires_opponent"])
        for key in ("title", "summary", "goal", "how_to_play", "rules", "details"):
            self.assertTrue(self.bingo.BingoCog.HELP_META.get(key))
        for key in ("how_to_play", "rules", "details"):
            self.assertLessEqual(len(self.bingo.BingoCog.HELP_META[key]), 1024)


if __name__ == "__main__":
    unittest.main()
