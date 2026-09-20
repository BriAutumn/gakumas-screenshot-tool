"""Compare a rule-only replay with a separately curated reference library.

The reference is an evaluation input only: this module never runs a recognizer,
writes a library, or copies a reference value into the candidate result.
"""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from gkshot.memory_library import assess_automatic
from gkshot.catalog import Catalog


def has_assistant_evidence(value):
    if isinstance(value,dict):
        return any(str(k).startswith('vision_') or has_assistant_evidence(v) for k,v in value.items())
    if isinstance(value,list):return any(has_assistant_evidence(v) for v in value)
    return isinstance(value,str) and value.startswith(('assistant-vision-','assistant-direct-'))


def records(library):
    with closing(sqlite3.connect((Path(library).resolve()/'library.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        rows=db.execute('select memory_id, report_json, review_json from memories').fetchall()
    return {mid:(json.loads(report),json.loads(review) if review else None) for mid,report,review in rows}


def tokens(value):
    if value is None:return None
    return sorted(value.split('e')) if value else []


def fields(report):
    draft=report['draft']
    return {**{k:draft.get(k) for k in ('p_idol_id','parameters','item_ids','card_ids')},
            'customizations':[tokens(t) for t in draft.get('customizations',[])]}


def evaluate(candidate_root,reference_root):
    candidates=records(candidate_root);reference=records(reference_root);catalog=Catalog()
    def source_keys(report):
        return {(str(Path(p['source_path']).resolve()).casefold(),p['sha256']) for p in report.get('pages',[])}
    index={}
    for mid,(report,_) in candidates.items():
        for key in source_keys(report):index.setdefault(key,set()).add(mid)
    rows=[];used=set()
    for mid,(truth,_) in reference.items():
        found=set().union(*(index.get(key,set()) for key in source_keys(truth)))
        row={'reference_memory_id':mid,'status':'missing'}
        if len(found)>1:row['status']='ambiguous-source'
        elif found:
            cid=next(iter(found));candidate,review=candidates[cid]
            row['candidate_memory_id']=cid
            if cid in used:raise ValueError('Candidate record reused for two owned memories')
            used.add(cid)
            if review is not None or has_assistant_evidence(candidate):
                row['status']='non-rule-evidence'
            else:
                status=assess_automatic(candidate,catalog)
                row['unresolved']=candidate['automatic_check']['unresolved']
                row['differences']=[k for k,v in fields(truth).items() if fields(candidate).get(k)!=v]
                row['status']=('ready-correct' if not row['differences'] else 'ready-disagreement') if status=='auto-resolved' else 'unresolved'
        rows.append(row)
    from collections import Counter
    counts=dict(Counter(r['status'] for r in rows));total=len(rows);correct=counts.get('ready-correct',0)
    wrong=counts.get('ready-disagreement',0)
    return {'reference_memories':total,'candidate_memories':len(candidates),'counts':counts,
            'rule_only_correct_complete':correct,'fraction':correct/total if total else 0,
            'accepted_agreement':correct/(correct+wrong) if correct+wrong else None,
            'unmatched_candidate_records':len(candidates)-len(used),
            'target_count':(total*9+9)//10,
            'target_met':bool(total and correct/total>=.9 and not wrong and not counts.get('non-rule-evidence')),
            'scope':'Agreement with curated references from the same captured batch; not unseen-data accuracy.',
            'rows':rows}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('candidate_library');p.add_argument('reference_library');p.add_argument('output')
    args=p.parse_args()
    result=evaluate(args.candidate_library,args.reference_library)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=False,indent=2))
