# tests/test_chest_pattern.py
from __future__ import annotations

__version__ = '1.8.5'

"""Offline tests for cogs/wos/chest_pattern.py (Dead Shot helper).

Run from the repository root:  python -m unittest discover -s tests -v

Uses small discord.py stand-ins when discord.py is not installed, and real
OpenCV/numpy for the image tests (skipped if they are missing). These are logic
tests; they do NOT prove behaviour on a live Discord server.
"""

import asyncio
import json
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
    # GitHub CI installs no packages: core.logger needs colorama.
    try:
        import colorama  # noqa: F401
    except ImportError:
        m = types.ModuleType('colorama')
        m.init = lambda **k: None
        m.Fore = types.SimpleNamespace(GREEN='', YELLOW='', RED='', CYAN='')
        m.Style = types.SimpleNamespace(RESET_ALL='')
        sys.modules['colorama'] = m
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
        secondary, danger, success, primary = 'secondary', 'danger', 'success', 'primary'

    class Button(Plain):
        pass

    class DynamicItem:
        def __class_getitem__(cls, item):
            return cls

        def __init_subclass__(cls, template=None, **kw):
            cls.template = template

        def __init__(self, item):
            self.item = item

    class View:
        def __init__(self, timeout=None):
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class AllowedMentions:
        @staticmethod
        def none():
            return 'none'

    class File(Plain):
        def __init__(self, fp, filename=''):
            self.fp, self.filename = fp, filename

    def passthrough(*a, **k):
        return lambda f: f

    class Cog:
        @classmethod
        def listener(cls, name=None):
            return lambda f: f

    ui = mod('discord.ui', DynamicItem=DynamicItem, Button=Button, View=View)
    discord = mod('discord', ui=ui, ButtonStyle=ButtonStyle, AllowedMentions=AllowedMentions,
                  File=File, Interaction=Plain, Attachment=Plain, Message=Plain)
    app_commands = mod('discord.app_commands', command=passthrough, describe=passthrough)
    ext = mod('discord.ext')
    commands = mod('discord.ext.commands', Cog=Cog, Context=Plain, command=passthrough,
                   guild_only=passthrough, Bot=Plain)
    discord.app_commands, discord.ext = app_commands, ext
    ext.commands = commands


_install_stubs()

try:
    import cv2  # noqa: F401
    import numpy as np
    HAVE_CV = True
except Exception:
    HAVE_CV = False

import cogs.wos.chest_pattern as cp  # noqa: E402


class FakeUser:
    def __init__(self, uid, manager=False):
        self.id = uid
        self.guild_permissions = types.SimpleNamespace(manage_guild=manager, administrator=False)


class FakeResponse:
    def __init__(self):
        self.edits, self.sent = [], []

    async def edit_message(self, content=None, view=None):
        self.edits.append((content, view))

    async def send_message(self, content, ephemeral=False):
        self.sent.append(content)


class FakeInteraction:
    def __init__(self, guild_id, user):
        self.guild_id, self.user = guild_id, user
        self.response = FakeResponse()


class FakeBot:
    hot_config = {}


class TempDataMixin:
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()


def legacy_v1_file():
    """A version 1 knowledge file as it exists on Railway today."""
    g = cp.fresh_guild_data()
    for k in ('model', 'legacy', 'boards', 'pending', 'samples', 'dhashes'):
        g.pop(k)
    g['totals']['R1C1']['KEY'] = 9
    g['totals']['R2C3']['KEY'] = 7
    g['totals']['R1C2']['PLUS'] = 5
    g['hashes'] = ['abc', 'def']
    g['images'] = 8
    g['last_order'] = cp.all_cells()
    g['order_stats']['1']['PLUS'] = 3
    return g


