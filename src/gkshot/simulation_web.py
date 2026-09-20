"""Loopback-only web UI for the pinned local engine; no external dependencies."""
import copy
import json
import re
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from uuid import uuid4

from gkshot.app_paths import USER_ROOT
from gkshot.catalog import Catalog, PROJECT_ROOT
from gkshot.models import Loadout
from gkshot.adapters.local_engine import LocalSimulator
from gkshot.simulation_rounds import POLICY as ROUND_POLICY
from gkshot.simulation_search import memory_records, memory_preview, enumerate_pairs, run_search, ranking, POLICY_ADAPTIVE, POLICY_BALANCED
from gkshot.simulation import atomic_json, compare, compose_memories, list_ready_memories, read_ready_memory

STATIC = PROJECT_ROOT / 'src/gkshot/web'


class SimulationServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        # Windows SO_REUSEADDR can bind two listeners to the same port.
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address=('127.0.0.1', 8765)):
        self.jobs = {}
        self.searches = {}
        self.search_root = USER_ROOT / "work/simulation/searches"
        self.guard = threading.Lock()
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.catalog = Catalog()
        self.libraries = {}
        for database in sorted((USER_ROOT / 'data/captures').glob('*/processing/*/memory-library/library.sqlite3')):
            key = str(database.parent.relative_to(USER_ROOT)).replace('\\', '/')
            self.libraries[key] = database.parent

    def load_searches(self):
        for path in sorted(self.search_root.glob('*/manifest.json'), key=lambda p: p.stat().st_mtime_ns):
            search_id = path.parent.name
            if not re.fullmatch(r'[a-f0-9]{32}', search_id) or search_id in self.searches:
                continue
            try:
                manifest = json.loads(path.read_text(encoding='utf-8'))
                saved = path.parent / 'state.json'
                state = json.loads(saved.read_text(encoding='utf-8')) if saved.exists() else {}
                if state.get('state') in (None, 'running'):
                    state['state'] = 'interrupted'
                self.searches[search_id] = {'manifest': manifest, 'snapshot': state, 'cancel': threading.Event()}
            except (OSError, ValueError):
                continue

    def busy(self):
        return (any(j['state'] == 'running' for j in self.jobs.values()) or
                any(j['snapshot'].get('state') == 'running' for j in self.searches.values()))

    def run_search_job(self, search_id):
        job = self.searches[search_id]
        def publish(state, progress_only=False):
            with self.guard:
                if progress_only:
                    job['snapshot']['progress'] = copy.deepcopy(state.get('progress'))
                else:
                    job['snapshot'] = copy.deepcopy(state)
        try:
            run_search(self.search_root / search_id, job['manifest'], cancel=job['cancel'],
                       publish=publish, cache=USER_ROOT / 'work/simulation/cache',
                       stop_after_round=job.get('stop_after_round'))
        except Exception as error:
            with self.guard:
                job['snapshot'].update(state='error', error=str(error))

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.server_port}'

    def run_job(self, job_id, candidates, trials, seed):
        job = self.jobs[job_id]
        def progress(name, done, total):
            with self.guard:
                job['progress'] = {'name': name, 'completed': done, 'trials': total}
        try:
            report = compare(candidates, trials=trials, seed=seed, cancel=job['cancel'],
                progress=progress, cache=USER_ROOT / 'work/simulation/cache')
            with self.guard:
                job['report'] = report
                job['state'] = report['status']
        except Exception as error:
            with self.guard:
                job.update(state='error', error=str(error))

    def server_close(self):
        for job in list(self.jobs.values()) + list(self.searches.values()):
            job['cancel'].set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, content, mime='application/json; charset=utf-8'):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(content)

    def valid_host(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Only the loopback host is allowed'})
        target = urlsplit(self.path)
        try:
            if target.path == '/':
                html = (STATIC / 'index.html').read_text(encoding='utf-8').replace('__TOKEN__', self.server.token)
                return self.send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
            if target.path in ('/app.js', '/ranking.js', '/style.css'):
                mime = 'text/javascript; charset=utf-8' if target.path.endswith('.js') else 'text/css; charset=utf-8'
                return self.send(200, (STATIC / target.path[1:]).read_bytes(), mime)
            if target.path == '/LICENSE':
                return self.send(200, (PROJECT_ROOT / 'runtime/node/upstream/LICENSE').read_bytes(), 'text/plain; charset=utf-8')
            match = re.fullmatch(r'/images/(cards|items)/(\d+)\.png', target.path)
            if match:
                folder = 'card-icons' if match[1] == 'cards' else 'pitem-icons'
                path = PROJECT_ROOT / 'assets/recognition' / folder / f'{int(match[2])}.png'
                if path.is_file():
                    return self.send(200, path.read_bytes(), 'image/png')
                return self.send(404, {'error': '图片不存在'})
            # API reads also require a per-process token, including library metadata.
            if self.headers.get('X-GK-Token') != self.server.token:
                return self.send(403, {'error': '请从本地首页重新打开'})
            if target.path == '/api/bootstrap':
                catalog = self.server.catalog
                return self.send(200, {'cards': list(catalog.skill_cards.values()),
                    'items': [x for x in catalog.p_items.values() if x['mode'] == 'stage'],
                    'stages': [x for x in catalog.stages.values() if x['type'] == 'contest'],
                    'customizations': list(catalog.customizations.values()),
                    'p_idols': json.loads((PROJECT_ROOT / 'runtime/node/upstream/packages/gakumas-data/json/p_idols.json').read_text(encoding='utf-8')),
                    'revision': catalog.manifest['revision'],
                    'initial': Loadout.load(PROJECT_ROOT / 'examples/simulation/original.loadout.json').to_dict(),
                    'libraries': list(reversed(self.server.libraries))})
            if target.path == '/api/memory-preview':
                query = parse_qs(target.query)
                library = self.server.libraries.get(query.get('library', [''])[0])
                if library is None:
                    raise ValueError('请选择已归档的记忆库')
                return self.send(200, memory_preview(library, query.get('memory', [''])[0]))
            if target.path == '/api/memories':
                library = parse_qs(target.query).get('library', [''])[0]
                if library not in self.server.libraries:
                    raise ValueError('请选择已归档的记忆库')
                return self.send(200, {'memories': memory_records(self.server.libraries[library])})
            if target.path == '/api/season-progress':
                queue_path = USER_ROOT / 'work/simulation/season52-rounds-queue.json'
                queue = json.loads(queue_path.read_text(encoding='utf-8')) if queue_path.exists() else []
                with self.server.guard:
                    self.server.load_searches()
                    stages = []
                    for stage_id in (181, 182, 183):
                        entry = next((e for e in queue if e['stage_id'] == stage_id), {})
                        search_id = entry.get('search_id')
                        job = self.server.searches.get(search_id)
                        if job is None or job['manifest']['config']['stage_id'] != stage_id:
                            candidates = [(key, value) for key, value in self.server.searches.items()
                                          if value['manifest']['config']['stage_id'] == stage_id]
                            def priority(candidate):
                                key, value = candidate
                                path = self.server.search_root / key / 'manifest.json'
                                modified = path.stat().st_mtime_ns if path.exists() else 0
                                return value['snapshot'].get('state') == 'running', modified, key
                            search_id, job = max(candidates, key=priority) if candidates else (None, None)
                        state = job['snapshot'] if job else {}
                        stages.append({'stage_id': stage_id, 'search_id': search_id,
                            'state': state.get('state', 'pending'), 'phase': state.get('phase'),
                            'trials': max((h['trials'] for h in state.get('round_history', [])), default=0),
                            'completed': state.get('completed', 0), 'total': state.get('total', 0)})
                return self.send(200, {'season': 52, 'stages': stages})
            if target.path == '/api/searches':
                with self.server.guard:
                    self.server.load_searches()
                    return self.send(200, {'searches': [{'id': key, 'config': j['manifest']['config'],
                        'state': j['snapshot'].get('state'), 'pairs': len(j['manifest']['candidates'])}
                        for key, j in reversed(list(self.server.searches.items()))]})
            match = re.fullmatch(r'/api/searches/([a-f0-9]{32})(?:/(screen|refine)/([a-f0-9]{24}))?', target.path)
            if match:
                query = parse_qs(target.query)
                with self.server.guard:
                    self.server.load_searches()
                    job = self.server.searches.get(match[1])
                    if job is None:
                        return self.send(404, {'error': '搜索不存在'})
                    if match[2]:
                        root = self.server.search_root / match[1]
                        if match[2] == 'screen' and job['snapshot'].get('detail_round'):
                            trials = int(query.get('trials', [str(job['snapshot']['detail_round']*20)])[0])
                            if trials not in (20,40,60,80,100):
                                raise ValueError('未知初筛轮次')
                            root = root / 'rounds' / str(trials//20)
                        else:
                            root = root / match[2]
                        path = root / (match[3] + '.json')
                        if not path.is_file():
                            return self.send(404, {'error': '该组合尚未完成'})
                        return self.send(200, path.read_bytes())
                    manifest, state = job['manifest'], job['snapshot']
                    phase = query.get('phase', ['refine'])[0]
                    view = query.get('view', ['total'])[0]
                    idol = int(query['idol'][0]) if query.get('idol', [''])[0] else None
                    offset = max(0, int(query.get('offset', ['0'])[0]))
                    rows = ranking(manifest, state, phase=phase, view=view, idol_id=idol)
                    errors = [{'id': k, 'phase': p, 'error': v.get('error')} for p in ('screen', 'refine')
                              for k, v in state.get(p, {}).items() if v['status'] != 'ok']
                    return self.send(200, {'id': match[1], 'config': manifest['config'],
                        'state': state.get('state'), 'phase': state.get('phase'), 'error': state.get('error'),
                        'progress': state.get('progress'), 'completed': state.get('completed', 0),
                        'total': state.get('total', len(manifest['candidates'])), 'pairs': len(manifest['candidates']),
                        'screen_done': sum(v['status'] == 'ok' for v in state.get('screen', {}).values()),
                        'refine_done': sum(v['status'] == 'ok' for v in state.get('refine', {}).values()),
                        'round_history': state.get('round_history', []), 'screen_round': state.get('screen_round'), 'round_errors': state.get('round_errors', []), 'allocation': state.get('allocation', {}), 'selected': len(state.get('selected') or []), 'rejected': manifest['rejected'],
                        'errors': errors, 'rows': rows[offset:offset + 50], 'row_count': len(rows)})
            match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})', target.path)
            if match:
                with self.server.guard:
                    job = self.server.jobs.get(match[1])
                    if job is None:
                        return self.send(404, {'error': '找不到此任务，请重新运行'})
                    return self.send(200, {k: v for k, v in job.items() if k != 'cancel'})
            return self.send(404, {'error': 'Not found'})
        except (ValueError, OSError, KeyError) as error:
            self.send(400, {'error': str(error)})

    def do_POST(self):
        if (not self.valid_host() or self.headers.get('X-GK-Token') != self.server.token
            or self.headers.get('Origin') not in (None, self.server.origin)):
            return self.send(403, {'error': '请求来源不匹配，请重新打开本地页面'})
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 1024 * 1024:
                raise ValueError('请求大小不正确')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('请求必须是对象')
            stop_after_round = body.get('stop_after_round')
            if stop_after_round is not None and (type(stop_after_round) is not int or not 1 <= stop_after_round <= 5):
                raise ValueError('轮次上限须为1至5的整数')
            defer_start = body.get('defer_start', False)
            if type(defer_start) is not bool:
                raise ValueError('延后启动须为布尔值')
            if self.path in ('/api/search/preview', '/api/search/start'):
                library = self.server.libraries.get(body['library'])
                if library is None:
                    raise ValueError('请选择已归档的记忆库')
                seed = body['seed']
                if type(seed) is not int or not 0 <= seed <= 0xffffffff:
                    raise ValueError('种子须为 uint32 整数')
                rows, rejected = enumerate_pairs(memory_records(library), stage_id=body['stage_id'],
                    support_bonus=body['support_bonus'], catalog=self.server.catalog)
                if not rows:
                    raise ValueError('本场没有可以配对的主副记忆')
                if self.path.endswith('/preview'):
                    return self.send(200, {'pairs': len(rows), 'characters': len({r['idol_id'] for r in rows}), 'rejected': rejected})
                with self.server.guard:
                    if self.server.busy() and not defer_start:
                        return self.send(409, {'error': '已有模拟正在运行，请等待或停止'})
                    search_id = uuid4().hex
                    config = {k: body[k] for k in ('library', 'stage_id', 'support_bonus', 'seed')}
                    config['refine_policy'] = POLICY_BALANCED
                    config['screen_policy'] = ROUND_POLICY
                    manifest = {'schema_version': 1, 'config': config, 'identity': LocalSimulator().identity(),
                                'candidates': rows, 'rejected': rejected}
                    atomic_json(self.server.search_root / search_id / 'manifest.json', manifest)
                    initial = {'state': 'queued' if defer_start else 'running', 'screen': {}, 'refine': {}, 'selected': None}
                    atomic_json(self.server.search_root / search_id / 'state.json', initial)
                    self.server.searches[search_id] = {'manifest': manifest, 'snapshot': initial, 'cancel': threading.Event(), 'stop_after_round': stop_after_round}
                if not defer_start:
                    threading.Thread(target=self.server.run_search_job, args=(search_id,), daemon=True).start()
                return self.send(202, {'search_id': search_id})
            if self.path in ('/api/search/resume', '/api/search/cancel'):
                with self.server.guard:
                    self.server.load_searches()
                    job = self.server.searches.get(body['search_id'])
                    if job is None:
                        raise ValueError('搜索不存在')
                    if self.path.endswith('/cancel'):
                        job['cancel'].set()
                    else:
                        if self.server.busy():
                            return self.send(409, {'error': '已有模拟正在运行'})
                        job['cancel'] = threading.Event()
                        job['stop_after_round'] = stop_after_round
                        job['snapshot']['state'] = 'running'
                        threading.Thread(target=self.server.run_search_job, args=(body['search_id'],), daemon=True).start()
                return self.send(200, {'ok': True})
            if self.path == '/api/run':
                rows = body['candidates']
                if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
                    raise ValueError('每批选择1到20个候选')
                candidates = [(r['name'], Loadout.from_dict(r['loadout'])) for r in rows]
                if any(not isinstance(name, str) or not name.strip() or len(name) > 120 for name, _ in candidates):
                    raise ValueError('请填写不超过120字的候选名称')
                if len({name for name, _ in candidates}) != len(candidates):
                    raise ValueError('候选名称不能重复')
                if len({l.stage_id for _, l in candidates}) != 1:
                    raise ValueError('请比较同一场地的候选')
                trials, seed = body['trials'], body['seed']
                if type(trials) is not int or not 1 <= trials <= 100000 or type(seed) is not int or not 0 <= seed <= 0xffffffff:
                    raise ValueError('次数须为1–100000，种子须为0–4294967295整数')
                with self.server.guard:
                    if self.server.busy():
                        return self.send(409, {'error': '已有模拟正在运行，请等待或取消'})
                    while len(self.server.jobs) >= 10:
                        del self.server.jobs[next(iter(self.server.jobs))]
                    job_id = uuid4().hex
                    self.server.jobs[job_id] = {'state': 'running', 'cancel': threading.Event(), 'progress': None}
                threading.Thread(target=self.server.run_job, args=(job_id, candidates, trials, seed), daemon=True).start()
                return self.send(202, {'job_id': job_id})
            if self.path == '/api/cancel':
                with self.server.guard:
                    job = self.server.jobs.get(body['job_id'])
                    if job is None:
                        raise ValueError('任务不存在')
                    job['cancel'].set()
                return self.send(200, {'ok': True})
            if self.path == '/api/compose':
                library = self.server.libraries.get(body['library'])
                if library is None or body['main'] == body['sub']:
                    raise ValueError('请选择记忆库及两个不同的持有副本')
                result = compose_memories(read_ready_memory(library, body['main']), read_ready_memory(library, body['sub']),
                    stage_id=body['stage_id'], support_bonus=body['support_bonus'])
                return self.send(200, {'loadout': result.to_dict()})
            self.send(404, {'error': 'Not found'})
        except (ValueError, TypeError, KeyError, OSError) as error:
            self.send(400, {'error': str(error)})


def launch(port=8765, open_browser=True):
    if not 0 <= port <= 65535:
        raise ValueError('端口范围为0–65535')
    try:
        server = SimulationServer(('127.0.0.1', port))
    except OSError:
        # Double-clicking the launcher should reuse this application's running page.
        if not open_browser:
            raise
        from http.client import HTTPConnection
        connection = HTTPConnection('127.0.0.1', port, timeout=2)
        try:
            connection.request('GET', '/')
            response = connection.getresponse()
            page = response.read(10000)
            if response.status != 200 or b'name="gk-token"' not in page:
                raise ValueError('此端口被其他程序占用，请使用 --port 指定其他端口')
        finally:
            connection.close()
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{port}')
        return
    print(f'本地模拟器：{server.origin} （关闭此窗口或 Ctrl+C 停止服务）', flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
