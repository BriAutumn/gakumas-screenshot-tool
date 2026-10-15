import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gkshot.models import Loadout
from gkshot.app_paths import RESOURCE_ROOT
from gkshot.adapters.local_engine import LocalSimulator, SimulationError
from gkshot.catalog import Catalog
from gkshot.simulation_search import pinned_idols


class ExplicitIdentityTests(unittest.TestCase):
    def test_legacy_schema_remains_unchanged(self):
        loadout=Loadout.load(RESOURCE_ROOT/'examples/simulation/original.loadout.json')
        self.assertNotIn('p_idol_id', loadout.to_dict())

    def test_free_only_pool_uses_explicit_idol(self):
        catalog=Catalog(); idols=pinned_idols(catalog)
        idol=next(p for p in idols.values() if p['plan']=='sense')
        stage=next(s for s in catalog.stages.values() if s['plan']=='sense' and s['type']=='contest')
        card=next(c for c in catalog.skill_cards.values() if c['plan']=='free' and not c['pIdolId'])
        value=Loadout.load(RESOURCE_ROOT/'examples/simulation/original.loadout.json').to_dict()
        value.update(stage_id=stage['id'],p_idol_id=idol['id'],item_ids=[0]*4,
                     card_ids=[[card['id'],0,0,0,0,0],[0]*6],customizations=[['']*6,['']*6])
        with LocalSimulator(persistent=True) as engine:
            result=engine.simulate(Loadout.from_dict(value),trials=2)
            self.assertEqual(result['effective']['idol']['pIdolId'],idol['id'])
            self.assertEqual(result['effective']['idol']['plan'],'sense')
            del value['p_idol_id']
            with self.assertRaises(SimulationError): engine.simulate(Loadout.from_dict(value),trials=1)

    def test_invalid_explicit_id_not_inferred_from_other_entities(self):
        value=Loadout.load(RESOURCE_ROOT/'examples/simulation/original.loadout.json').to_dict()
        value['p_idol_id']=999999
        with LocalSimulator() as engine:
            with self.assertRaises(SimulationError): engine.simulate(Loadout.from_dict(value),trials=1)
