"""Round-robin screening with equal-sample ranking snapshots."""
import json
from pathlib import Path
from gkshot.models import Loadout
from gkshot.simulation import atomic_json, compare, summary

POLICY = 'five-rounds-20-v1'

def stability(candidates, results, previous=None, select=None):
    ranked=sorted(results,key=lambda k:(-results[k]['summary']['mean'],k))
    best={}
    idols={c['id']:c['idol_id'] for c in candidates}
    for k in ranked:
        idol=idols[k]
        best.setdefault(str(idol),k)
    selected,allocation=select(candidates,results) if select else ([],{})
    snapshot={'trials':results[ranked[0]]['summary']['trials'], 'ranking':ranked,
              'character_best':best,'finalists':selected,'allocation':allocation}
    if previous:
        old=previous['ranking'];positions={k:i for i,k in enumerate(ranked)}
        top=old[:30]; retained=len(set(top)&set(ranked[:30]))
        before=set(previous['finalists']);after=set(selected)
        snapshot['changes']={'top_retained':retained,'top_count':len(top),
            'top_mean_rank_shift':sum(abs(positions[k]-i) for i,k in enumerate(top))/len(top),
            'character_best_changed':sum(previous['character_best'].get(k)!=v for k,v in best.items()),
            'character_count':len(best),'finalists_added':len(after-before),'finalists_removed':len(before-after)}
    return snapshot

def screen_rounds(folder, manifest, state, simulator, cancel, publish, cache, select, *, stop_after_round=None):
    if stop_after_round is not None and (type(stop_after_round) is not int or not 1 <= stop_after_round <= 5):
        raise ValueError('轮次上限须为1至5的整数')
    folder=Path(folder); rows=manifest['candidates']; previous_snapshot=None
    state.setdefault('round_history',[])
    finished=max((h['trials'] for h in state['round_history']),default=0)//20
    if cancel.is_set():
        state['state']='cancelled';return False
    if stop_after_round is not None and finished >= stop_after_round:
        state['state']='round-wait';return False
    for index in range(1,6):
        if index<=finished:
            previous_snapshot=json.loads((folder/'snapshots'/f'{index*20}.json').read_text(encoding='utf-8'))
            continue
        seed=(manifest['config']['seed']+(index-1)*0x6d2b79f5)&0xffffffff
        destination=folder/'rounds'/str(index)
        completed={}
        for row in rows:
            path=destination/(row['id']+'.json')
            if path.exists():
                entry=json.loads(path.read_text(encoding='utf-8'))
                if entry['status']=='ok' and len(entry['simulation']['scores'])==index*20 and entry['loadout']==row['loadout']:
                    completed[row['id']]=entry
        state.update(phase='screen',screen_round=index,total=len(rows),completed=len(completed),progress=None)
        publish(state)
        def save(entry):
            key=entry['name']
            if entry['status']!='ok':
                return
            previous=json.loads((folder/'rounds'/str(index-1)/(key+'.json')).read_text(encoding='utf-8')) if index>1 else None
            scores=(previous['simulation']['scores'] if previous else [])+entry['simulation']['scores']
            entry['simulation']['rng_calls'] += previous['simulation']['rng_calls'] if previous else 0
            entry['simulation']['seed']=manifest['config']['seed']
            entry['simulation']['rng']='upstream-mulberry32-five-rounds-v1'
            entry['simulation']['scores']=scores
            entry['simulation']['trials']=len(scores)
            entry['simulation']['round_seeds']=(previous['simulation']['round_seeds'] if previous else [])+[seed]
            entry['summary']=summary(scores)
            atomic_json(destination/(key+'.json'),entry)
            completed[key]=entry
            state['completed']=len(completed)
            if index==1:
                state['screen'][key]={'status':'ok','summary':entry['summary']}
                state['detail_round']=1
            publish(state)
        def progress(key,done,total):
            state['progress']={'id':key,'completed':done,'trials':total}
            publish(state,progress_only=True)
        pending=[(c['id'],Loadout.from_dict(c['loadout'])) for c in rows if c['id'] not in completed]
        if pending:
            report=compare(pending,trials=20,seed=seed,cache=cache,cancel=cancel,simulator=simulator,on_result=save,progress=progress)
            if report['status']=='cancelled':
                state['state']='cancelled';return False
            if len(completed)!=len(rows):
                state.update(state='error',error='本轮有组合失败，已完成部分保留；继续可重试失败项。')
                state['round_errors']=[{'id':r['name'],'error':r['error']} for r in report['results'] if r['status']!='ok']
                return False
        if cancel.is_set():
            state['state']='cancelled';return False
        results={k:{'status':'ok','summary':v['summary']} for k,v in completed.items()}
        snap=stability(rows,results,previous_snapshot,select)
        atomic_json(folder/'snapshots'/f'{index*20}.json',dict(snap,results=results))
        previous_snapshot=snap
        # Commit the round only after every candidate has equal cumulative trials.
        state.update(screen=results,detail_round=index,progress=None,round_errors=[],allocation=snap['allocation'])
        compact={k:v for k,v in snap.items() if k not in ('ranking','character_best','finalists','allocation')}
        state['round_history']=[h for h in state['round_history'] if h['trials']<index*20]+[compact]
        atomic_json(folder/'state.json',state);publish(state)
        if cancel.is_set():
            state['state']='cancelled';return False
        if stop_after_round is not None and index >= stop_after_round:
            state['state']='round-wait';return False
    return True
