"""Read-only completeness check against every position in a capture ledger."""
import json,sys,hashlib,sqlite3
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from gkshot.catalog import Catalog
from gkshot.memory_library import assess_automatic,validate_memory
from gkshot.simulation_search import memory_records
from gkshot.simulation import read_ready_memory

def verify(library,session_path,exclusion_path):
    library=Path(library).resolve();session_path=Path(session_path).resolve()
    session=json.loads(session_path.read_text(encoding='utf8'));exclusions=json.loads(Path(exclusion_path).read_text(encoding='utf8'));catalog=Catalog()
    automatic_excluded={e['ordinal'] for e in session['entries'] if e['status']=='skipped-production-only'}
    audited=set()
    for r in exclusions:
        entry=next(e for e in session['entries'] if e['ordinal']==r['ordinal'])
        assert entry['page_index']==r['page_index'] and entry['cell_index']==r['cell_index']
        page=session['pages'][entry['page_index']];assert page['source']==r['source']
        path=session_path.parent/r['source']['path'];assert hashlib.sha256(path.read_bytes()).hexdigest()==r['source']['sha256']
        assert r['status']=='production-only' and r['observation'];audited.add(r['ordinal'])
    with sqlite3.connect((library/'library.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        rows=db.execute('select memory_id,status,report_json,review_json from memories').fetchall()
    for mid,status,raw,review in rows:
        report=json.loads(raw);assert not review
        assert status=='auto-resolved' and assess_automatic(report,catalog)=='auto-resolved'
        validate_memory(report['draft'],catalog);assert read_ready_memory(library,mid)==report['draft']
    records=memory_records(library);ordinals=[r['ordinal'] for r in records]
    assert None not in ordinals and len(ordinals)==len(set(ordinals))==len(rows)
    expected={e['ordinal'] for e in session['entries']}
    excluded=automatic_excluded|audited
    assert not set(ordinals)&excluded and set(ordinals)|excluded==expected
    return dict(total_positions=len(expected),usable_memories=len(rows),production_only=len(excluded),
                originally_excluded=len(automatic_excluded),visually_audited_exclusions=sorted(audited),
                unresolved_memories=0,unknown_customizations=0,unaccounted_positions=[],
                simulator_readable=len(records),all_catalogs_valid=True,
                evidence_methods=dict(Counter(e.get('method','recognizer') for _,_,raw,_ in rows
                    for es in json.loads(raw).get('evidence',{}).values() for e in es)))

if __name__=='__main__':
    if len(sys.argv)!=4:raise SystemExit('verify_memory_completion.py LIBRARY SESSION_JSON EXCLUSION_JSON')
    print(json.dumps(verify(*sys.argv[1:]),ensure_ascii=False,indent=2))
