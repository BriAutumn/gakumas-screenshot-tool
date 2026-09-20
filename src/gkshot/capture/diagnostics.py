"""Describe saved batch issues, preserving source/evidence links and field indexes."""
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from gkshot.capture.store import atomic_json
from gkshot.catalog import Catalog
from gkshot.memory_library import validate_memory


def write_diagnostics(directory, *, library_root=None):
    directory=Path(directory)
    batch=json.loads((directory/'results.json').read_text(encoding='utf8'))
    catalog=Catalog()
    records={}
    database=Path(library_root or directory/'memory-library')/'library.sqlite3'
    if database.is_file():
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            records={mid:json.loads(report) for mid,report in db.execute('SELECT memory_id, report_json FROM memories')}
    issues=[]
    field_counts=Counter()
    category_counts=Counter()
    card_counts=Counter()
    for result in batch['results']:
        ordinal=result['ordinal']
        report=records.get(result.get('memory_id'),{})
        draft=report.get('draft',{})
        details=[]
        for path in directory.glob(f'{ordinal:05d}-card-*.json'):
            detail=json.loads(path.read_text(encoding='utf8'))
            details.append((int(path.name.split('-')[2])-1,path,detail))
        for field in result.get('unresolved',[]):
            field_counts[field]+=1
            root,_,suffix=field.partition('.')
            slot=int(suffix) if suffix.isdigit() else None
            related=[(path,d) for s,path,d in details if s==slot] if slot is not None else []
            card_id=(draft.get('card_ids',[None]*6)[slot] if root in ('customizations','card_ids') and slot is not None else None)
            category={'item_ids':'item-identity-or-upgrade','parameters':'number-ocr',
                      'card_ids':'card-identity','customizations':'customization'}.get(root,'validation')
            if root=='customizations':
                statuses={d['status'] for _,d in related}
                if 'unsupported' in statuses: category='missing-detail-rule'
                elif 'conflict' in statuses: category='detail-conflict'
                elif related: category='detail-ocr-or-incomplete'
                else: category='missing-or-unbound-detail'
            category_counts[category]+=1
            if card_id is not None: card_counts[str(card_id)]+=1
            sources=[p['source_path'] for p in report.get('pages',[])]
            row={'ordinal':ordinal,'capture_id':result['capture_id'],'field':field,'slot_index':slot,
                 'category':category,'card_id':card_id,
                 'card_name':catalog.skill_cards.get(card_id,{}).get('name'),
                 'sources':sources,'detail_reports':[str(path.resolve()) for path,_ in related],
                 'evidence':report.get('evidence',{}).get(field,[])}
            if field=='loadout-validation' and draft:
                try: validate_memory(draft,catalog)
                except (ValueError,TypeError,KeyError) as error: row['reason']=str(error)
            issues.append(row)
        for error in result.get('errors',[]):
            issues.append({'ordinal':ordinal,'capture_id':result['capture_id'],
                           'category':'processing-error','error':error})
    summary={'run_id':batch['run_id'],'processed':len(batch['results']),
             'imported':sum(bool(r.get('memory_id')) for r in batch['results']),
             'ready':sum(r.get('memory_status') in ('auto-resolved','reviewed') for r in batch['results']),
             'failed':sum(r['status']=='failed' for r in batch['results']),
             'statuses':dict(Counter(r['status'] for r in batch['results'])),
             'unresolved_fields':dict(field_counts.most_common()),
             'issue_categories':dict(category_counts.most_common()),
             'affected_card_fields':dict(card_counts.most_common()),
             'accuracy_measured':False,'issues':issues}
    atomic_json(directory/'diagnostics.json',summary)
    return summary
