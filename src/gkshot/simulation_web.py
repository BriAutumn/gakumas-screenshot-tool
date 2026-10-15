"""Loopback-only web UI for the pinned local engine; no external dependencies."""
import copy
import hashlib
import json
import math
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
from gkshot.resource_paths import RECOGNITION_ROOT, RUNTIME_ROOT
from gkshot.models import Loadout
from gkshot.adapters.local_engine import LocalSimulator
from gkshot.simulation_rounds import POLICY as ROUND_POLICY
from gkshot.simulation_search import memory_records, memory_preview, enumerate_pairs, run_search, ranking, POLICY_ADAPTIVE, POLICY_BALANCED
from gkshot.season_runs import (FINISHED as GROUP_FINISHED, ACTIVE as GROUP_ACTIVE,
    child_summary as season_child_summary, contest_seasons, new_group as season_new_group,
    run_group as season_run_group, season_stage_rows, validate_season)
from gkshot.simulation import atomic_json, compare, compose_memories, list_ready_memories, read_ready_memory
from gkshot.native_service import NativeService
from gkshot.native_memories import CONVERSION_POLICY

STATIC = PROJECT_ROOT / 'src/gkshot/web'

#: POST endpoints that begin or resume engine work; refused while a data
#: activation holds the maintenance marker (a deferred start still counts).
MAINTENANCE_BLOCKED = frozenset({
    '/api/run',
    '/api/search/start',
    '/api/search/resume',
    '/api/season-runs/start',
    '/api/season-runs/resume',
})


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
        self.groups = {}
        self.search_root = USER_ROOT / "work/simulation/searches"
        self.groups_root = USER_ROOT / "work/simulation/season-runs"
        self.cache_root = USER_ROOT / "work/simulation/cache"
        # One injectable factory so tests can supply a fake engine without touching the real one.
        self.simulator_factory = lambda: LocalSimulator(persistent=True)
        self._closing = False
        self.guard = threading.Lock()
        # Set while an upstream data install/rollback activates: no new engine
        # work may start until the process restarts (or the marker is cleared).
        self.maintenance = threading.Event()
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.icon_sha256 = hashlib.sha256((PROJECT_ROOT / "assets/app-icon.png").read_bytes()).hexdigest()
        self.catalog = Catalog()
        self.native_service = NativeService(catalog=self.catalog)
        self.discover_libraries()

    def discover_libraries(self):
        self.libraries = {}
        for database in sorted((USER_ROOT / 'data/native-memories').glob('*/library.sqlite3')):
            try:
                manifest = json.loads((database.parent / 'native-import.json').read_text(encoding='utf8'))
            except (OSError, ValueError):
                continue
            if (manifest.get('conversion_policy') == CONVERSION_POLICY
                    and manifest.get('catalog_revision') == self.catalog.manifest['revision']):
                key = database.parent.relative_to(USER_ROOT).as_posix()
                self.libraries[key] = database.parent

    def library_label(self, path):
        native = path / 'native-import.json'
        if native.is_file():
            manifest = json.loads(native.read_text(encoding='utf-8'))
            return f"原生导出 · {manifest['exported_at'][:10]} · {manifest['ready']} 份"
        return (f"{path.parent.parent.parent.name} / {path.parent.name}"
                if path.parent.parent.name == 'processing' else f"{path.parent.name} / {path.name}")

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

    def load_groups(self):
        try:
            self.groups_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for path in sorted(self.groups_root.glob('*/group.json'), key=lambda p: p.stat().st_mtime_ns):
            group_id = path.parent.name
            if group_id in self.groups or not re.fullmatch(r'[a-f0-9]{32}', group_id):
                continue
            try:
                group = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if group.get('state') == GROUP_ACTIVE:
                # A server restart cannot own a live run; expose it for explicit resume.
                group['state'] = 'interrupted'
            self.reconcile_group(group)
            try:
                atomic_json(path, group)
            except OSError:
                pass
            self.groups[group_id] = {'group': group, 'cancel': threading.Event()}

    def reconcile_group(self, group):
        """Refresh child summaries from each child's own state.json; never recompute segments."""
        for child in group['stages']:
            search_id = child.get('search_id')
            if not search_id:
                continue
            try:
                state = json.loads((self.search_root / search_id / 'state.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            summary = season_child_summary(state)
            if summary['state'] == GROUP_ACTIVE and group.get('state') != GROUP_ACTIVE:
                summary['state'] = 'interrupted'
            child.update(summary)
        return group

    def group_active(self):
        return any(job['group'].get('state') == GROUP_ACTIVE for job in self.groups.values())

    def work_busy(self):
        """Any engine work currently running, ignoring the maintenance marker."""
        return (any(j['state'] == 'running' for j in self.jobs.values()) or
                any(j['snapshot'].get('state') == 'running' for j in self.searches.values()) or
                self.group_active())

    def busy(self):
        # Maintenance counts as busy so every existing start/resume gate refuses
        # new engine work while a data activation is in progress.
        if self.maintenance.is_set():
            return True
        return self.work_busy()

    def begin_maintenance(self):
        """Atomically claim the maintenance marker when no engine work is running."""
        with self.guard:
            if self.maintenance.is_set() or self.work_busy():
                return False
            self.maintenance.set()
            return True

    def end_maintenance(self):
        self.maintenance.clear()

    def publish_group(self, group_id):
        def publish(group, progress_only=False):
            if progress_only:
                return
            with self.guard:
                job = self.groups.get(group_id)
                if job is not None:
                    job['group'] = copy.deepcopy(group)
        return publish

    def run_group_job(self, group_id):
        job = self.groups[group_id]
        def publish_search(search_id, state, progress_only=False):
            with self.guard:
                child = self.searches[search_id]
                if progress_only:
                    child['snapshot']['progress'] = copy.deepcopy(state.get('progress'))
                else:
                    child['snapshot'] = copy.deepcopy(state)
        try:
            group = season_run_group(self.groups_root / group_id, copy.deepcopy(job['group']), cancel=job['cancel'],
                publish=self.publish_group(group_id), simulator_factory=self.simulator_factory,
                search_root=self.search_root, cache=self.cache_root, publish_search=publish_search)
            with self.guard:
                job['group'] = group
        except Exception as error:
            with self.guard:
                job['group'].update(state='error', error=str(error))

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
                       publish=publish, cache=self.cache_root,
                       simulator=self.simulator_factory(),
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
                progress=progress, cache=self.cache_root)
            with self.guard:
                job['report'] = report
                job['state'] = report['status']
        except Exception as error:
            with self.guard:
                job.update(state='error', error=str(error))

    def cancel_owners(self):
        for job in list(self.jobs.values()) + list(self.searches.values()):
            job['cancel'].set()
        for job in list(self.groups.values()):
            job['cancel'].set()

    def shutdown(self):
        # The desktop window stops by calling shutdown() then polling busy(); groups must stop too.
        self._closing = True
        with self.guard:
            self.cancel_owners()
        super().shutdown()

    def server_close(self):
        self.cancel_owners()
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

    def season_progress(self, query):
        server = self.server
        server.load_searches()
        server.load_groups()
        seasons = sorted(contest_seasons(server.catalog), reverse=True)
        raw = query.get('season', [''])[0]
        if raw == '':
            season = seasons[0] if seasons else None
        else:
            try:
                season = int(raw)
            except ValueError:
                raise ValueError('赛季参数不正确')
        if season is None or season not in seasons:
            raise ValueError('该赛季没有可运行的普通竞技场舞台')
        library = query.get('library', [None])[0] or None
        if library is not None and library not in server.libraries:
            raise ValueError('记忆库不在本服务中')
        group_id = query.get('group', [None])[0] or None
        group = None
        if group_id:
            job = server.groups.get(group_id)
            if job is not None:
                config = job['group']['config']
                if config.get('season') == season and (library is None or config.get('library') == library):
                    group = job['group']
        stages = []
        for row in season_stage_rows(server.catalog, season):
            child = next((c for c in group['stages'] if c['stage_id'] == row['id']), None) if group else None
            if child:
                stages.append({'stage_id': row['id'], 'stage': row['stage'],
                    'search_id': child.get('search_id'), 'state': child.get('state', 'pending'),
                    'phase': child.get('phase'), 'trials': child.get('trials', 0),
                    'completed': child.get('completed', 0), 'total': child.get('total', 0)})
            else:
                stages.append({'stage_id': row['id'], 'stage': row['stage'], 'search_id': None,
                    'state': 'pending', 'phase': None, 'trials': 0, 'completed': 0, 'total': 0})
        # Without a selected group, fall back to the current/recent matching searches.
        for item in stages:
            if item['search_id']:
                continue
            candidates = [(key, job) for key, job in server.searches.items()
                          if job['manifest'].get('config', {}).get('stage_id') == item['stage_id']
                          and (library is None or job['manifest']['config'].get('library') == library)]
            if not candidates:
                continue
            def priority(candidate):
                key, job = candidate
                path = server.search_root / key / 'manifest.json'
                modified = path.stat().st_mtime_ns if path.exists() else 0
                return job['snapshot'].get('state') == 'running', modified, key
            key, job = max(candidates, key=priority)
            snapshot = job['snapshot']
            item.update(search_id=key, state=snapshot.get('state', 'pending'), phase=snapshot.get('phase'),
                trials=max((h.get('trials', 0) for h in snapshot.get('round_history', [])), default=0),
                completed=snapshot.get('completed', 0), total=snapshot.get('total', 0))
        return {'season': season, 'library': library,
                'group_id': group['group_id'] if group else None, 'stages': stages}

    def group_view(self, group):
        return {'id': group['group_id'], 'state': group.get('state'), 'error': group.get('error'),
                'active_stage': group.get('active_stage'), 'created_at': group.get('created_at'),
                'updated_at': group.get('updated_at'), 'config': group['config'],
                'errors': group.get('errors') or [],
                'stages': [dict(child) for child in group['stages']]}

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Only the loopback host is allowed'})
        target = urlsplit(self.path)
        try:
            if target.path == '/':
                html = (STATIC / 'index.html').read_text(encoding='utf-8').replace('__TOKEN__', self.server.token).replace('__APP_ICON_SHA__', self.server.icon_sha256)
                return self.send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
            if target.path in ('/app.js', '/ranking.js', '/library-handoff.js', '/season.js', '/chart.js', '/native-import.js', '/style.css'):
                mime = 'text/javascript; charset=utf-8' if target.path.endswith('.js') else 'text/css; charset=utf-8'
                return self.send(200, (STATIC / target.path[1:]).read_bytes(), mime)
            if target.path in ('/app-icon.png', '/favicon.ico'):
                filename = 'app-icon.png' if target.path.endswith('.png') else 'app-icon.ico'
                mime = 'image/png' if target.path.endswith('.png') else 'image/x-icon'
                return self.send(200, (PROJECT_ROOT / 'assets' / filename).read_bytes(), mime)
            if target.path == '/LICENSE':
                return self.send(200, (RUNTIME_ROOT / 'upstream/LICENSE').read_bytes(), 'text/plain; charset=utf-8')
            match = re.fullmatch(r'/images/(cards|items)/(\d+)\.png', target.path)
            if match:
                folder = 'card-icons' if match[1] == 'cards' else 'pitem-icons'
                path = RECOGNITION_ROOT / folder / f'{int(match[2])}.png'
                if path.is_file():
                    return self.send(200, path.read_bytes(), 'image/png')
                return self.send(404, {'error': '图片不存在'})
            # API reads also require a per-process token, including library metadata.
            if self.headers.get('X-GK-Token') != self.server.token:
                return self.send(403, {'error': '请从本地首页重新打开'})
            if target.path == '/api/bootstrap':
                with self.server.guard:
                    self.server.discover_libraries()
                catalog = self.server.catalog
                seasons = sorted(contest_seasons(catalog), reverse=True)
                return self.send(200, {'cards': list(catalog.skill_cards.values()),
                    'items': [x for x in catalog.p_items.values() if x['mode'] == 'stage'],
                    'stages': [x for x in catalog.stages.values() if x['type'] == 'contest'],
                    'customizations': list(catalog.customizations.values()),
                    'p_idols': json.loads((RUNTIME_ROOT / 'upstream/packages/gakumas-data/json/p_idols.json').read_text(encoding='utf-8')),
                    'revision': catalog.manifest['revision'],
                    'initial': Loadout.load(PROJECT_ROOT / 'examples/simulation/original.loadout.json').to_dict(),
                    'libraries': list(reversed(self.server.libraries)),
                    'seasons': seasons, 'default_season': seasons[0] if seasons else None,
                    'desktop_workbench': hasattr(self.server, 'workbench_requested'),
                    'library_labels': {key: self.server.library_label(path)
                        for key, path in self.server.libraries.items()}})
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
                with self.server.guard:
                    return self.send(200, self.season_progress(parse_qs(target.query)))
            if target.path == '/api/season-runs':
                with self.server.guard:
                    self.server.load_groups()
                    return self.send(200, {'groups': [self.group_view(job['group'])
                        for job in sorted(self.server.groups.values(),
                                          key=lambda j: j['group'].get('updated_at') or '', reverse=True)]})
            match = re.fullmatch(r'/api/season-runs/([a-f0-9]{32})', target.path)
            if match:
                with self.server.guard:
                    self.server.load_groups()
                    job = self.server.groups.get(match[1])
                    if job is None:
                        return self.send(404, {'error': '赛季任务不存在'})
                    return self.send(200, self.group_view(job['group']))
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
        except (ValueError, OSError, KeyError, TypeError) as error:
            self.send(400, {'error': str(error)})

    def start_group(self, body):
        library = self.server.libraries.get(body.get('library'))
        if library is None:
            raise ValueError('请选择已归档的记忆库')
        validate_season(self.server.catalog, body.get('season'), body.get('stages'))
        support = body.get('support_bonus')
        if isinstance(support, bool) or not isinstance(support, (int, float)) or not math.isfinite(support) or support < 0:
            raise ValueError('支援加成不正确')
        seed = body.get('seed')
        if type(seed) is not int or not 0 <= seed <= 0xffffffff:
            raise ValueError('种子须为 uint32 整数')
        support = float(support)
        records = memory_records(library)
        identity = self.server.simulator_factory().identity()
        group = season_new_group(self.server.catalog, season=body.get('season'), library=body.get('library'),
            support_bonus=support, seed=seed, refine_policy=POLICY_BALANCED, screen_policy=ROUND_POLICY,
            stages=body.get('stages'))
        prepared = []
        for child in group['stages']:
            candidates, rejected = enumerate_pairs(records, stage_id=child['stage_id'],
                support_bonus=support, catalog=self.server.catalog)
            if not candidates:
                raise ValueError(f'第 {child["stage"]} 舞台没有可以配对的主副记忆；未创建赛季任务。')
            search_id = uuid4().hex
            child['search_id'] = search_id
            prepared.append((search_id, {'schema_version': 1, 'identity': identity,
                'candidates': candidates, 'rejected': rejected,
                'config': {'library': body.get('library'), 'stage_id': child['stage_id'],
                           'support_bonus': support, 'seed': seed, 'refine_policy': POLICY_BALANCED,
                           'screen_policy': ROUND_POLICY, 'season': body.get('season'),
                           'group_id': group['group_id']}}))
        with self.server.guard:
            self.server.load_groups()
            if self.server.busy():
                return self.send(409, {'error': '已有模拟正在运行，请等待或暂停'})
            for search_id, manifest in prepared:
                atomic_json(self.server.search_root / search_id / 'manifest.json', manifest)
                initial = {'state': 'queued', 'screen': {}, 'refine': {}, 'selected': None}
                atomic_json(self.server.search_root / search_id / 'state.json', initial)
                self.server.searches[search_id] = {'manifest': manifest, 'snapshot': initial, 'cancel': threading.Event()}
            group['state'] = GROUP_ACTIVE
            atomic_json(self.server.groups_root / group['group_id'] / 'group.json', group)
            self.server.groups[group['group_id']] = {'group': group, 'cancel': threading.Event()}
        threading.Thread(target=self.server.run_group_job, args=(group['group_id'],), daemon=True).start()
        return self.send(202, {'group_id': group['group_id'],
                               'stages': [child['stage_id'] for child in group['stages']]})

    def resume_group(self, body):
        with self.server.guard:
            self.server.load_searches()
            self.server.load_groups()
            job = self.server.groups.get(body.get('group_id'))
            if job is None:
                raise ValueError('赛季任务不存在')
            group = job['group']
            library = body.get('library')
            if library is not None and library != group['config']['library']:
                return self.send(403, {'error': '此赛季任务属于另一个记忆库，不能在当前会话继续。'})
            if group.get('state') == GROUP_ACTIVE:
                return self.send(409, {'error': '该赛季任务正在运行'})
            if group.get('state') == 'complete':
                return self.send(409, {'error': '该赛季任务已完成；如需重跑请新建。'})
            if self.server.busy():
                return self.send(409, {'error': '已有模拟正在运行'})
            self.server.reconcile_group(group)
            for child in group['stages']:
                child_job = self.server.searches.get(child['search_id'])
                if child_job is None:
                    raise ValueError('缺少子任务记录，不能续跑')
                if child_job['manifest']['identity'] != self.server.simulator_factory().identity():
                    raise ValueError('引擎版本发生变化，请新建赛季任务')
            for child in group['stages']:
                if child.get('state') in ('error', 'cancelled', 'interrupted'):
                    child['state'] = 'queued'
            group['errors'] = []
            group['state'] = GROUP_ACTIVE
            group['active_stage'] = None
            job['cancel'] = threading.Event()
            atomic_json(self.server.groups_root / group['group_id'] / 'group.json', group)
        threading.Thread(target=self.server.run_group_job, args=(group['group_id'],), daemon=True).start()
        return self.send(200, {'ok': True, 'group_id': group['group_id']})

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
            # A data activation pins the marker: refuse any new engine work so
            # the running process never mixes an activated release with old work.
            if self.path in MAINTENANCE_BLOCKED and self.server.maintenance.is_set():
                return self.send(409, {'error': '正在更新模拟数据，请稍后重试'})
            if self.path == '/api/workbench':
                event = getattr(self.server, 'workbench_requested', None)
                if event is None: return self.send(404, {'error': '此服务未由桌面工作台启动'})
                event.set()
                return self.send(200, {'ok': True})
            if self.path == '/api/native/import':
                if body:
                    raise ValueError('读取最新回忆不接受自定义文件路径')
                result = self.server.native_service.import_latest()
                with self.server.guard:
                    self.server.discover_libraries()
                return self.send(200, result)
            stop_after_round = body.get('stop_after_round')
            if stop_after_round is not None and (type(stop_after_round) is not int or not 1 <= stop_after_round <= 5):
                raise ValueError('轮次上限须为1至5的整数')
            defer_start = body.get('defer_start', False)
            if type(defer_start) is not bool:
                raise ValueError('延后启动须为布尔值')
            if self.path == '/api/season-runs/start':
                return self.start_group(body)
            if self.path == '/api/season-runs/pause':
                with self.server.guard:
                    self.server.load_groups()
                    job = self.server.groups.get(body.get('group_id'))
                    if job is None:
                        raise ValueError('赛季任务不存在')
                    if body.get('library') is not None and body['library'] != job['group']['config']['library']:
                        return self.send(403, {'error': '此赛季任务属于另一个记忆库'})
                    job['cancel'].set()
                return self.send(200, {'ok': True})
            if self.path == '/api/season-runs/resume':
                return self.resume_group(body)
            if self.path in ('/api/search/preview', '/api/search/start'):
                library = self.server.libraries.get(body['library'])
                if library is None:
                    raise ValueError('请选择已归档的记忆库')
                seed = body['seed']
                if type(seed) is not int or not 0 <= seed <= 0xffffffff:
                    raise ValueError('种子须为 uint32 整数')
                existing = self.server.catalog.stages.get(body['stage_id'])
                if not existing or existing['type'] != 'contest':
                    raise ValueError('请选择普通竞技场场地；活动与联赛不在此工具范围内')
                rows, rejected = enumerate_pairs(memory_records(library), stage_id=body['stage_id'],
                    support_bonus=body['support_bonus'], catalog=self.server.catalog)
                if not rows:
                    raise ValueError('本场没有可以配对的主副记忆')
                if self.path.endswith('/preview'):
                    return self.send(200, {'pairs': len(rows), 'characters': len({r['idol_id'] for r in rows}), 'rejected': rejected})
                with self.server.guard:
                    self.server.load_groups()
                    if self.server.group_active():
                        return self.send(409, {'error': '赛季轮转正在占用模拟，请先暂停后再单独搜索。'})
                    if self.server.busy() and not defer_start:
                        return self.send(409, {'error': '已有模拟正在运行，请等待或停止'})
                    search_id = uuid4().hex
                    config = {k: body[k] for k in ('library', 'stage_id', 'support_bonus', 'seed')}
                    config['refine_policy'] = POLICY_BALANCED
                    config['screen_policy'] = ROUND_POLICY
                    manifest = {'schema_version': 1, 'config': config,
                                'identity': self.server.simulator_factory().identity(),
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
                    self.server.load_groups()
                    job = self.server.searches.get(body['search_id'])
                    if job is None:
                        raise ValueError('搜索不存在')
                    if body.get('library') is not None and job['manifest']['config'].get('library') != body['library']:
                        return self.send(403, {'error': '此搜索属于另一个记忆库，不能在当前会话继续。'})
                    if self.path.endswith('/cancel'):
                        owner = job['manifest']['config'].get('group_id')
                        if owner and owner in self.server.groups and self.server.groups[owner]['group']['state'] == GROUP_ACTIVE:
                            return self.send(409, {'error': '请使用暂停三场轮转，子任务不能单独停止。'})
                        job['cancel'].set()
                    else:
                        if self.server.group_active():
                            return self.send(409, {'error': '赛季轮转正在占用模拟，请先暂停后再单独继续。'})
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
