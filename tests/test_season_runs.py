import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from gkshot.simulation_web import SimulationServer
from gkshot.adapters.local_engine import SimulationCancelled, SimulationError


class SeasonRunTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=SimulationServer(('127.0.0.1',0))
        self.server.search_root=Path(self.temp.name)/'searches'
        self.server.groups_root=Path(self.temp.name)/'groups'
        self.server.cache_root=Path(self.temp.name)/'cache'
        self.server.libraries={'test':Path(self.temp.name)}
        self.calls=[];self.gate=None;self.entered=threading.Event();self.fail_stage=None
        test=self
        class Engine:
            def identity(self):return {'revision':'season-v1'}
            def close(self):pass
            def simulate(self,loadout,*,trials,seed,cancel=None,progress=None,timeout=None):
                test.entered.set()
                if test.gate:
                    while not test.gate.wait(.01):
                        if cancel.is_set():raise SimulationCancelled('pause')
                if cancel.is_set():raise SimulationCancelled('pause')
                if loadout.stage_id==test.fail_stage:raise SimulationError('test stage failure')
                test.calls.append((loadout.stage_id,trials,seed))
                if progress:progress(trials,trials)
                return {'scores':list(range(1,trials+1)),'rng_calls':trials,'trials':trials,'seed':seed,'engine_revision':'season-v1'}
        self.server.simulator_factory=Engine
        self.loadout=json.loads((ROOT/'examples/simulation/original.loadout.json').read_text())
        def pairs(records,stage_id,support_bonus,catalog):
            l=copy.deepcopy(self.loadout);l.update(stage_id=stage_id,support_bonus=support_bonus)
            return [{'id':format(stage_id,'024x'),'idol_id':1,'idol_name':'test','main':{},'sub':{},'loadout':l}],[]
        self.patches=[patch('gkshot.simulation_web.memory_records',return_value=[{}]),patch('gkshot.simulation_web.enumerate_pairs',side_effect=pairs)]
        for p in self.patches:p.start()
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        self.config={'season':52,'library':'test','seed':42,'support_bonus':.0472}
    def tearDown(self):
        if self.gate:self.gate.set()
        self.server.shutdown()
        deadline=time.monotonic()+5
        while self.server.busy() and time.monotonic()<deadline:time.sleep(.01)
        self.server.server_close();self.thread.join()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def req(self,path,body=None,auth=True):
        c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            c.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,
                      {'X-GK-Token':self.server.token} if auth else {})
            r=c.getresponse();return r.status,json.loads(r.read())
        finally:c.close()
    def start(self):
        code,data=self.req('/api/season-runs/start',self.config)
        self.assertEqual(code,202,data);return data['group_id']
    def done(self,gid):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            code,g=self.req('/api/season-runs/'+gid)
            self.assertEqual(code,200)
            if g['state']!='running':return g
            time.sleep(.01)
        self.fail('group did not finish')
    def test_round_order_then_refine_and_live_rank_snapshots(self):
        gid=self.start();g=self.done(gid)
        self.assertEqual(g['state'],'complete',g)
        self.assertEqual([(s,t) for s,t,_ in self.calls],[(s,20) for _ in range(5) for s in (181,182,183)]+[(s,2000) for s in (181,182,183)])
        for child in g['stages']:
            _,d=self.req('/api/searches/'+child['search_id']+'?phase=screen')
            self.assertEqual(d['rows'][0]['summary']['trials'],100)
            self.assertEqual(d['state'],'complete')
        _,p=self.req('/api/season-progress?season=52&library=test&group='+gid)
        self.assertEqual([s['trials'] for s in p['stages']],[100]*3)
    def test_pause_restore_and_busy_owner_between_stages(self):
        self.gate=threading.Event();gid=self.start();self.assertTrue(self.entered.wait(3))
        self.assertEqual(self.req('/api/season-runs/start',self.config)[0],409)
        self.assertEqual(self.req('/api/search/start',dict(self.config,stage_id=181))[0],409)
        self.assertEqual(self.req('/api/run',{'candidates':[{'name':'x','loadout':self.loadout}],'trials':1,'seed':1})[0],409)
        # The group owns execution even when all child snapshots look idle.
        with self.server.guard:
            for j in self.server.searches.values():j['snapshot']['state']='round-wait'
            self.assertTrue(self.server.busy())
        self.assertEqual(self.req('/api/season-runs/pause',{'group_id':gid,'library':'wrong'})[0],403)
        self.assertEqual(self.req('/api/season-runs/pause',{'group_id':gid,'library':'test'})[0],200)
        g=self.done(gid);self.assertEqual(g['state'],'paused')
        ids=[c['search_id'] for c in g['stages']]
        self.server.groups.clear();self.server.searches.clear()
        self.assertEqual(self.req('/api/season-runs/'+gid)[1]['state'],'paused')
        self.assertEqual(self.req('/api/season-runs/resume',{'group_id':gid,'library':'wrong'})[0],403)
        self.gate.set()
        self.assertEqual(self.req('/api/season-runs/resume',{'group_id':gid,'library':'test'})[0],200)
        g=self.done(gid);self.assertEqual(g['state'],'complete',g)
        self.assertEqual([c['search_id'] for c in g['stages']],ids)
        self.assertEqual(len(list(self.server.search_root.iterdir())),3)
    def test_failed_stage_does_not_block_others_and_retry_reuses_scores(self):
        self.fail_stage=182;gid=self.start();g=self.done(gid)
        self.assertEqual(g['state'],'completed-with-errors',g)
        self.assertEqual([s['state'] for s in g['stages']],['complete','error','complete'])
        before=len(self.calls);self.fail_stage=None
        self.assertEqual(self.req('/api/season-runs/resume',{'group_id':gid,'library':'test'})[0],200)
        self.assertEqual(self.done(gid)['state'],'complete')
        self.assertEqual([s for s,_,_ in self.calls[before:]],[182]*6)
    def test_invalid_season_and_missing_stage_preflight_write_nothing(self):
        for config in [dict(self.config,season=True),dict(self.config,season=999),dict(self.config,stages=[181,182,True]),dict(self.config,support_bonus=-1)]:
            self.assertEqual(self.req('/api/season-runs/start',config)[0],400)
        with patch('gkshot.simulation_web.enumerate_pairs',return_value=([],[])):
            self.assertEqual(self.req('/api/season-runs/start',self.config)[0],400)
        self.assertFalse(self.server.search_root.exists())
        self.assertEqual(self.req('/api/season-runs',auth=False)[0],403)

if __name__=='__main__':unittest.main()