class ModelTests(TempDataMixin, unittest.TestCase):
    def test_legacy_data_preserved_and_migrated(self):
        g = legacy_v1_file()
        before = json.loads(json.dumps(g))
        data = {'guilds': {'1': g}}
        out = cp.ensure_guild_data(data, '1')
        for key, value in before.items():
            self.assertEqual(out[key], value, key)
        self.assertEqual(out['legacy']['key_counts']['1'], 9)
        self.assertEqual(out['legacy']['key_counts']['7'], 7)
        self.assertEqual(out['legacy']['images'], 8)
        # Running again must not re-snapshot or double count.
        out['totals']['R1C1']['KEY'] = 50
        cp.ensure_guild_data(data, '1')
        self.assertEqual(out['legacy']['key_counts']['1'], 9)

    def test_legacy_file_round_trip_through_storage(self):
        cp.save_guild_json(42, cp.KNOWLEDGE_FILENAME, legacy_v1_file())
        key, data, g = cp.ChestPatternCog(FakeBot())._guild(42)
        self.assertEqual(g['hashes'], ['abc', 'def'])
        self.assertEqual(g['order_stats']['1']['PLUS'], 3)
        self.assertEqual(g['legacy']['images'], 8)

    def test_position_mapping(self):
        self.assertEqual(cp.pos_to_cell(1), 'R1C1')
        self.assertEqual(cp.pos_to_cell(8), 'R2C4')
        self.assertEqual(cp.pos_to_cell(12), 'R3C4')
        self.assertEqual([cp.cell_to_pos(c) for c in cp.all_cells()], cp.POSITIONS)

    def test_valid_keys(self):
        self.assertEqual(cp.valid_keys([10, 3]), [3, 10])
        self.assertIsNone(cp.valid_keys([3, 3]))
        self.assertIsNone(cp.valid_keys([3]))
        self.assertIsNone(cp.valid_keys([0, 13]))
        self.assertIsNone(cp.valid_keys(['x', 2]))

    def test_chi2_sf_against_known_values(self):
        self.assertAlmostEqual(cp.chi2_sf(19.675, 11), 0.05, places=3)
        self.assertAlmostEqual(cp.chi2_sf(24.725, 11), 0.01, places=3)
        self.assertAlmostEqual(cp.chi2_sf(5.578, 11), 0.90, places=2)
        self.assertEqual(cp.chi2_sf(0, 11), 1.0)

    def test_random_keys_show_no_pattern(self):
        rng = random.Random(7)
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        for _ in range(300):
            cp.record_board(g, {'src': 'manual'}, rng.sample(cp.POSITIONS, 2), 1)
        m = cp.build_model(g)
        self.assertEqual(m['boards'], 300)
        self.assertGreater(m['p_value'], 0.01)
        self.assertIn('no evidence', cp.pattern_verdict(m))

    def test_biased_keys_detected_and_avoided(self):
        rng = random.Random(3)
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        weights = [8 if p in (2, 11) else 1 for p in cp.POSITIONS]
        for _ in range(150):
            keys = set()
            while len(keys) < 2:
                keys.add(rng.choices(cp.POSITIONS, weights)[0])
            cp.record_board(g, {'src': 'manual'}, sorted(keys), 1)
        m = cp.build_model(g)
        self.assertLess(m['p_value'], 0.01)
        self.assertEqual(set(m['order'][-2:]), {2, 11})
        self.assertIn('strong evidence', cp.pattern_verdict(m))
        self.assertEqual(g['last_order'][-2:] in (['R1C2', 'R3C3'], ['R3C3', 'R1C2']), True)

    def test_second_key_odds_learn_pairs(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        for _ in range(40):
            cp.record_board(g, {'src': 'manual'}, [2, 8], 1)
        odds, seen = cp.second_key_odds(cp.build_model(g), 2)
        self.assertEqual(seen, 40)
        self.assertNotIn(2, odds)
        self.assertEqual(max(odds, key=odds.get), 8)
        self.assertAlmostEqual(sum(odds.values()), 1.0, places=6)

    def test_unopened_cells_are_not_rewarded(self):
        """v1 bug: unopened (TARGET) counted as a good outcome."""
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        syms = {str(p): 'TARGET' for p in cp.POSITIONS}
        syms.update({'1': 'KEY', '2': 'KEY', '3': 'PLUS'})
        for _ in range(30):
            cp.record_board(g, {'src': 'manual', 'symbols': syms}, [1, 2], 1)
        m = cp.build_model(g)
        self.assertEqual(set(m['order'][-2:]), {1, 2})
        # Opened-but-not-key (3) and unopened cells are equally safe.
        self.assertAlmostEqual(m['prob'][3], m['prob'][12])

    def test_first_key_skips_targets_already_shot(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        m = cp.build_model(g)
        order = cp.followed_order(g, m)
        self.assertEqual(order, list(range(1, 13)))
        text = cp.second_key_text(m, 5, order)
        self.assertIn('1, 2, 3, 4 already open', text)
        self.assertIn('Shoot next:** 6', text)
        self.assertIn('All 7 targets left are equal (14% each)', text)
        self.assertNotIn('1 → 2', text)
        self.assertIn('!ds 5 <second key>', text)
        first = cp.second_key_text(m, 1, order)
        self.assertIn('the key was your first shot', first)

    def test_first_key_ranks_remaining_by_pairs(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        for _ in range(30):
            cp.record_board(g, {'src': 'manual'}, [5, 6], 1)
        m = cp.build_model(g)
        text = cp.second_key_text(m, 5, cp.followed_order(g, m))
        # Keys always at 5 and 6, so 5 and 6 come last in the order: once 5 is
        # hit, every other target has been shot and only 6 remains.
        self.assertIn('Only 6 is left', text)
        mixed = cp.second_key_text(m, 5, list(range(1, 13)))
        line = [l for l in mixed.splitlines() if l.startswith('**Shoot next')][0]
        self.assertTrue(line.rstrip().endswith('6'))

    def test_find_order_kept_stats_unchanged(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        board = cp.record_board(g, {'src': 'manual'}, [11, 4], 1, found=[11, 4])
        self.assertEqual(board['keys'], [4, 11])
        self.assertEqual(board['found'], [11, 4])
        self.assertIn('first key **11**, second key **4**', cp.saved_text(board, cp.build_model(g)))
        m = cp.build_model(g)
        self.assertEqual(m['key_counts'][4], 1)
        self.assertEqual(m['pair'][4][11], 1)

    def test_image_board_has_no_invented_find_order(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        board = cp.record_board(g, {'src': 'image'}, [3, 9], 1)
        self.assertIsNone(board['found'])
        self.assertIn('keys at **3** and **9**', cp.saved_text(board, cp.build_model(g)))
        self.assertEqual(cp.board_found({'keys': [3, 9], 'found': [9, 2]}), [3, 9])
        self.assertEqual(cp.board_found({'keys': [3, 9]}), [3, 9])

    def test_record_keeps_v1_totals_and_order_stats(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        order = cp.all_cells()
        syms = {str(p): 'PLUS' for p in cp.POSITIONS}
        cp.record_board(g, {'src': 'image', 'symbols': syms, 'exact_order': order,
                            'hash': 'h1', 'dhash': '0' * 16}, [5, 6], 1)
        self.assertEqual(g['totals']['R2C1']['KEY'], 1)
        self.assertEqual(g['totals']['R1C1']['PLUS'], 1)
        self.assertEqual(g['images'], 1)
        self.assertIn('h1', g['hashes'])
        self.assertEqual(sum(g['order_stats']['1'].values()), 1)

    def test_fix_and_undo_keep_totals_consistent(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        syms = {str(p): 'UPGRADE' for p in cp.POSITIONS}
        board = cp.record_board(g, {'src': 'image', 'symbols': syms}, [1, 2], 1)
        cp.record_board(g, {'replaces': board['id']}, [3, 4], 1)
        self.assertEqual(len(g['boards']), 1)
        self.assertEqual(g['boards'][0]['keys'], [3, 4])
        self.assertEqual(g['totals']['R1C1']['KEY'], 0)
        self.assertEqual(g['totals']['R1C3']['KEY'], 1)
        self.assertEqual(g['totals']['R1C1']['UPGRADE'], 0)
        self.assertEqual(g['totals']['R1C1']['PLUS'], 1)
        cp.remove_board(g, board['id'])
        self.assertEqual(g['boards'], [])
        self.assertEqual(sum(sum(c.values()) for c in g['totals'].values()), 0)
        self.assertEqual(g['images'], 0)

    def test_legacy_prior_fades(self):
        g = cp.ensure_guild_data({'guilds': {'1': legacy_v1_file()}}, '1')
        m0 = cp.build_model(g)
        self.assertGreater(m0['prob'][1], m0['prob'][12])
        for _ in range(240):
            cp.record_board(g, {'src': 'manual'}, [11, 12], 1)
        m1 = cp.build_model(g)
        self.assertLess(m1['legacy_weight'], cp.LEGACY_WEIGHT)
        self.assertEqual(set(m1['order'][-2:]), {11, 12})

    def test_summary_fits_discord_and_csv_shape(self):
        g = cp.ensure_guild_data({'guilds': {'1': legacy_v1_file()}}, '1')
        ranked = cp.build_ranked_order(g)
        self.assertEqual([r['rank'] for r in ranked], list(range(1, 13)))
        text = cp.build_summary(ranked, 3, 1, 1, 8, ['bad.gif'], [], True, False,
                                model=cp.build_model(g))
        self.assertLess(len(text), 2000)
        self.assertIn('Shoot order', text)
        self.assertLess(len(cp.second_key_text(cp.build_model(g), 5)), 2000)


class ButtonTests(TempDataMixin, unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def _pending(self, keys, owner=10):
        cog = cp.ChestPatternCog(FakeBot())
        key, data, g = cog._guild(5)
        pid = cp.new_board_id(g)
        g['pending'][pid] = {'id': pid, 'by': str(owner), 't': cp.utcnow(), 'src': 'image',
                             'keys': keys, 'symbols': {str(p): 'PLUS' for p in cp.POSITIONS},
                             'feats': {str(p): [1 / 32] * 32 for p in cp.POSITIONS},
                             'hash': 'hh', 'dhash': 'f' * 16}
        cog._save(key, data)
        return pid

    def test_restart_toggle_save_exactly_once(self):
        pid = self._pending([3])
        # A brand-new cog object = bot restarted; state must come from disk.
        cog = cp.ChestPatternCog(FakeBot())
        owner = FakeUser(10)
        self.run_async(cog.handle_board_button(FakeInteraction(5, owner), pid, 'k9'))
        cog = cp.ChestPatternCog(FakeBot())
        _, _, g = cog._guild(5)
        self.assertEqual(g['pending'][pid]['keys'], [3, 9])
        inter = FakeInteraction(5, owner)
        self.run_async(cog.handle_board_button(inter, pid, 'save'))
        self.assertIn('Saved', inter.response.edits[-1][0])
        again = FakeInteraction(5, owner)
        self.run_async(cp.ChestPatternCog(FakeBot()).handle_board_button(again, pid, 'save'))
        self.assertIn('already saved', again.response.sent[-1])
        _, _, g = cog._guild(5)
        self.assertEqual(len(g['boards']), 1)
        self.assertEqual(g['images'], 1)
        self.assertEqual(len(g['samples']), 12)
        self.assertEqual(sum(1 for s in g['samples'] if s['label'] == 'KEY'), 2)

    def test_tap_order_becomes_find_order(self):
        pid = self._pending([])
        cog = cp.ChestPatternCog(FakeBot())
        for act in ('k11', 'k4', 'save'):
            cog = cp.ChestPatternCog(FakeBot())
            inter = FakeInteraction(5, FakeUser(10))
            self.run_async(cog.handle_board_button(inter, pid, act))
        self.assertIn('first key **11**, second key **4**', inter.response.edits[-1][0])
        _, _, g = cog._guild(5)
        self.assertEqual(g['boards'][0]['found'], [11, 4])
        bid = g['boards'][0]['id']
        self.run_async(cog.handle_board_button(FakeInteraction(5, FakeUser(10)), bid, 'fix'))
        _, _, g = cog._guild(5)
        fix = [v for v in g['pending'].values() if v.get('replaces') == bid][0]
        self.assertEqual(fix['found'], [11, 4])
        self.run_async(cog.handle_board_button(FakeInteraction(5, FakeUser(10)), fix['id'], 'save'))
        _, _, g = cog._guild(5)
        self.assertEqual(g['boards'][0]['found'], [11, 4])

    def test_save_needs_two_keys(self):
        pid = self._pending([3])
        cog = cp.ChestPatternCog(FakeBot())
        inter = FakeInteraction(5, FakeUser(10))
        self.run_async(cog.handle_board_button(inter, pid, 'save'))
        self.assertIn('Exactly two keys', inter.response.edits[-1][0])
        _, _, g = cog._guild(5)
        self.assertEqual(g['boards'], [])
        self.assertIn(pid, g['pending'])

    def test_third_key_replaces_oldest(self):
        pid = self._pending([3, 9])
        cog = cp.ChestPatternCog(FakeBot())
        self.run_async(cog.handle_board_button(FakeInteraction(5, FakeUser(10)), pid, 'k12'))
        _, _, g = cog._guild(5)
        self.assertEqual(g['pending'][pid]['keys'], [9, 12])

    def test_other_member_blocked_manager_allowed(self):
        pid = self._pending([3, 9])
        cog = cp.ChestPatternCog(FakeBot())
        stranger = FakeInteraction(5, FakeUser(99))
        self.run_async(cog.handle_board_button(stranger, pid, 'drop'))
        self.assertIn('Only the person', stranger.response.sent[-1])
        boss = FakeInteraction(5, FakeUser(99, manager=True))
        self.run_async(cog.handle_board_button(boss, pid, 'drop'))
        _, _, g = cog._guild(5)
        self.assertNotIn(pid, g['pending'])
        self.assertEqual(g['boards'], [])

    def test_fix_saved_board(self):
        pid = self._pending([3, 9])
        cog = cp.ChestPatternCog(FakeBot())
        self.run_async(cog.handle_board_button(FakeInteraction(5, FakeUser(10)), pid, 'save'))
        _, _, g = cog._guild(5)
        bid = g['boards'][0]['id']
        inter = FakeInteraction(5, FakeUser(10))
        self.run_async(cog.handle_board_button(inter, bid, 'fix'))
        _, _, g = cog._guild(5)
        fix_id = [k for k, v in g['pending'].items() if v.get('replaces') == bid][0]
        self.run_async(cog.handle_board_button(FakeInteraction(5, FakeUser(10)), fix_id, 'k3'))
        self.run_async(cog.handle_board_button(FakeInteraction(5, FakeUser(10)), fix_id, 'k4'))
        self.run_async(cog.handle_board_button(FakeInteraction(5, FakeUser(10)), fix_id, 'save'))
        _, _, g = cog._guild(5)
        self.assertEqual(len(g['boards']), 1)
        self.assertEqual(g['boards'][0]['keys'], [4, 9])
        self.assertEqual(g['images'], 1)

    def test_button_templates_match_ids(self):
        import re
        tpl = re.compile(cp.DeadShotBoardButton.template)
        for act in ['k1', 'k12', 'save', 'drop', 'fix']:
            self.assertTrue(tpl.fullmatch(f'ds:0123456789ab:{act}'), act)
        self.assertFalse(tpl.fullmatch('ds:0123456789ab:k13'))
        ntpl = re.compile(cp.DeadShotNextButton.template)
        self.assertTrue(ntpl.fullmatch('dsn:12'))
        self.assertFalse(ntpl.fullmatch('dsn:0'))
        self.assertEqual(len(cp.pending_view('0123456789ab', [1, 2]).children), 14)


class AccountTests(TempDataMixin, unittest.TestCase):
    def record(self, g, keys, account, n=1, by=1):
        for _ in range(n):
            cp.record_board(g, {'src': 'manual', 'account': account}, keys, by, found=keys)

    def test_master_and_account_tables(self):
        g = cp.ensure_guild_data({'guilds': {'1': legacy_v1_file()}}, '1')
        self.record(g, [1, 2], 'main', 6)
        self.record(g, [11, 12], 'alt', 4)
        self.assertEqual(cp.build_model(g)['boards'], 10)
        main = cp.build_model(g, 'main')
        self.assertEqual(main['boards'], 6)
        self.assertEqual(main['legacy_images'], 0)
        self.assertEqual(set(main['order'][-2:]), {1, 2})
        self.assertEqual(cp.build_model(g, 'alt')['boards'], 4)
        self.assertIn('account **main**', cp.build_summary([], 0, 0, 0, 0, [], [], False, False, model=main))
        self.assertIn('all accounts', cp.build_summary([], 0, 0, 0, 0, [], [], False, False,
                                                      model=cp.build_model(g)))

    def test_untagged_boards_are_their_own_group(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        self.record(g, [3, 4], None, 2)
        self.record(g, [5, 6], 'alt', 1)
        self.assertEqual(cp.account_names(g), ['untagged', 'alt'])
        self.assertEqual(cp.build_model(g, 'untagged')['boards'], 2)

    def test_clean_account(self):
        self.assertEqual(cp.clean_account('  Main  Farm '), 'main farm')
        self.assertIsNone(cp.clean_account(''))
        self.assertIsNone(cp.clean_account('x' * 30))
        self.assertIsNone(cp.clean_account('bad@name'))

    def test_compare_detects_real_difference(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        rng = random.Random(4)
        for _ in range(40):
            self.record(g, sorted(rng.sample([1, 2, 3], 2)), 'a')
            self.record(g, sorted(rng.sample([10, 11, 12], 2)), 'b')
        result = cp.compare_accounts(g)
        self.assertEqual(result['tested'], 2)
        self.assertLess(result['p_value'], 0.01)
        self.assertIn('accounts clearly differ', cp.compare_text(result))

    def test_compare_same_random_source_not_flagged(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        rng = random.Random(9)
        for _ in range(80):
            self.record(g, rng.sample(cp.POSITIONS, 2), 'a')
            self.record(g, rng.sample(cp.POSITIONS, 2), 'b')
        result = cp.compare_accounts(g)
        self.assertGreater(result['p_value'], 0.01)

    def test_compare_needs_enough_boards(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        self.record(g, [1, 2], 'a', 6)
        self.record(g, [3, 4], 'b', 2)
        result = cp.compare_accounts(g)
        self.assertIsNone(result['p_value'])
        self.assertIn('needs at least two accounts', cp.compare_text(result))
        self.assertLess(len(cp.compare_text(result)), 2000)

    def test_fix_keeps_account(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        board = cp.record_board(g, {'src': 'manual', 'account': 'alt'}, [1, 2], 1)
        cp.record_board(g, {'replaces': board['id']}, [3, 4], 1)
        self.assertEqual(g['boards'][0]['account'], 'alt')
        self.assertIn('account **alt**', cp.saved_text(g['boards'][0], cp.build_model(g)))


class TimeTests(TempDataMixin, unittest.TestCase):
    def make(self, n, keys_fn, start=None, step=None, account='main'):
        from datetime import datetime, timedelta, timezone
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        t = start or datetime(2026, 10, 31, 0, 0, 0, tzinfo=timezone.utc)
        rng = random.Random(11)
        prev = None
        for i in range(n):
            t = t + (step(i, rng) if step else timedelta(seconds=rng.randint(40, 400)))
            keys = keys_fn(i, t, rng, prev)
            b = cp.record_board(g, {'src': 'manual', 'account': account}, keys, 1, found=keys)
            b['t'] = t.isoformat()
            prev = sorted(keys)
        return g

    def test_needs_enough_boards(self):
        g = self.make(10, lambda i, t, r, p: r.sample(cp.POSITIONS, 2))
        text = cp.time_text(cp.time_analysis(g))
        self.assertIn('Needs 30', text)

    def test_random_boards_show_no_link(self):
        g = self.make(200, lambda i, t, r, p: r.sample(cp.POSITIONS, 2))
        res = cp.time_analysis(g)
        self.assertGreaterEqual(len(res['tests']), 6)
        self.assertTrue(all(t['p_adj'] >= 0.01 for t in res['tests']))
        self.assertIn('Repeats spots', cp.time_text(res))
        self.assertLess(len(cp.time_text(res)), 2000)

    def test_clock_seeded_layout_is_caught(self):
        def by_minute(i, t, r, p):
            block = t.minute // 15
            return [[1, 2], [5, 6], [9, 10], [3, 12]][block]
        g = self.make(80, by_minute)
        res = cp.time_analysis(g)
        minute = [t for t in res['tests'] if t['name'].startswith('Minute')][0]
        self.assertLess(minute['p_adj'], 0.01)
        self.assertIn('LINK FOUND', cp.time_text(res))

    def test_attempt_sequence_is_caught(self):
        def alternate(i, t, r, p):
            return [1, 2] if i % 2 else [11, 12]
        g = self.make(60, alternate)
        res = cp.time_analysis(g)
        parity = [t for t in res['tests'] if 'odd/even' in t['name'] and 'Attempt' in t['name']][0]
        self.assertLess(parity['p_adj'], 0.01)

    def test_repeat_of_previous_board_is_caught(self):
        def sticky(i, t, r, p):
            return p if p and r.random() < 0.7 else r.sample(cp.POSITIONS, 2)
        g = self.make(80, sticky)
        seq = cp.time_analysis(g)['sequence']
        self.assertGreater(seq['mean'], 0.33)
        self.assertLess(seq['p_adj'], 0.01)

    def test_export_has_every_board_and_brisbane_time(self):
        import csv as _csv, io as _io
        from datetime import datetime, timezone
        g = self.make(3, lambda i, t, r, p: [4, 11],
                      start=datetime(2026, 10, 31, 1, 0, 0, tzinfo=timezone.utc))
        rows = list(_csv.reader(_io.StringIO(cp.export_csv(g))))
        self.assertEqual(rows[0][:6], ['board_id', 'account', 'logged_utc', 'logged_brisbane',
                                      'attempt_on_account', 'key_a'])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1][4], '1')
        self.assertEqual(rows[3][4], '3')
        self.assertTrue(rows[1][3].startswith('2026-10-31 11:'))
        self.assertEqual(rows[1][7:9], ['4', '11'])


class RecencyTests(TempDataMixin, unittest.TestCase):
    def board(self, g, keys, account, minute):
        b = cp.record_board(g, {'src': 'manual', 'account': account}, keys, 1, found=keys)
        b['t'] = f'2026-10-31T01:{minute:02d}:00+00:00'
        return b

    def test_recent_partner_shot_first_after_first_key(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        self.board(g, [5, 9], 'spy', 1)
        self.board(g, [2, 5], 'spy', 2)
        m = cp.play_model(g, 'spy')
        odds, recent = cp.second_key_odds(m, 5)
        self.assertEqual(recent, 2)
        order = sorted(odds, key=lambda p: (odds[p], p))
        self.assertEqual(set(order[:2]), {2, 9})
        self.assertAlmostEqual(sum(odds.values()), 1.0)
        text = cp.second_key_text(m, 5)
        self.assertIn('Account **spy**', text)

    def test_other_account_history_not_used(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        self.board(g, [5, 9], 'red', 1)
        m = cp.play_model(g, 'spy')
        self.assertNotIn('pair_weight', m)
        self.assertIn('Set your account', cp.build_summary([], 0, 0, 0, 0, [], [], False, False,
                                                          model=cp.play_model(g, None)))

    def test_window_limits_history(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        for i in range(cp.RECENT_WINDOW + 5):
            self.board(g, [1, 2] if i == 0 else [3 + i % 9, 12], 'a', i % 60)
        # Sort by time only matters within an hour here; use the helper directly.
        recent = cp.recent_pairs(g, 'a')
        self.assertEqual(len(recent), cp.RECENT_WINDOW)

    def test_new_account_message(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        m = cp.play_model(g, 'main')
        text = cp.build_summary([], 0, 0, 0, 0, [], [], False, False, model=m)
        self.assertIn('Account **main** has no boards yet', text)
        self.assertNotIn('Set your account', text)
        self.assertNotIn('pair_weight', m)

    def test_member_history_crosses_accounts(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        self.board(g, [5, 9], 'spy', 1)          # logged by member 1
        m = cp.play_model(g, 'main', member=1)   # brand-new account, same member
        self.assertEqual(m['recent_scope'], 'all')
        odds, recent = cp.second_key_odds(m, 5)
        self.assertEqual(recent, 1)
        self.assertEqual(min(odds, key=odds.get), 9)
        self.assertIn('across all your accounts', cp.build_summary(
            [], 0, 0, 0, 0, [], [], False, False, model=m))
        self.assertIn('(all accounts)', cp.second_key_text(m, 5))

    def test_other_members_boards_not_used(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        self.board(g, [5, 9], 'spy', 1)
        m = cp.play_model(g, None, member=2)
        self.assertNotIn('pair_weight', m)
        self.assertIn('You have no boards yet', cp.build_summary(
            [], 0, 0, 0, 0, [], [], False, False, model=m))

    def test_member_window_is_time_ordered(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        self.board(g, [1, 2], 'a', 30)
        self.board(g, [3, 4], 'b', 10)
        self.assertEqual(cp.member_recent_pairs(g, 1), [(3, 4), (1, 2)])
        self.assertEqual(cp.member_recent_pairs(g, 1, window=1), [(1, 2)])

    def test_probabilities_sum_to_two_keys(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        self.board(g, [3, 4], 'a', 1)
        m = cp.play_model(g, 'a')
        self.assertAlmostEqual(sum(m['prob'].values()), 2.0)
        self.assertEqual(m['order'][:2], [3, 4])
        self.assertIn("account **a**'s last 1 boards", cp.build_summary(
            [], 0, 0, 0, 0, [], [], False, False, model=m))


class ShotPathTests(TempDataMixin, unittest.TestCase):
    def test_rebuild_path_both_phases(self):
        plan = {'order': [10, 3, 4, 6, 1, 2, 7, 9, 12, 5, 8, 11], 'first': 6,
                'next': [9, 1, 2, 7, 12, 5, 8, 11]}
        shots, ok = cp.shots_from_plan(plan, [6, 2])
        self.assertTrue(ok)
        self.assertEqual(shots, [10, 3, 4, 6, 9, 1, 2])

    def test_rebuild_without_first_key_step(self):
        plan = {'order': [10, 3, 4, 6, 1, 2], 'first': None, 'next': None}
        self.assertEqual(cp.shots_from_plan(plan, [4, 1]), ([10, 3, 4, 6, 1], True))

    def test_path_not_followed(self):
        plan = {'order': [10, 3, 4, 6, 1, 2], 'first': None, 'next': None}
        self.assertEqual(cp.shots_from_plan(plan, [4, 3]), (None, False))
        self.assertEqual(cp.shots_from_plan(None, [4, 3]), (None, None))
        self.assertEqual(cp.shots_from_plan(plan, None), (None, None))

    def test_next_plan_for_other_first_key_ignored(self):
        plan = {'order': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], 'first': 9, 'next': [12, 11, 10]}
        self.assertEqual(cp.shots_from_plan(plan, [2, 5])[0], [1, 2, 3, 4, 5])


class OrderTestReportTests(TempDataMixin, unittest.TestCase):
    def run_arm(self, g, arm, keys_fn, n, rng):
        for _ in range(n):
            order = cp.TEST_ORDERS[arm]
            i, j = keys_fn(rng)
            found = [order[i], order[j]]
            b = cp.record_board(g, {'src': 'manual', 'by': '1'}, found, 1, found=found)
            b['test'] = arm
            b['shots'] = order[:j + 1]
            b['followed_path'] = True

    def test_keys_decided_on_shot_detected(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        rng = random.Random(1)
        early = lambda r: sorted(r.sample(range(6), 2))      # keys always in first 6 shots
        self.run_arm(g, 'fwd', early, 30, rng)
        self.run_arm(g, 'rev', early, 30, rng)
        res = cp.test_report(g, 1)
        self.assertLess(res['p'], 0.01)
        self.assertIn('Keys follow your shooting order', cp.test_text(res, False, 60))
        self.assertFalse(cp.test_active(g, 1))

    def test_placed_at_start_not_flagged(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        rng = random.Random(2)
        anywhere = lambda r: sorted(r.sample(range(12), 2))
        self.run_arm(g, 'fwd', anywhere, 20, rng)
        self.run_arm(g, 'rev', anywhere, 20, rng)
        res = cp.test_report(g, 1)
        self.assertGreater(res['p'], 0.01)
        self.assertLess(len(cp.test_text(res, False, 60)), 2000)


class AlgoReportTests(TempDataMixin, unittest.TestCase):
    def test_hazard_table(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        for i in range(20):
            first, second = (1, 2) if i % 2 else (3, 7)
            b = cp.record_board(g, {'src': 'manual', 'by': '1'}, [first, second], 1, found=[first, second])
            b['shots'] = list(range(1, second + 1))
            b['followed_path'] = True
        res = cp.algo_report(g)
        self.assertEqual(res['n'], 20)
        self.assertEqual(res['hazard'][0][:3], (1, 10, 20))
        self.assertAlmostEqual(res['rewards'], (0 + 5) / 2)
        text = cp.algo_text(res)
        self.assertIn('shot  1:  10/20', text)
        self.assertLess(len(text), 2000)
        self.assertIn('No stages', cp.algo_text(cp.algo_report(cp.ensure_guild_data({'guilds': {}}, '2'))))



class AnnounceTests(TempDataMixin, unittest.TestCase):
    def test_announces_once_per_version(self):
        sent = []

        class Chan:
            async def send(self, content, allowed_mentions=None):
                sent.append(content)

        class Settings:
            def feature_channels(self, gid, feature):
                return [111] if gid == 5 else []

        bot = FakeBot()
        bot.settings = Settings()
        bot.guilds = [types.SimpleNamespace(id=5), types.SimpleNamespace(id=6)]
        bot.get_channel = lambda cid: Chan() if cid == 111 else None
        asyncio.run(cp.ChestPatternCog(bot).announce_release())
        asyncio.run(cp.ChestPatternCog(bot).announce_release())   # restart: no repeat
        self.assertEqual(len(sent), 1)
        self.assertIn(f'v{cp.__version__} is live', sent[0])

    def test_announces_in_last_ds_channel(self):
        sent = []

        class Chan:
            async def send(self, content, allowed_mentions=None):
                sent.append(content)

        class Settings:
            def feature_channels(self, gid, feature):
                return []

        ctx = FakeCtx(10)
        ctx.channel = types.SimpleNamespace(id=222)
        asyncio.run(cp.ChestPatternCog(FakeBot()).ds_prefix(ctx, 'help'))
        bot = FakeBot()
        bot.settings = Settings()
        bot.guilds = [types.SimpleNamespace(id=5)]
        bot.get_channel = lambda cid: Chan() if cid == 222 else None
        asyncio.run(cp.ChestPatternCog(bot).announce_release())
        self.assertEqual(len(sent), 1)


class FakeCtx:
    def __init__(self, uid, guild_id=5):
        self.author = types.SimpleNamespace(id=uid)
        self.guild = types.SimpleNamespace(id=guild_id)
        self.sent = []

    async def send(self, content, view=None, allowed_mentions=None, file=None):
        self.sent.append(content)
        self.file = file


@unittest.skipIf('discord' in sys.modules and hasattr(sys.modules['discord'], '__version__'),
                 'prefix callbacks are wrapped by real discord.py')
class PrefixTests(TempDataMixin, unittest.TestCase):
    def ds(self, uid, *words):
        ctx = FakeCtx(uid)
        asyncio.run(cp.ChestPatternCog(FakeBot()).ds_prefix(ctx, *words))
        return ctx.sent[-1]

    def test_account_flow_survives_restart(self):
        self.assertIn('not set', self.ds(10, 'account'))
        self.ds(10, '3', '9')
        self.assertIn('1 older board', self.ds(10, 'account', 'Main'))
        self.assertIn('Tagged 1', self.ds(10, 'tag', 'main'))
        self.ds(10, 'account', 'alt', 'one')
        self.assertIn('account **alt one**', self.ds(10, '11', '4'))
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        self.assertEqual([b['account'] for b in g['boards']], ['main', 'alt one'])
        self.assertEqual(g['boards'][1]['found'], [11, 4])
        self.assertIn('account **alt one**', self.ds(10, 'mine'))
        self.assertIn('side by side', self.ds(10, 'compare'))
        # Another member's account is separate.
        self.assertIn('not set', self.ds(20, 'account'))
        self.assertIn('Set your account first', self.ds(20, 'mine'))

    def test_time_and_export_commands(self):
        self.ds(10, 'account', 'main')
        self.ds(10, '11', '4')
        self.assertIn('Needs 30', self.ds(10, 'time'))
        self.assertIn('account **main**', self.ds(10, 'time', 'mine'))
        self.assertIn('Set your account first', self.ds(20, 'time', 'mine'))
        ctx = FakeCtx(10)
        asyncio.run(cp.ChestPatternCog(FakeBot()).ds_prefix(ctx, 'export'))
        self.assertIn('All `1` logged', ctx.sent[-1])
        self.assertEqual(ctx.file.filename, 'dead_shot_boards.csv')

    def test_shot_path_recorded_across_restarts(self):
        self.ds(10, 'account', 'main')
        msg = self.ds(10)                         # random order, stored
        self.assertIn('Next stage — shoot in this order', msg)
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        shown = g['plans']['10']['order']
        self.assertEqual(sorted(shown), list(range(1, 13)))
        self.ds(10)                               # asking again keeps the same order
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        self.assertEqual(g['plans']['10']['order'], shown)
        first, second = shown[3], shown[8]
        self.assertIn('Keep going in the same order', self.ds(10, str(first)))
        reply = self.ds(10, str(first), str(second))
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        board = g['boards'][-1]
        self.assertEqual(board['shots'], shown[:9])
        self.assertTrue(board['followed_path'])
        self.assertIn('Shots recorded:', reply)
        self.assertIn('Next stage', reply)
        rows = cp.export_csv(g).splitlines()
        self.assertIn('shots_in_order', rows[0])
        self.assertIn(' '.join(str(x) for x in shown[:9]), rows[-1])
        algo = self.ds(10, 'algo')
        self.assertIn('1 stages with a full shot path', algo)
        self.assertIn('Rewards per stage **7.00**', algo)

    def test_unfollowed_path_still_saves_keys(self):
        self.ds(10)
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        shown = g['plans']['10']['order']
        reply = self.ds(10, str(shown[5]), str(shown[1]))
        self.assertIn('Shots not recorded', reply)
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        self.assertEqual(len(g['boards']), 1)
        self.assertFalse(g['boards'][0]['followed_path'])

    def test_order_test_alternates_and_records(self):
        self.ds(10, 'test', 'on')
        msg = self.ds(10)
        self.assertIn('Test stage 1/60', msg)
        self.assertIn('1 → 2 → 3', msg)
        self.assertIn('Keep going in the same order', self.ds(10, '4'))
        reply = self.ds(10, '4', '9')               # 1->12: keys at shots 4 and 9
        self.assertIn('Shots recorded: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9', reply)
        self.assertIn('Test stage 2/60', reply)
        self.assertIn('12 → 11 → 10', reply)
        reply = self.ds(10, '11', '7')              # 12->1: keys at shots 2 and 6
        self.assertIn('Shots recorded: 12 → 11 → 10 → 9 → 8 → 7', reply)
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        self.assertEqual([b['test'] for b in g['boards']], ['fwd', 'rev'])
        self.assertIn('Needs at least 5', self.ds(10, 'test'))
        self.assertIn('test **off**', self.ds(10, 'test', 'off'))
        self.assertIn('Next stage — shoot in this order', self.ds(10))
        self.assertIn('test **on**', self.ds(10, 'test', 'on'))
        self.assertIn('Test stage 3/60', self.ds(10))

    def test_safest_targets_first(self):
        self.ds(10)
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        self.assertEqual(g['plans']['10']['order'], list(range(1, 13)))   # no data: ties by number
        # Target 1 keeps being the key on shot 1; others are safe there.
        cog = cp.ChestPatternCog(FakeBot())
        key, data, g = cog._guild(5)
        for i in range(12):
            if i % 2 == 0:      # target 1 shot first: it's a key
                shots, keys = [1, 2, 3, 12], [1, 12]
            else:               # target 2 shot first: safe
                shots, keys = [2, 1, 3, 12], [3, 12]
            b = cp.record_board(g, {'src': 'manual', 'by': '10'}, keys, 10, found=keys)
            b['shots'] = shots
            b['followed_path'] = True
        cog._save(key, data)
        reply = self.ds(10, '5', '6')
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        order = g['plans']['10']['order']
        self.assertEqual(sorted(order), list(range(1, 13)))
        risk, n = cp.target_risk(g)
        self.assertGreater(risk[1], 1.0)
        self.assertLess(risk[2], 1.0)
        self.assertLess(order.index(2), order.index(1))
        self.assertIn('Next stage', reply)
        self.assertIn('Safest → riskiest targets', self.ds(10, 'algo'))

    def test_old_mode_plan_not_reused(self):
        cog = cp.ChestPatternCog(FakeBot())
        key, data, g = cog._guild(5)
        g['plans']['10'] = {'order': list(range(12, 0, -1)), 'fixed': True, 'first': None, 'next': None}
        cog._save(key, data)
        msg = self.ds(10)
        self.assertNotIn('12 → 11 → 10', msg)
        self.assertIn('**1 → 2 → 3', msg)

    def test_full_clear_flagged_and_listed(self):
        self.assertIn('No confirmed full clears', self.ds(10, 'wins'))
        self.ds(10)
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        shown = g['plans']['10']['order']
        reply = self.ds(10, str(shown[4]), str(shown[11]))
        self.assertIn('10 rewards?', reply)
        self.assertIn('big stage(s) to check', self.ds(10, 'wins'))
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        bid = g['boards'][-1]['id']
        inter = FakeInteraction(5, FakeUser(10))
        asyncio.run(cp.ChestPatternCog(FakeBot()).handle_confirm_button(inter, bid, 'y'))
        self.assertIn('FULL CLEAR', inter.response.edits[-1][0])
        wins = self.ds(10, 'wins')
        self.assertIn('full clears** (1)', wins)
        self.assertIn(' → '.join(str(p) for p in shown), wins)

    def test_custom_path_recorded(self):
        self.assertIn('Give all 12', self.ds(10, 'path', '1', '2'))
        self.assertIn('Give all 12', self.ds(10, 'path', *['1'] * 12))
        path = ['10', '3', '9', '4', '6', '7', '8', '12', '11', '5', '1', '2']
        msg = self.ds(10, 'path', *path)
        self.assertIn('**10 → 3 → 9', msg)
        self.assertIn('**10 → 3 → 9', self.ds(10))        # !ds keeps it until logged
        self.assertIn('Keep going in the same order: 11 → 5', self.ds(10, '12'))
        reply = self.ds(10, '12', '2')
        self.assertIn('10 rewards?', reply)
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        self.assertEqual(g['boards'][-1]['shots'], [int(x) for x in path])

    def test_reject_big_stage_and_review(self):
        self.ds(10, 'path', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12')
        self.ds(10, '3', '12')                     # bot assumes 10 rewards
        msg = self.ds(10, 'review')
        self.assertIn('recorded as **10 rewards**', msg)
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        bid = g['boards'][-1]['id']
        stranger = FakeInteraction(5, FakeUser(99))
        asyncio.run(cp.ChestPatternCog(FakeBot()).handle_confirm_button(stranger, bid, 'n'))
        self.assertIn('Only the person', stranger.response.sent[-1])
        inter = FakeInteraction(5, FakeUser(10))
        asyncio.run(cp.ChestPatternCog(FakeBot()).handle_confirm_button(inter, bid, 'n'))
        self.assertIn('Path removed', inter.response.edits[-1][0])
        _, _, g = cp.ChestPatternCog(FakeBot())._guild(5)
        b = g['boards'][-1]
        self.assertIsNone(b['shots'])
        self.assertEqual(b['keys'], [3, 12])
        self.assertIn('Nothing to check', self.ds(10, 'review'))
        self.assertIn('No confirmed full clears', self.ds(10, 'wins'))
        import re
        self.assertTrue(re.fullmatch(cp.DeadShotConfirmButton.template, f'dsc:{bid}:y'))

    def test_bad_account_name(self):
        self.assertIn('Account names', self.ds(10, 'account', 'untagged'))
        self.assertIn('Use `!ds tag', self.ds(10, 'tag'))


@unittest.skipUnless(HAVE_CV, 'OpenCV/numpy not installed')
class ImageTests(TempDataMixin, unittest.TestCase):
    W, H = 720, 1560

    def board_image(self, keys, seed=0):
        """Synthetic screenshot: grey cells = Point Boost, gold = upgrade,
        partial gold = key, red = unopened target."""
        rng = np.random.default_rng(seed)
        img = np.full((self.H, self.W, 3), 40, np.uint8)
        img += rng.integers(0, 6, img.shape, dtype=np.uint8)
        half = int(self.W * cp.CROP_SIZE_REL) // 2
        for r in range(3):
            for c in range(4):
                p = r * 4 + c + 1
                cx, cy = int(self.W * cp.CELL_X[c]), int(self.H * cp.CELL_Y[r])
                x1, y1 = cx - half, cy - half
                size = 2 * half
                if p in keys:
                    img[y1:y1 + size, x1:x1 + size] = (150, 150, 150)
                    band = int(size * 0.14)
                    img[y1:y1 + band, x1:x1 + size] = (0, 200, 240)
                elif p % 3 == 0:
                    img[y1:y1 + size, x1:x1 + size] = (0, 200, 240)
                elif p % 3 == 1:
                    img[y1:y1 + size, x1:x1 + size] = (30, 30, 220)
                else:
                    img[y1:y1 + size, x1:x1 + size] = (150, 150, 150)
        return img

    def test_colour_read_finds_keys(self):
        read = cp.detect_board(cv2, np, self.board_image([2, 11]), [])
        self.assertEqual(read['keys'], [2, 11])
        self.assertEqual(read['method'], 'colour')
        self.assertFalse(read['confident'])

    def test_learned_reader_takes_over_and_autosaves(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        rng = random.Random(1)
        for i in range(5):
            keys = sorted(rng.sample([p for p in cp.POSITIONS], 2))
            read = cp.detect_board(cv2, np, self.board_image(keys, seed=i), g['samples'])
            cp.record_board(g, {'src': 'image', 'symbols': read['symbols'], 'feats': read['feats']},
                            keys, 1)
        self.assertTrue(cp.learned_ready(g['samples']))
        read = cp.detect_board(cv2, np, self.board_image([4, 7], seed=99), g['samples'])
        self.assertEqual(read['method'], 'learned')
        self.assertEqual(read['keys'], [4, 7])
        self.assertTrue(read['confident'])

    def test_rescreenshot_is_duplicate(self):
        img = self.board_image([1, 6])
        a = cp.board_dhash(cv2, img)
        b = cp.board_dhash(cv2, cv2.resize(img, (540, 1170)))
        self.assertLessEqual(cp.hamming_hex(a, b), 3)

    def test_channel_flow_end_to_end(self):
        cog = cp.ChestPatternCog(FakeBot())
        ok, raw = cv2.imencode('.png', self.board_image([3, 10]))

        class Att:
            filename, size = 'board.png', len(raw)

            async def read(self):
                return raw.tobytes()

        (results, model), err = asyncio.run(cog._process_attachments(7, 10, [Att(), Att()]))
        self.assertIsNone(err)
        self.assertEqual(results[0][0], 'pending')
        pid = results[0][1]['id']
        asyncio.run(cog.handle_board_button(FakeInteraction(7, FakeUser(10)), pid, 'save'))
        (results, _), _ = asyncio.run(cog._process_attachments(7, 10, [Att()]))
        self.assertEqual(results[0][0], 'dup')
        _, _, g = cog._guild(7)
        self.assertEqual(len(g['boards']), 1)


if __name__ == '__main__':
    unittest.main()
