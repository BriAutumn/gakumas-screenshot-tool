"""Read-only memory provenance and resumable, per-stage exhaustive pair search."""
import base64
import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from gkshot.adapters.local_engine import LocalSimulator
from gkshot.resource_paths import RUNTIME_ROOT
from gkshot.catalog import Catalog, PROJECT_ROOT
from gkshot.models import Loadout
from gkshot.native_photos import native_photo_preview
from gkshot.simulation_rounds import POLICY as ROUND_POLICY, screen_rounds
from gkshot.memory_library import validate_memory
from gkshot.simulation import atomic_json, compare, compose_memories, native_memory_source


def memory_records(library):
    library = Path(library).resolve()
    session_path = next((p / 'session.json' for p in library.parents if (p / 'session.json').is_file()), None)
    session = json.loads(session_path.read_text(encoding='utf-8')) if session_path else {}
    by_hash = defaultdict(list)
    for entry in session.get('entries', []):
        for image in entry.get('images', []):
            by_hash[image['sha256']].append(entry)
    with closing(sqlite3.connect((library / 'library.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT memory_id,capture_key,status,report_json,review_json FROM memories WHERE status IN ('auto-resolved','reviewed','native-imported') ORDER BY created_at,memory_id").fetchall()
    records = []
    for row in rows:
        report = json.loads(row['report_json'])
        if row['status'] == 'native-imported':
            source = native_memory_source(report, library, row['memory_id'])
            records.append({'id': row['memory_id'], 'draft': report['draft'], 'ordinal': source['ordinal'],
                            'capture_id': None, 'session_id': None, 'identity': report.get('identity', {}),
                            'source': 'native-export', 'snapshot_sha256': source['snapshot_sha256']})
            continue
        if row['status'] == 'reviewed':
            review = json.loads(row['review_json'] or 'null')
            if not review or review.get('confirmed') is not True:
                continue
            draft = review['draft']
        else:
            draft = report['draft']
        hashes = sorted(p['sha256'] for p in report['pages'])
        # Equal screenshots may occur in different owned copies. Require both the
        # ledger's original path and hash for every source page, never hash alone.
        def owns_pages(entry):
            evidence = {(str((session_path.parent / i['path']).resolve()), i['sha256'])
                        for i in entry.get('images', [])}
            return all((str(Path(p['source_path']).resolve()), p['sha256']) in evidence
                       for p in report['pages'])
        matches = {e['capture_id']: e for h in hashes for e in by_hash[h] if owns_pages(e)}
        capture = next(iter(matches.values())) if len(matches) == 1 else {}
        records.append({'id': row['memory_id'], 'draft': draft, 'ordinal': capture.get('ordinal'),
                        'capture_id': capture.get('capture_id'), 'session_id': session.get('session_id'),
                        'identity': report.get('identity', {})})
    return sorted(records, key=lambda r: (r['ordinal'] is None, r['ordinal'] or 0, r['id']))


def pinned_idols(catalog):
    runtime = RUNTIME_ROOT
    manifest = json.loads((runtime / 'manifest.json').read_text(encoding='utf-8'))
    name = 'packages/gakumas-data/json/p_idols.json'
    raw = (runtime / 'upstream' / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['files'][name] or catalog.manifest['revision'] != manifest['revision']:
        raise ValueError('记忆目录和模拟数据的版本不匹配')
    return {p['id']: p for p in json.loads(raw)}


def pinned_names():
    names_path = RUNTIME_ROOT / 'upstream/packages/gakumas-data/json/idols.json'
    names_raw = names_path.read_bytes()
    manifest = json.loads((RUNTIME_ROOT / 'manifest.json').read_text(encoding='utf-8'))
    if hashlib.sha256(names_raw).hexdigest() != manifest['files']['packages/gakumas-data/json/idols.json']:
        raise ValueError('角色名称数据校验失败')
    names = {r['id']: r['name'] for r in json.loads(names_raw)}
    return names


def enumerate_pairs(records, *, stage_id, support_bonus, catalog=None):
    catalog = catalog or Catalog()
    stage = catalog.stages.get(stage_id)
    if not stage or stage['type'] != 'contest':
        raise ValueError('请选择普通竞技场场地')
    idols = pinned_idols(catalog)
    names = pinned_names()
    groups, rejected = defaultdict(list), []
    for record in records:
        try:
            draft = validate_memory(record['draft'], catalog)
            idol = idols[draft['p_idol_id']]
            if stage['plan'] not in ('free', idol['plan']):
                continue
            # Reject mixed plans before expanding into pairs.
            entities = [catalog.skill_cards[i] for i in draft['card_ids'] if i]
            entities += [catalog.p_items[i] for i in draft['item_ids'] if i]
            if any(e['plan'] not in ('free', idol['plan']) for e in entities):
                raise ValueError('卡片或道具计划冲突')
            groups[(idol['idolId'], idol['plan'])].append(record)
        except (ValueError, KeyError, TypeError) as error:
            rejected.append({'memory_id': record['id'], 'ordinal': record['ordinal'], 'error': str(error)})
    candidates = []
    for (idol_id, plan), members in groups.items():
        for main in members:
            for sub in members:
                if main['id'] == sub['id']:
                    continue
                loadout = compose_memories(main['draft'], sub['draft'], stage_id=stage_id,
                    support_bonus=support_bonus, catalog=catalog, idols=idols)
                candidate_id = hashlib.sha256((main['id'] + ':' + sub['id']).encode()).hexdigest()[:24]
                def reference(r):
                    return {k: v for k, v in r.items() if k != 'draft'}
                candidates.append({'id': candidate_id, 'idol_id': idol_id, 'plan': plan,
                    'idol_name': names.get(idol_id, f'角色 {idol_id}'),
                    'main': reference(main), 'sub': reference(sub), 'loadout': loadout.to_dict()})
    return candidates, rejected


def select_finalists(candidates, results, total_top=30, character_top=3):
    """Keep top K plus overlapping mean +/- 2 SE; a screening heuristic, not a proof."""
    ranked = sorted((c for c in candidates if results.get(c['id'], {}).get('status') == 'ok'),
                    key=lambda c: (-results[c['id']]['summary']['mean'], c['id']))
    selected, groups = set(), defaultdict(list)
    for c in ranked:
        groups[c['idol_id']].append(c)
    for rows, count in [(ranked, total_top)] + [(g, character_top) for g in groups.values()]:
        if not rows:
            continue
        cutoff = results[rows[min(count, len(rows)) - 1]['id']]['summary']
        lower = cutoff['mean'] - 2 * (cutoff['standard_error'] or 0)
        for c in rows:
            s = results[c['id']]['summary']
            if s['mean'] + 2 * (s['standard_error'] or 0) >= lower:
                selected.add(c['id'])
    return [c['id'] for c in ranked if c['id'] in selected]


POLICY_ADAPTIVE = 'adaptive-character-v1'
POLICY_BALANCED = 'character-balanced-v2'


def adaptive_finalists(candidates, screen, refined=None, *, character_balanced=False):
    """Fixed screening boundary; bounded promotions use independent refinement evidence."""
    refined = refined or {}
    ranked = sorted((c for c in candidates if screen.get(c['id'], {}).get('status') == 'ok'),
                    key=lambda c: (-screen[c['id']]['summary']['mean'], c['id']))
    if not ranked:
        return [], {}
    groups = defaultdict(list)
    for c in ranked:
        groups[c['idol_id']].append(c)
    representatives = [rows[0] for rows in groups.values()]
    reference = representatives if character_balanced else ranked
    cutoff_rank = min(3 if character_balanced else 30, len(reference))
    cutoff = screen[reference[cutoff_rank-1]['id']]['summary']
    boundary = max(0, cutoff['mean'] - 2*(cutoff['standard_error'] or 0))
    selected, allocation = set(), {}
    for idol, rows in groups.items():
        best = screen[rows[0]['id']]['summary']
        upper = best['mean'] + 2*(best['standard_error'] or 0)
        tier = 'competitive' if upper >= boundary else 'distant' if upper >= boundary*.9 else 'weak'
        if character_balanced and tier != 'competitive':
            tier = 'baseline'
        promoted = False
        if tier != 'competitive':
            for c in rows:
                result = refined.get(c['id'], {})
                if result.get('status') != 'ok':
                    continue
                measured = result['summary']
                original = screen[c['id']]['summary']
                if (measured['mean'] > original['mean'] + 2*(original['standard_error'] or 0)
                    and measured['mean'] + 2*(measured['standard_error'] or 0) >= boundary):
                    tier, promoted = 'competitive', True
                    break
        if tier == 'competitive':
            third = screen[rows[min(3,len(rows))-1]['id']]['summary']
            near = third['mean'] - 2*(third['standard_error'] or 0)
            picks = [c for i,c in enumerate(rows) if i < 3 or
                     screen[c['id']]['summary']['mean'] + 2*(screen[c['id']]['summary']['standard_error'] or 0) >= near][:15]
        else:
            picks = rows[:3 if character_balanced else 2 if tier == 'distant' else 1]
        # Preserve previously tested pairs, while enforcing the total per-character cap.
        prior = [c for c in rows if c['id'] in refined]
        chosen = list(dict.fromkeys([c['id'] for c in prior+picks]))[:15]
        selected.update(chosen)
        allocation[str(idol)] = {'tier': tier, 'groups': len(chosen), 'promoted': promoted,
                                 'screen_boundary': boundary, 'boundary_rank': cutoff_rank,
                                 'boundary_basis': 'characters' if character_balanced else 'combinations'}
    return [c['id'] for c in ranked if c['id'] in selected], allocation


def balanced_finalists(candidates, screen, refined=None):
    return adaptive_finalists(candidates, screen, refined, character_balanced=True)


def upgrade_screening_policy(folder):
    """Explicit migration of a stopped pre-refinement job; preserve all simulation scores."""
    from datetime import datetime, timezone
    from gkshot.simulation_rounds import stability
    folder=Path(folder)
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    state=json.loads((folder/'state.json').read_text(encoding='utf-8'))
    if state.get('state')=='running' or state.get('refine') or any((folder/'refine').glob('*.json')):
        raise ValueError('仅可更新已停止且尚未复测的搜索')
    if manifest['config'].get('refine_policy')==POLICY_BALANCED:
        return
    atomic_json(folder/'policy-backup-v1.json',{'manifest':manifest,'state':state})
    previous=None
    history=[]
    for path in sorted((folder/'snapshots').glob('*.json'),key=lambda p:int(p.stem)):
        old=json.loads(path.read_text(encoding='utf-8'))
        updated=stability(manifest['candidates'],old['results'],previous,balanced_finalists)
        atomic_json(folder/'policy-snapshots-v1'/path.name,old)
        atomic_json(path,dict(updated,results=old['results']))
        previous=updated
        history.append({k:v for k,v in updated.items() if k not in ('ranking','character_best','finalists','allocation')})
    state['round_history']=history
    state['selected']=None
    state['allocation']=previous.get('allocation',{}) if previous else {}
    manifest.setdefault('policy_history',[]).append({'previous':manifest['config'].get('refine_policy'),
        'new':POLICY_BALANCED,'changed_at':datetime.now(timezone.utc).isoformat(),'reason':'用户要求按角色竞争力分配名额，每角色保底3组'})
    manifest['config']['refine_policy']=POLICY_BALANCED
    atomic_json(folder/'state.json',state)
    atomic_json(folder/'manifest.json',manifest)


def run_search(folder, manifest, *, cancel, publish, cache=None, simulator=None, stop_after_round=None):
    """Immutable input snapshot + per-result files; cached successes survive interrupted jobs."""
    folder = Path(folder)
    config = manifest['config']
    simulator = simulator or LocalSimulator(persistent=True)
    state_path = folder / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {
        'screen': {}, 'refine': {}, 'selected': None}
    identity = simulator.identity()
    if identity != manifest['identity']:
        raise ValueError('引擎版本发生变化，请新建搜索，不能混用旧结果')
    state.update(state='running', phase='screen', error=None)
    last_save = 0
    def update(force=False):
        nonlocal last_save
        if force or time.monotonic() - last_save >= 5:
            atomic_json(state_path, state)
            last_save = time.monotonic()
        publish(state)
    selector = balanced_finalists if config.get('refine_policy') == POLICY_BALANCED else adaptive_finalists
    try:
        phases = [('screen', 100, config['seed']), ('refine', 2000, config['seed'] ^ 0x9e3779b9)]
        if config.get('screen_policy') == ROUND_POLICY:
            if not screen_rounds(folder, manifest, state, simulator, cancel, publish, cache, selector, stop_after_round=stop_after_round):
                return state
            phases = phases[1:]
        adaptive = config.get('refine_policy') in (POLICY_ADAPTIVE, POLICY_BALANCED)
        for phase, trials, seed in phases:
            if cancel.is_set():
                state['state'] = 'cancelled'
                break
            if phase == 'refine':
                if adaptive:
                    state['selected'], state['allocation'] = selector(manifest['candidates'], state['screen'], state['refine'])
                else:
                    state['selected'] = sorted(set(select_finalists(manifest['candidates'], state['screen'])) | set(state['refine']))
            rows = [c for c in manifest['candidates'] if phase == 'screen' or c['id'] in set(state['selected'])]
            state.update(phase=phase, total=len(rows), completed=sum(state[phase].get(c['id'], {}).get('status') == 'ok' for c in rows))
            update(True)
            pending = [(c['id'], Loadout.from_dict(c['loadout'])) for c in rows if state[phase].get(c['id'], {}).get('status') != 'ok']
            if not pending:
                continue
            def on_result(entry):
                atomic_json(folder / phase / (entry['name'] + '.json'), entry)
                state[phase][entry['name']] = {k: entry[k] for k in ('status', 'summary', 'error') if k in entry}
                state['completed'] += 1
                state['progress'] = None
                update()
            def progress(name, done, total):
                # Publish only a small progress event; the server owns its synchronized snapshot.
                state['progress'] = {'id': name, 'completed': done, 'trials': total}
                publish(state, progress_only=True)
            report = compare(pending, trials=trials, seed=seed, cache=cache,
                             cancel=cancel, on_result=on_result, progress=progress, simulator=simulator)
            if report['status'] == 'cancelled':
                state['state'] = 'cancelled'
                break
            if adaptive and phase == 'refine':
                expanded, allocation = selector(manifest['candidates'], state['screen'], state['refine'])
                state['allocation'] = allocation
                if set(expanded) - set(state['selected']):
                    phases.append(('refine', 2000, seed))
        else:
            if config.get('screen_policy') == ROUND_POLICY:
                verified = sorted((k for k,v in state['refine'].items() if v['status']=='ok'),
                                  key=lambda k: (-state['refine'][k]['summary']['mean'],k))[:30]
                for h in state.get('round_history', []):
                    snap = json.loads((folder/'snapshots'/f"{h['trials']}.json").read_text(encoding='utf-8'))
                    h['verified_top_covered'] = len(set(verified)&set(snap['finalists']))
                    h['verified_top_count'] = len(verified)
            errors = any(r['status'] != 'ok' for phase in ('screen', 'refine') for r in state[phase].values())
            state['state'] = 'completed-with-errors' if errors else 'complete'
    except Exception as error:
        state.update(state='error', error=str(error))
    finally:
        simulator.close()
        state['progress'] = None
        update(True)
    return state


def ranking(manifest, state, *, phase='refine', view='total', idol_id=None):
    if phase not in ('screen', 'refine') or view not in ('total', 'characters'):
        raise ValueError('未知榜单')
    rows = [{**c, **state.get(phase, {}).get(c['id'], {})} for c in manifest['candidates']
            if state.get(phase, {}).get(c['id'], {}).get('status') == 'ok' and (idol_id is None or c['idol_id'] == idol_id)]
    rows.sort(key=lambda r: (-r['summary']['mean'], r['id']))
    if view == 'characters':
        seen = set()
        rows = [r for r in rows if not (r['idol_id'] in seen or seen.add(r['idol_id']))]
    names = pinned_names()
    for row in rows:
        row['idol_name'] = names.get(row['idol_id'], row['idol_name'])
    if rows:
        top = rows[0]['summary']
        for index, row in enumerate(rows):
            s = row['summary']
            row['rank'] = index + 1
            row['gap'] = top['mean'] - s['mean']
            row['close'] = s['mean'] + 2 * (s['standard_error'] or 0) >= top['mean'] - 2 * (top['standard_error'] or 0)
    return rows


def memory_preview(library, memory_id):
    """Return the exact selected-memory screenshot, never a generic idol/card image."""
    library = Path(library).resolve()
    record = next((r for r in memory_records(library) if r['id'] == memory_id), None)
    if record and record.get('source') == 'native-export':
        preview = {'source': 'native-export', 'ordinal': record['ordinal'], 'memory_id': record['id'],
                   'draft': record['draft'], 'snapshot_sha256': record['snapshot_sha256']}
        try:
            photo = native_photo_preview(library, record['id'], record['ordinal'], record['snapshot_sha256'])
        except Exception:
            # The structured native preview must stay usable even if a photo look-up fails.
            photo = {'photo_status': 'unavailable', 'photo_message': '原始照片暂时无法读取。' + '请在游戏中打开回忆一览后重试。'}
        preview.update(photo)
        return preview
    if not record or not record['capture_id']:
        raise ValueError('此回忆没有可验证的采集来源')
    session_path = next(p / 'session.json' for p in library.parents if (p / 'session.json').is_file())
    session = json.loads(session_path.read_text(encoding='utf-8'))
    entry = next(e for e in session['entries'] if e['capture_id'] == record['capture_id'])
    with closing(sqlite3.connect((library / 'library.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        report = json.loads(db.execute('SELECT report_json FROM memories WHERE memory_id=?', (memory_id,)).fetchone()[0])
    sources = {(str(Path(p['source_path']).resolve()), p['sha256']) for p in report['pages']}
    attempts = {i.get('metadata', {}).get('attempt') for i in entry['images']
                if (str((session_path.parent / i['path']).resolve()), i['sha256']) in sources}
    images = [i for i in entry['images'] if i['role'] == 'list-selected' and i.get('metadata', {}).get('attempt') in attempts]
    if len(attempts) != 1 or not images:
        raise ValueError('此回忆缺少同次采集的封面截图')
    source = images[-1]
    path = (session_path.parent / source['path']).resolve()
    if not path.is_relative_to(session_path.parent / 'raw'):
        raise ValueError('截图不属于此采集会话')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != source['sha256'] or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('原始截图校验失败')
    return {'ordinal': record['ordinal'], 'image': 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')}
