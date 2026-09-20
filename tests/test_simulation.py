import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from gkshot.adapters.gktools import GKToolsAdapter
from gkshot.adapters.local_engine import LocalSimulator, SimulationError, SimulationCancelled
from gkshot.catalog import Catalog
from gkshot.models import Loadout
from gkshot.simulation import compare, compose_memories, read_ready_memory, summary


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.loadout = Loadout.load(ROOT / 'tests/fixtures/example-url.loadout.json')
        self.simulator = LocalSimulator()

    def reference(self, loadout, trials=20, seed=610397104):
        runtime = ROOT / 'runtime/node'
        result = subprocess.run([self.simulator.node, '--no-warnings', '--loader',
            (runtime / 'upstream/scripts/extensionless-loader.mjs').as_uri(), str(runtime / 'reference.mjs')],
            input=json.dumps({'query': urlsplit(GKToolsAdapter().to_url(loadout)).query,
                              'trials': trials, 'seed': seed}), text=True, encoding='utf-8',
            capture_output=True, check=True, timeout=30)
        return json.loads(result.stdout)

    def test_reference_parity_all_input_fields(self):
        original = self.loadout.to_dict()
        variants = [original]
        for field in ('parameters', 'support_bonus', 'item_ids', 'card_ids', 'customizations', 'stage_id'):
            data = copy.deepcopy(original)
            if field == 'parameters':
                data[field] = {'vocal': 1101, 'dance': 2419, 'visual': 1968, 'stamina': 47}
            elif field == 'support_bonus': data[field] = 0.10
            elif field == 'item_ids': data[field] = [0, 0, 0, 0]
            elif field == 'card_ids': data[field][0][3] = 0
            elif field == 'customizations': data[field][1][3] = '31x1e39x1'
            elif field == 'stage_id':
                data[field] = next(s['id'] for s in Catalog().stages.values()
                    if s['type'] == 'contest' and s['plan'] in ('sense', 'free') and s['id'] != 160)
            variants.append(data)
        for data in variants:
            with self.subTest(data=data):
                loadout = Loadout.from_dict(data)
                actual = self.simulator.simulate(loadout, trials=20)
                reference = self.reference(loadout)
                self.assertEqual(actual['scores'], reference['scores'])
                for field in ('idol', 'typeMultipliers', 'defaultCardIds'):
                    self.assertEqual(actual['effective'][field], reference[field])

    def test_reproducible_and_varied_seed(self):
        a = self.simulator.simulate(self.loadout, trials=100)
        b = self.simulator.simulate(self.loadout, trials=100)
        c = self.simulator.simulate(self.loadout, trials=100, seed=9)
        self.assertEqual(a['scores'], b['scores'])
        self.assertNotEqual(a['scores'], c['scores'])
        self.assertEqual(a['scores'][:3], [94042, 24119, 85058])
        self.assertEqual(summary(a['scores'])['mean'], 35358.2)
        self.assertEqual(a['scores'], self.reference(self.loadout, trials=100)['scores'])

    def test_effective_duplicate_and_customization_mapping(self):
        result = self.simulator.simulate(self.loadout, trials=1)['effective']
        self.assertEqual(result['idol']['dupeIndices'], [6, 2])
        self.assertIn({'id': 117, 'customizations': {'31': 1}}, result['idol']['cards'])
        self.assertEqual(result['idol']['pItemIds'], [293])
        self.assertEqual(result['idol']['params'], self.loadout.to_dict()['parameters'])

    def test_same_character_different_version_signature_is_allowed(self):
        data = self.loadout.to_dict()
        idols = json.loads((ROOT / 'runtime/node/upstream/packages/gakumas-data/json/p_idols.json').read_text(encoding='utf-8'))
        versions = {p['id'] for p in idols if p['idolId'] == 7 and p['plan'] == 'sense' and p['id'] != 97}
        alternative = next(c['id'] for c in Catalog().skill_cards.values() if c['pIdolId'] in versions)
        data['card_ids'][1][0] = alternative
        loadout = Loadout.from_dict(data)
        self.assertEqual(self.simulator.simulate(loadout, trials=5)['scores'], self.reference(loadout, trials=5)['scores'])

    def test_corrupt_runtime_is_not_used(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'upstream').mkdir()
            (root / 'upstream/data.js').write_text('changed')
            (root / 'manifest.json').write_text(json.dumps({'revision': 'test', 'files': {'data.js': 'incorrect'}}))
            with self.assertRaisesRegex(SimulationError, '校验失败'):
                LocalSimulator(runtime=root).identity()

    def test_node_crash_becomes_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'upstream/scripts').mkdir(parents=True)
            (root / 'upstream/scripts/extensionless-loader.mjs').write_text('')
            (root / 'manifest.json').write_text(json.dumps({'revision': 'test', 'files': {}}))
            (root / 'worker.mjs').write_text("console.error('deliberate test crash'); process.exit(2);")
            with self.assertRaisesRegex(SimulationError, '意外退出'):
                LocalSimulator(runtime=root).simulate(self.loadout, trials=1)

    def test_unknowns_invalid_ids_and_customizations_rejected(self):
        for field, value in [('stage_id', 999999), ('item_ids', [999999, 0, 0, 0])]:
            data = self.loadout.to_dict()
            data[field] = value
            with self.assertRaises(SimulationError): self.simulator.simulate(Loadout.from_dict(data), trials=1)
        for token in (None, '99999x1', '31x999', '31x1e31x1'):
            data = self.loadout.to_dict()
            data['customizations'][1][3] = token
            with self.assertRaises((ValueError, SimulationError)):
                self.simulator.simulate(Loadout.from_dict(data), trials=1)

    def test_timeout(self):
        with self.assertRaisesRegex(SimulationError, '超过'):
            self.simulator.simulate(self.loadout, trials=100000, timeout=0.01)

    def test_cancel_while_running(self):
        cancel = threading.Event()
        with self.assertRaises(SimulationCancelled):
            self.simulator.simulate(self.loadout, trials=100000, cancel=cancel,
                progress=lambda *_: cancel.set())

    def test_cache_order_and_seed_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            first = compare([('a', self.loadout), ('b', self.loadout)], trials=3, cache=folder)
            self.assertFalse(first['results'][0]['cached'])
            self.assertTrue(first['results'][1]['cached'])
            self.assertEqual(first['ranking'], ['a', 'b'])
            other_seed = compare([('a', self.loadout)], trials=3, seed=8, cache=folder)
            self.assertFalse(other_seed['results'][0]['cached'])
            for path in Path(folder).glob('*.json'): path.write_text('{}')
            repaired = compare([('a', self.loadout)], trials=3, cache=folder)
            self.assertFalse(repaired['results'][0]['cached'])

    def test_failure_does_not_hide_valid_candidates(self):
        data = self.loadout.to_dict()
        data['item_ids'][0] = 999999
        report = compare([('bad', Loadout.from_dict(data)), ('good', self.loadout)], trials=1)
        self.assertEqual(report['status'], 'completed-with-errors')
        self.assertEqual(report['ranking'], ['good'])

    def test_cancel_retains_finished_candidates(self):
        cancel = threading.Event()
        report = compare([('a', self.loadout), ('b', self.loadout)], trials=1, cancel=cancel,
                         on_result=lambda _: cancel.set())
        self.assertEqual(report['status'], 'cancelled')
        self.assertEqual(report['ranking'], ['a'])

    def test_different_stages_rejected(self):
        data = self.loadout.to_dict()
        data['stage_id'] = 1
        with self.assertRaises(ValueError):
            compare([('a', self.loadout), ('b', Loadout.from_dict(data))])

    def test_summary_sample_deviation(self):
        self.assertEqual(summary([1, 3])['sample_stddev'], 2 ** 0.5)
        self.assertIsNone(summary([1])['sample_stddev'])

    def memory(self, group=0):
        data = self.loadout.to_dict()
        return {'parameters': data['parameters'], 'item_ids': data['item_ids'],
                'card_ids': data['card_ids'][group], 'customizations': data['customizations'][group], 'p_idol_id': 97}

    def test_memory_composition_floors_all_four_stats_and_main_items(self):
        main, sub = self.memory(), self.memory(1)
        sub['parameters'] = {'vocal': 1004, 'dance': 1009, 'visual': 1011, 'stamina': 49}
        sub['item_ids'] = [0, 0, 0, 0]
        result = compose_memories(main, sub, stage_id=160, support_bonus=0.0472)
        self.assertEqual(result.to_dict()['parameters'], {'vocal': 1831, 'dance': 3161, 'visual': 2143, 'stamina': 60})
        self.assertEqual(result.item_ids, (293, 0, 0, 0))
        self.assertEqual(self.simulator.simulate(result, trials=5)['scores'], self.reference(result, trials=5)['scores'])
        sub['customizations'][0] = None
        with self.assertRaises(ValueError): compose_memories(main, sub, stage_id=160, support_bonus=0.0472)

    def test_library_is_readonly_and_review_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'library.sqlite3'
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE memories(memory_id TEXT,status TEXT,report_json TEXT,review_json TEXT)')
                for status in ('reviewed', 'auto-resolved', 'needs-review', 'incomplete'):
                    db.execute('INSERT INTO memories VALUES(?,?,?,?)', (status, status,
                        json.dumps({'draft': self.memory()}), json.dumps({'draft': self.memory(1), 'confirmed': True})))
                db.commit()
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            self.assertEqual(read_ready_memory(folder, 'reviewed'), self.memory(1))
            self.assertEqual(read_ready_memory(folder, 'auto-resolved'), self.memory())
            for status in ('needs-review', 'incomplete', 'absent'):
                with self.assertRaises(ValueError): read_ready_memory(folder, status)
            self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())

    def test_missing_library_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, '只读打开'):
                read_ready_memory(folder, 'missing')
            self.assertFalse((Path(folder) / 'library.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
