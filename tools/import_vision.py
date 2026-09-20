"""Apply authored visual decisions to a NEW library copy; keep the source intact.

python tools/import_vision.py SOURCE_LIBRARY NEW_LIBRARY RESULTS_JSON ...
"""
import copy,json,shutil,sys,hashlib,sqlite3
from pathlib import Path
from collections import Counter
from contextlib import closing
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from gkshot.catalog import Catalog
from gkshot.memory_library import MemoryLibrary
from gkshot.recognition.vision_evidence import add_visual_detail

def apply(source,target,batches,*,field_mode=False):
    if field_mode:
        from gkshot.recognition.vision_fields import add as importer, METHOD as method
    else:
        from gkshot.recognition.vision_evidence import add_visual_detail as importer, METHOD as method
    source=Path(source).resolve();target=Path(target).resolve()
    if target.exists() or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('目标必须是尚不存在、与原库互不嵌套的独立目录。')
    initial_hash=hashlib.sha256((source/'library.sqlite3').read_bytes()).hexdigest()
    # Copy immutable archived images; SQLite's backup API gives a consistent
    # snapshot even when the source library has an open connection/WAL.
    target.mkdir(parents=True)
    shutil.copytree(source/'sources',target/'sources')
    with closing(sqlite3.connect(str(source/'library.sqlite3'))) as original:
        with closing(sqlite3.connect(str(target/'library.sqlite3'))) as snapshot:
            original.backup(snapshot)
    library=MemoryLibrary(target);catalog=Catalog()
    old={r['memory_id']:r for r in library.list()};applied=[]
    for path in batches:
        data=json.loads(Path(path).read_text(encoding='utf8'))
        for row in data['decisions']:
            result=importer(library,row['memory_id'],row,revision=data['catalog_revision'],catalog=catalog)
            applied.append(result)
            assert importer(library,row['memory_id'],row,revision=data['catalog_revision'],catalog=catalog)['reason']=='already-attached'
    new={r['memory_id']:r for r in library.list()}
    assert old.keys()==new.keys()
    allowed={(r['memory_id'],r['field'] if field_mode else f"customizations.{r['slot']}"):r['value'] if field_mode else r['token'] for r in applied if r['applied']}
    for mid,before in old.items():
        after=new[mid];expected=copy.deepcopy(before['report']['draft'])
        for (target_id,field),value in allowed.items():
            if mid==target_id:
                kind,key=field.split('.');key=key if kind=='parameters' else int(key)
                if not field_mode:assert expected[kind][key] is None
                expected[kind][key]=value
        assert expected==after['report']['draft'],mid
        assert before['review']==after['review'],mid
        if before['status']=='auto-resolved':assert after['status']=='auto-resolved',mid
    def stats(rows):
        return dict(total=len(rows),statuses=dict(Counter(r['status'] for r in rows.values())),
                    unknown_customizations=sum(t is None for r in rows.values() for t in r['report']['draft']['customizations']))
    assert hashlib.sha256((source/'library.sqlite3').read_bytes()).hexdigest()==initial_hash
    report=dict(source=str(source),target=str(target),source_db_sha256=initial_hash,before=stats(old),after=stats(new),applied=applied,
                method=method,independent_accuracy_measured=False,source_unchanged=True)
    (target.parent/'vision-import-comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in report.items() if k!='applied'},ensure_ascii=False,indent=2))
    return report

if __name__=='__main__':
    if len(sys.argv)<4:raise SystemExit(__doc__)
    apply(sys.argv[1],sys.argv[2],sys.argv[3:])
