# tests/test_chest_pattern.py
from __future__ import annotations

__version__ = '1.3.0'

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

    def test_probabilities_sum_to_two_keys(self):
        g = cp.ensure_guild_data({'guilds': {}}, '1')
        self.board(g, [3, 4], 'a', 1)
        m = cp.play_model(g, 'a')
        self.assertAlmostEqual(sum(m['prob'].values()), 2.0)
        self.assertEqual(m['order'][:2], [3, 4])
        self.assertIn("account **a**'s last 1 boards", cp.build_summary(
            [], 0, 0, 0, 0, [], [], False, False, model=m))


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
