import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.client import HTTPConnection

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from gkshot.simulation_web import SimulationServer, launch
from gkshot.models import Loadout


class SimulationWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = SimulationServer(('127.0.0.1', 0))
        cls.library_fixture = tempfile.TemporaryDirectory()
        cls.server.libraries = {'test-library': Path(cls.library_fixture.name)}
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.loadout = Loadout.load(ROOT / 'examples/simulation/original.loadout.json').to_dict()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.library_fixture.cleanup()

    def request(self, path, body=None, headers=None, authenticated=True):
        h = {'X-GK-Token': self.server.token} if authenticated else {}
        if headers: h.update(headers)
        data = json.dumps(body).encode() if body is not None else None
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            connection.request('POST' if body is not None else 'GET', path, body=data, headers=h)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def submit(self, trials=3):
        code, raw = self.request('/api/run', {'candidates': [{'name': 'web-test', 'loadout': self.loadout}], 'trials': trials, 'seed': 610397104})
        self.assertEqual(code, 202, raw)
        return json.loads(raw)['job_id']

    def completed(self, job_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            code, raw = self.request('/api/jobs/' + job_id)
            self.assertEqual(code, 200)
            job = json.loads(raw)
            if job['state'] != 'running': return job
            time.sleep(.03)
        self.fail('Job did not complete')

    def test_real_scores_through_http(self):
        job = self.completed(self.submit())
        self.assertEqual(job['state'], 'complete')
        self.assertEqual(job['report']['results'][0]['simulation']['scores'], [94042, 24119, 85058])

    def test_cancel_and_busy_guard(self):
        job_id = self.submit(100000)
        code, _ = self.request('/api/run', {'candidates': [{'name': 'another', 'loadout': self.loadout}], 'trials': 3, 'seed': 1})
        self.assertEqual(code, 409)
        self.assertEqual(self.request('/api/cancel', {'job_id': job_id})[0], 200)
        self.assertEqual(self.completed(job_id)['state'], 'cancelled')

    def test_origin_and_token_required(self):
        self.assertEqual(self.request('/api/bootstrap', authenticated=False)[0], 403)
        self.assertEqual(self.request('/api/cancel', {'job_id': 'x'}, {'Origin':'https://other.example'})[0], 403)

    def test_return_to_workbench_requires_managed_service_and_authentication(self):
        self.assertEqual(self.request('/api/workbench', {})[0],404)
        event=threading.Event()
        self.server.workbench_requested=event
        try:
            self.assertEqual(self.request('/api/workbench', {},authenticated=False)[0],403)
            self.assertEqual(self.request('/api/workbench', {},{'Origin':'https://other.example'})[0],403)
            self.assertFalse(event.is_set())
            self.assertEqual(self.request('/api/workbench', {})[0],200)
            self.assertTrue(event.is_set())
        finally:
            del self.server.workbench_requested
        self.assertEqual(self.request('/', headers={'Host':'other.example'})[0], 403)

    def test_no_arbitrary_file_or_library_access(self):
        self.assertEqual(self.request('/../../HANDOFF.md')[0], 404)
        self.assertEqual(self.request('/images/cards/../../HANDOFF.md')[0], 404)
        self.assertEqual(self.request('/api/memories?library=C:/Windows')[0], 400)

    def test_static_assets_and_catalog(self):
        code, html = self.request('/', authenticated=False)
        self.assertEqual(code, 200)
        self.assertIn(self.server.token.encode(), html)
        self.assertEqual(self.request('/images/cards/567.png', authenticated=False)[0], 200)
        self.assertEqual(self.request('/library-handoff.js', authenticated=False)[0], 200)
        code, raw = self.request('/api/bootstrap')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['initial'], self.loadout)

    def test_unknown_customization_not_filled(self):
        data = json.loads(json.dumps(self.loadout))
        data['customizations'][0][0] = None
        code, raw = self.request('/api/run', {'candidates':[{'name':'invalid','loadout':data}], 'trials':1,'seed':1})
        self.assertEqual(code, 400)
        self.assertIn('customization', raw.decode())

    def test_different_stages_rejected(self):
        other = dict(self.loadout, stage_id=161)
        code, _ = self.request('/api/run', {'candidates':[{'name':'A','loadout':self.loadout},{'name':'B','loadout':other}],'trials':1,'seed':1})
        self.assertEqual(code, 400)

    def test_search_http_lifecycle_and_rankings(self):
        from gkshot.simulation_search import enumerate_pairs
        records=[]
        for i in range(2):
            records.append({'id':str(i), 'ordinal':i+10, 'identity':{}, 'draft':{
                'parameters':self.loadout['parameters'], 'item_ids':self.loadout['item_ids'],
                'card_ids':self.loadout['card_ids'][i], 'customizations':self.loadout['customizations'][i], 'p_idol_id':97}})
        library=next(iter(self.server.libraries))
        config={'library':library,'stage_id':160,'support_bonus':.0472,'seed':610397104}
        original_root=self.server.search_root
        try:
            with tempfile.TemporaryDirectory() as folder, patch('gkshot.simulation_web.memory_records',return_value=records):
                self.server.search_root=Path(folder)
                code,raw=self.request('/api/search/preview',config)
                self.assertEqual(code,200,raw);self.assertEqual(json.loads(raw)['pairs'],2)
                self.assertEqual(self.request('/api/search/start',dict(config,stop_after_round=True))[0],400)
                code,raw=self.request('/api/search/start',dict(config,defer_start=True))
                self.assertEqual(code,202,raw);sid=json.loads(raw)['search_id']
                self.assertEqual(json.loads(self.request('/api/searches/'+sid)[1])['state'],'queued')
                self.assertEqual(self.request('/api/search/resume',{'search_id':sid,'stop_after_round':1})[0],200)
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    data=json.loads(self.request('/api/searches/'+sid)[1])
                    if data['state']!='running':break
                    time.sleep(.05)
                self.assertEqual(data['state'],'round-wait',data)
                self.assertEqual(data['refine_done'],0)
                self.assertEqual(data['round_history'][-1]['trials'],20)
                self.assertEqual(self.request('/api/search/resume',{'search_id':sid})[0],200)
                self.assertEqual(self.request('/api/run',{'candidates':[{'name':'busy','loadout':self.loadout}],'trials':1,'seed':1})[0],409)
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    code,raw=self.request('/api/searches/'+sid)
                    data=json.loads(raw)
                    if data['state']!='running':break
                    time.sleep(.05)
                self.assertEqual(data['state'],'complete',data)
                self.assertEqual(data['row_count'],2)
                self.assertEqual(data['rows'][0]['summary']['trials'],2000)
                code,raw=self.request('/api/searches/'+sid+'?view=characters&phase=screen')
                self.assertEqual(json.loads(raw)['row_count'],1)
                candidate=data['rows'][0]['id']
                code,raw=self.request(f'/api/searches/{sid}/refine/{candidate}')
                self.assertEqual(len(json.loads(raw)['simulation']['scores']),2000)
                # Simulate reopening the process: persisted manifests/results reconstruct the UI.
                self.server.searches.clear()
                code,raw=self.request('/api/searches')
                self.assertIn(sid,[r['id'] for r in json.loads(raw)['searches']])
                self.assertEqual(self.request('/api/searches/'+sid+'?phase=invalid')[0],400)
                self.assertEqual(self.request('/api/memory-preview?library=C:/Windows&memory=x')[0],400)
        finally:
            self.server.search_root=original_root
            self.server.searches.clear()

    def test_launcher_reuses_existing_server(self):
        with patch('webbrowser.open') as browser:
            launch(port=self.server.server_port)
            browser.assert_called_once_with(self.server.origin)

    def test_season_navigation_without_development_queue(self):
        import os
        original_root = self.server.search_root
        original_searches = self.server.searches
        try:
            with tempfile.TemporaryDirectory() as folder, patch('gkshot.simulation_web.PROJECT_ROOT', Path(folder)), patch.object(self.server, 'load_searches'):
                self.server.search_root = Path(folder) / 'searches'
                self.server.searches = {}
                # Deliberately insert oldest last: in-memory insertion is not creation order.
                for key, stage, status, timestamp in [('b'*32,181,'complete',200), ('c'*32,182,'complete',300), ('a'*32,181,'running',100)]:
                    path = self.server.search_root / key / 'manifest.json'
                    path.parent.mkdir(parents=True)
                    path.write_text('{}')
                    os.utime(path, ns=(timestamp, timestamp))
                    self.server.searches[key] = {'manifest': {'config': {'stage_id': stage}}, 'snapshot': {'state': status}}
                def stages():
                    code, raw = self.request('/api/season-progress?season=52')
                    self.assertEqual(code, 200, raw)
                    return json.loads(raw)['stages']
                rows = stages()
                self.assertEqual([r['search_id'] for r in rows], ['a'*32, 'c'*32, None])
                self.server.searches['a'*32]['snapshot']['state'] = 'complete'
                self.assertEqual(stages()[0]['search_id'], 'b'*32)
                queue = Path(folder) / 'work/simulation/season52-rounds-queue.json'
                queue.parent.mkdir(parents=True)
                queue.write_text(json.dumps([{'stage_id':181,'search_id':'a'*32}, {'stage_id':182,'search_id':'f'*32}]))
                # Legacy script queues must not override the current web selection.
                self.assertEqual([r['search_id'] for r in stages()], ['b'*32, 'c'*32, None])
        finally:
            self.server.search_root = original_root
            self.server.searches = original_searches


if __name__ == '__main__':
    unittest.main()
