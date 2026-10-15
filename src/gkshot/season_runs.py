"""Server-managed finite season round-robin over three contest stages.

This module only orchestrates the existing ``run_search`` over the pinned engine;
it never rewrites simulation logic, recognition rules or user data.  One immutable
group record per season run is persisted under ``<USER_ROOT>/work/simulation/season-runs``.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from gkshot.simulation import atomic_json
from gkshot.simulation_queue import DONE, next_stage
from gkshot.simulation_search import run_search

SCHEMA_VERSION = 1
# A child only counts as finished for the group when it is in DONE.
YIELD = 'round-wait'
FAILED = 'error'
ACTIVE = 'running'
RESTARTABLE = {'queued', 'paused', 'interrupted', 'error'}
FINISHED = {'complete', 'completed-with-errors'}


def now():
    return datetime.now(timezone.utc).isoformat()


def contest_seasons(catalog):
    """Numeric contest seasons -> ordered contest stages.  Non-contest types are never runnable."""
    seasons = {}
    for stage in catalog.stages.values():
        if stage.get('type') != 'contest':
            continue
        season = stage.get('season')
        if isinstance(season, bool) or not isinstance(season, int) or season <= 0:
            continue
        seasons.setdefault(season, []).append(stage)
    return {season: sorted(rows, key=lambda s: s['stage']) for season, rows in seasons.items()}


def season_stage_rows(catalog, season):
    return contest_seasons(catalog).get(season, [])


def validate_season(catalog, season, stages=None):
    """Return ordered stages for a runnable season, or raise ValueError before anything is created."""
    if isinstance(season, bool) or not isinstance(season, int):
        raise ValueError('赛季须为整数')
    rows = season_stage_rows(catalog, season)
    if not rows:
        raise ValueError('该赛季没有可运行的普通竞技场舞台；活动与联赛不在此工具范围内。')
    if len(rows) != 3:
        raise ValueError(f'赛季轮转要求恰好3个普通竞技场舞台，第{season}赛季为{len(rows)}个，不能运行。')
    ids = [row['id'] for row in rows]
    if stages is not None:
        if not isinstance(stages, list) or any(type(x) is not int for x in stages) or stages != ids:
            raise ValueError('赛季舞台列表与数据目录不一致，请重新选择赛季。')
    return rows


def child_summary(state):
    """Small, immutable-safe progress view of one child search state.json."""
    trials = max((h.get('trials', 0) for h in state.get('round_history', [])), default=0)
    if not trials and state.get('phase') == 'refine':
        trials = 100
    return {'state': state.get('state', 'pending'), 'phase': state.get('phase'),
            'trials': trials, 'completed': state.get('completed', 0), 'total': state.get('total', 0)}


def new_group(catalog, *, season, library, support_bonus, seed, refine_policy, screen_policy,
              stages=None, group_id=None):
    rows = validate_season(catalog, season, stages)
    return {'schema_version': SCHEMA_VERSION, 'group_id': group_id or uuid4().hex,
            'created_at': now(), 'updated_at': now(), 'state': 'queued', 'error': None,
            'active_stage': None,
            'config': {'season': season, 'library': library, 'support_bonus': support_bonus,
                       'seed': seed, 'stages': [row['id'] for row in rows],
                       'refine_policy': refine_policy, 'screen_policy': screen_policy},
            'stages': [{'stage_id': row['id'], 'stage': row['stage'], 'search_id': None,
                        'state': 'pending', 'phase': None, 'trials': 0, 'completed': 0, 'total': 0}
                       for row in rows],
            'errors': []}


def blocked_stages(group):
    return {child['stage_id'] for child in group['stages'] if child.get('state') == FAILED}


def save_group(folder, group):
    group['updated_at'] = now()
    atomic_json(Path(folder) / 'group.json', group)
    return group


def load_group(folder):
    return json.loads((Path(folder) / 'group.json').read_text(encoding='utf-8'))


def stage_rows(group):
    return [{'stage_id': child['stage_id'], 'state': child.get('state', 'pending'),
             'trials': child.get('trials', 0)} for child in group['stages']]


def run_group(folder, group, *, cancel, publish, simulator_factory, search_root, cache=None, publish_search=None):
    """Serially run the lowest completed round of the lowest stage, then refine in stage order.

    ``publish(group)`` mirrors an in-memory snapshot for the HTTP server; ``group.json`` is
    written at stage boundaries so a restart can reconcile children from their own state.json.
    """
    folder = Path(folder)
    save_group(folder, group)
    publish(group)
    try:
        group['state'] = ACTIVE
        group['error'] = None
        group['active_stage'] = None
        save_group(folder, group)
        publish(group)
        while not cancel.is_set():
            choice = next_stage(stage_rows(group), blocked_stages(group))
            if choice is None:
                break
            stage_id, limit = choice
            child = next(child for child in group['stages'] if child['stage_id'] == stage_id)
            search_folder = Path(search_root) / child['search_id']
            manifest = json.loads((search_folder / 'manifest.json').read_text(encoding='utf-8'))
            group['active_stage'] = stage_id
            save_group(folder, group)
            publish(group)

            def publish_child(state, progress_only=False):
                if publish_search:
                    publish_search(child['search_id'], state, progress_only)
                if progress_only:
                    return
                child.update(child_summary(state))
                publish(group)

            try:
                state = run_search(search_folder, manifest, cancel=cancel, publish=publish_child,
                                   cache=cache, simulator=simulator_factory(), stop_after_round=limit)
            except Exception as error:
                state = {'state': 'error', 'error': str(error), 'round_history': []}
                publish_child(state)
            child.update(child_summary(state))
            group['active_stage'] = None
            save_group(folder, group)
            publish(group)
            if cancel.is_set():
                break
            if state.get('state') != YIELD and state.get('state') not in DONE:
                child['state'] = FAILED
                group['errors'] = list(group.get('errors') or []) + [
                    {'stage_id': stage_id, 'state': state.get('state'), 'error': state.get('error')}]
        if cancel.is_set():
            group['state'] = 'paused'
        else:
            statuses = {child.get('state') for child in group['stages']}
            if FAILED in statuses:
                group['state'] = 'completed-with-errors'
            elif statuses <= DONE:
                group['state'] = 'completed-with-errors' if 'completed-with-errors' in statuses else 'complete'
            else:
                group['state'] = 'interrupted'
    except Exception as error:  # pragma: no cover - defensive; surfaced through group.error
        group['state'] = FAILED
        group['error'] = str(error)
    finally:
        group['active_stage'] = None
        save_group(folder, group)
        publish(group)
    return group
