import base64
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from gkshot.adapters.local_engine import LocalSimulator, SimulationCancelled
from gkshot.models import Loadout
from gkshot.simulation import atomic_json, summary
from gkshot.simulation_search import POLICY_BALANCED, balanced_finalists, upgrade_screening_policy, POLICY_ADAPTIVE, adaptive_finalists, memory_records, memory_preview, enumerate_pairs, select_finalists, run_search, ranking


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.loadout = Loadout.load(ROOT / 'examples/simulation/original.loadout.json')

    def record(self, name, ordinal, group=0):
        l = self.loadout.to_dict()
        return {'id': name, 'ordinal': ordinal, 'identity': {'idol_name': '测试'}, 'draft': {
            'parameters': l['parameters'], 'item_ids': l['item_ids'], 'card_ids': l['card_ids'][group],
            'customizations': l['customizations'][group], 'p_idol_id': 97}}

    def test_exhaustive_ordered_pairs_and_unknown_rejection(self):
        a,b,c = self.record('a',12),self.record('b',35,1),self.record('c',100)
        bad = self.record('bad',101);bad['draft']['customizations'][1] = None
        rows, rejected = enumerate_pairs([a,b,c,bad], stage_id=160, support_bonus=.0472)
        self.assertEqual({(r['main']['id'],r['sub']['id']) for r in rows},
                         {('a','b'),('a','c'),('b','a'),('b','c'),('c','a'),('c','b')})
        self.assertEqual(len(rejected),1)
        self.assertEqual(rejected[0]['ordinal'],101)
        self.assertEqual(rows[0]['main']['ordinal'],12)
        self.assertEqual(enumerate_pairs([a,b], stage_id=161, support_bonus=0)[0],[])

    def test_provenance_duplicate_hash_different_owned_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); library=root/'processing/run/memory-library';library.mkdir(parents=True)
            entries=[{'capture_id':f'c{i}','ordinal':i,'images':[{'path':f'raw/c{i}/page.png','sha256':'same'}]} for i in (2,7)]
            atomic_json(root/'session.json',{'session_id':'session','entries':entries})
            report={'pages':[{'source_path':str(root/'raw/c7/page.png'),'sha256':'same'}], 'draft': self.record('a',7)['draft']}
            database=library/'library.sqlite3'
            db=sqlite3.connect(database)
            db.execute('CREATE TABLE memories(memory_id TEXT,capture_key TEXT,status TEXT,report_json TEXT,review_json TEXT,created_at TEXT)')
            db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?)',('a','key','auto-resolved',json.dumps(report),None,'now'));db.commit();db.close()
            before=hashlib.sha256(database.read_bytes()).hexdigest()
            self.assertEqual(memory_records(library)[0]['ordinal'],7)
            self.assertEqual(before,hashlib.sha256(database.read_bytes()).hexdigest())
            # Preview must use the same capture attempt, and reject changed source bytes.
            import io
            from PIL import Image
            stream=io.BytesIO();Image.new('RGB',(16,16)).save(stream,format='PNG');raw=stream.getvalue()
            image_path=root/'raw/c7/cover.png';image_path.parent.mkdir(parents=True);image_path.write_bytes(raw)
            entries[1]['images'][0]['metadata']={'attempt':2}
            entries[1]['images'].append({'path':'raw/c7/cover.png','sha256':hashlib.sha256(raw).hexdigest(),
                                        'role':'list-selected','metadata':{'attempt':2}})
            for e in entries:
                for i in e['images']:i.setdefault('role','overview-top')
            atomic_json(root/'session.json',{'session_id':'session','entries':entries})
            preview=memory_preview(library,'a')
            self.assertEqual(preview['ordinal'],7)
            self.assertEqual(base64.b64decode(preview['image'].split(',')[1]),raw)
            image_path.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'校验失败'):memory_preview(library,'a')


    def test_selection_keeps_each_character_and_uncertain_challengers(self):
        candidates=[{'id':str(i),'idol_id':i//3} for i in range(6)]
        results={str(i):{'status':'ok','summary':{'mean':v,'standard_error':se}} for i,(v,se) in enumerate([(100,1),(95,1),(90,10),(40,1),(20,1),(1,1)])}
        self.assertEqual(set(select_finalists(candidates,results,1,1)),{'0','2','3'})

    def test_adaptive_caps_and_promotion(self):
        candidates=[{'id':f'{idol}-{i}', 'idol_id':idol} for idol in range(3) for i in range(40)]
        scores={c['id']:{'status':'ok','summary':{'mean':[100,95,80][c['idol_id']], 'standard_error':0}}
                for c in candidates}
        picks, allocation=adaptive_finalists(candidates,scores)
        self.assertEqual([allocation[str(i)]['groups'] for i in range(3)],[15,2,1])
        weak=next(k for k in picks if k.startswith('2-'))
        refined={weak:{'status':'ok','summary':{'mean':110,'standard_error':1}}}
        expanded, allocation=adaptive_finalists(candidates,scores,refined)
        self.assertEqual(allocation['2']['groups'],15)
        self.assertTrue(allocation['2']['promoted'])
        self.assertIn(weak,expanded)
        self.assertTrue(set(picks).issubset(expanded))
        refined[weak]['summary']['mean']=85
        self.assertEqual(adaptive_finalists(candidates,scores,refined)[1]['2']['groups'],1)

    def test_balanced_policy_unaffected_by_one_character_flooding_top(self):
        rows=[{'id':f'{idol}-{i:03}', 'idol_id':idol} for idol in range(4) for i in range(20)]
        results={c['id']:{'status':'ok','summary':{'mean':[100,95,90,50][c['idol_id']], 'standard_error':0}} for c in rows}
        picks,allocation=balanced_finalists(rows,results)
        self.assertEqual([allocation[str(i)]['groups'] for i in range(4)],[15,15,15,3])
        extra=[{'id':f'extra-{i}', 'idol_id':0} for i in range(100)]
        flooded={**results,**{c['id']:{'status':'ok','summary':{'mean':100,'standard_error':0}} for c in extra}}
        _,after=balanced_finalists(rows+extra,flooded)
        for idol in ('1','2','3'):self.assertEqual(allocation[idol],after[idol])
        self.assertEqual(allocation['3']['screen_boundary'],90)
        measured={'3-000':{'status':'ok','summary':{'mean':110,'standard_error':0}}}
        _,promoted=balanced_finalists(rows,results,measured)
        self.assertEqual(promoted['3']['groups'],15)
        self.assertTrue(promoted['3']['promoted'])

    def test_balanced_policy_small_character_and_snapshot_upgrade(self):
        from gkshot.simulation_rounds import stability
        rows=[{'id':str(i),'idol_id':i//2} for i in range(5)]
        results={r['id']:{'status':'ok','summary':{'mean':100-i,'standard_error':0,'trials':20}} for i,r in enumerate(rows)}
        self.assertEqual(len(balanced_finalists(rows,results)[0]),5)
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            manifest={'config':{'refine_policy':POLICY_ADAPTIVE},'candidates':rows}
            state={'state':'cancelled','screen':results,'refine':{},'selected':None}
            snap=stability(rows,results,select=adaptive_finalists)
            atomic_json(folder/'manifest.json',manifest);atomic_json(folder/'state.json',state)
            atomic_json(folder/'snapshots/20.json',dict(snap,results=results))
            upgrade_screening_policy(folder)
            upgraded=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(upgraded['config']['refine_policy'],POLICY_BALANCED)
            self.assertEqual(json.loads((folder/'snapshots/20.json').read_text(encoding='utf-8'))['results'],results)
            self.assertTrue((folder/'policy-backup-v1.json').exists())
            state['state']='running';atomic_json(folder/'state.json',state)
            with self.assertRaises(ValueError):upgrade_screening_policy(folder)

    def test_round_screening_resume_and_equal_trial_snapshots(self):
        from gkshot.simulation_rounds import screen_rounds
        rows,_=enumerate_pairs([self.record('a',12),self.record('b',35,1)],stage_id=160,support_bonus=.0472)
        manifest={'config':{'seed':42},'candidates':rows}
        state={'screen':{},'refine':{},'selected':None,'state':'running'}
        cancel=threading.Event()
        with tempfile.TemporaryDirectory() as temp, LocalSimulator(persistent=True) as engine:
            def publish(s,progress_only=False):
                if not progress_only and s.get('screen_round')==1 and s.get('completed')==1:cancel.set()
            self.assertFalse(screen_rounds(temp,manifest,state,engine,cancel,publish,None,adaptive_finalists))
            first=next((Path(temp)/'rounds/1').glob('*.json'));stamp=first.stat().st_mtime_ns
            state['state']='running'
            self.assertTrue(screen_rounds(temp,manifest,state,engine,threading.Event(),lambda *a,**k:None,None,adaptive_finalists))
            self.assertEqual(first.stat().st_mtime_ns,stamp)
            self.assertEqual([h['trials'] for h in state['round_history']],[20,40,60,80,100])
            for count in (20,40,60,80,100):
                snapshot=json.loads((Path(temp)/'snapshots'/f'{count}.json').read_text())
                self.assertEqual({r['summary']['trials'] for r in snapshot['results'].values()},{count})
            final=json.loads((Path(temp)/'rounds/5'/first.name).read_text())
            self.assertEqual(len(set(final['simulation']['round_seeds'])),5)
            self.assertEqual(len(final['simulation']['scores']),100)
            self.assertEqual(final['simulation']['scores'][:20],json.loads(first.read_text())['simulation']['scores'])
            self.assertIn('changes',state['round_history'][-1])

    def test_round_boundary_yields_without_refinement_and_resumes(self):
        rows,_=enumerate_pairs([self.record('a',12),self.record('b',35,1)],stage_id=160,support_bonus=.0472)
        manifest={'config':{'seed':42,'refine_policy':POLICY_BALANCED,'screen_policy':'five-rounds-20-v1'},
                  'identity':LocalSimulator().identity(),'candidates':rows,'rejected':[]}
        with tempfile.TemporaryDirectory() as folder:
            def run(limit):
                return run_search(folder,manifest,cancel=threading.Event(),publish=lambda *a,**k:None,stop_after_round=limit)
            state=run(1)
            self.assertEqual(state['state'],'round-wait')
            self.assertFalse(state['refine'])
            first=next((Path(folder)/'rounds/1').glob('*.json'));stamp=first.stat().st_mtime_ns
            self.assertEqual(run(1)['round_history'],state['round_history'])
            self.assertFalse((Path(folder)/'rounds/2').exists())
            state=run(5)
            self.assertEqual(state['state'],'round-wait')
            self.assertFalse(state['refine'])
            self.assertEqual([h['trials'] for h in state['round_history']],[20,40,60,80,100])
            self.assertEqual(first.stat().st_mtime_ns,stamp)
            state=run(None)
            self.assertEqual(state['state'],'complete')
            self.assertEqual(len(state['refine']),2)

    def test_persistent_worker_parity_and_recovery_after_cancel(self):
        other=self.loadout.to_dict();other['customizations'][1][3]=''
        other=Loadout.from_dict(other)
        with LocalSimulator(persistent=True) as engine:
            a=engine.simulate(self.loadout,trials=10)
            pid=engine._worker[0].pid
            b=engine.simulate(other,trials=10)
            self.assertEqual(pid,engine._worker[0].pid)
            self.assertEqual(a['scores'],engine.simulate(self.loadout,trials=10)['scores'])
            self.assertEqual(b['scores'],LocalSimulator().simulate(other,trials=10)['scores'])
            cancel=threading.Event()
            with self.assertRaises(SimulationCancelled):
                engine.simulate(self.loadout,trials=100000,cancel=cancel,progress=lambda *_:cancel.set())
            self.assertIsNone(engine._worker)
            self.assertEqual(a['scores'],engine.simulate(self.loadout,trials=10)['scores'])
        self.assertIsNone(engine._worker)

    def test_real_two_phase_run_and_resume_avoids_repeating(self):
        rows,_=enumerate_pairs([self.record('a',12),self.record('b',35,1)],stage_id=160,support_bonus=.0472)
        cancel=threading.Event()
        manifest={'config':{'seed':42,'refine_policy':POLICY_ADAPTIVE},'identity':LocalSimulator().identity(),'candidates':rows,'rejected':[]}
        with tempfile.TemporaryDirectory() as temp:
            def publish(s,progress_only=False):
                if not progress_only and len(s.get('screen',{}))==1 and not s.get('refine'):
                    cancel.set()
            first=run_search(temp,manifest,cancel=cancel,publish=publish)
            self.assertEqual(first['state'],'cancelled')
            saved=Path(temp)/'screen'/f"{rows[0]['id']}.json";stamp=saved.stat().st_mtime_ns
            final=run_search(temp,manifest,cancel=threading.Event(),publish=lambda *a,**k:None)
            self.assertEqual(final['state'],'complete')
            self.assertEqual(len(final['screen']),2);self.assertEqual(len(final['refine']),2)
            self.assertEqual(saved.stat().st_mtime_ns,stamp)
            self.assertEqual({r['summary']['trials'] for r in ranking(manifest,final,phase='refine')},{2000})
            self.assertEqual({r['summary']['trials'] for r in ranking(manifest,final,phase='screen')},{100})
            self.assertEqual(len(ranking(manifest,final,view='characters')),1)
            detail=json.loads(next((Path(temp)/'refine').glob('*.json')).read_text())
            self.assertEqual(detail['simulation']['seed'],42 ^ 0x9e3779b9)
            self.assertEqual(summary(detail['simulation']['scores']),detail['summary'])
            changed=copy.deepcopy(manifest);changed['identity']['revision']='changed'
            with self.assertRaisesRegex(ValueError,'引擎版本'):
                run_search(temp,changed,cancel=threading.Event(),publish=lambda *a,**k:None)

if __name__=='__main__':unittest.main()
