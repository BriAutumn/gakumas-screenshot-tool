"""Offline, read-only experiment; does not modify recognition rules or memories."""
import json,sys,hashlib
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from gkshot.catalog import Catalog
from gkshot.recognition.catalog_candidates import load_tree,compile_card,Unsupported,candidates
from gkshot.recognition.card_detail import match_rule,rule_fingerprint
catalog=Catalog();tree=load_tree();rules=[];unsupported={};candidate_count=0
for card in catalog.skill_cards.values():
 if not card['availableCustomizations']:continue
 try:
  candidate_count+=sum(1 for _ in candidates(card['id'],catalog,tree))
  cases=compile_card(card['id'],catalog,tree)
 except (Unsupported,KeyError,TypeError) as e:
  unsupported[card['id']]=str(e);continue
 effects=[catalog.customizations[int(i)] for i in card['availableCustomizations'].split(',')]
 rules.append(dict(card_id=card['id'],catalog_fingerprint=rule_fingerprint(card,effects),min_line_score=.7,
                   complete_text_cases=cases,corner_cost_required=any('corner_cost' in c for c in cases)))
root=Path(sys.argv[1]);output=Path(sys.argv[2]);output.mkdir(parents=True,exist_ok=True)
probes=[]
for path in root.glob('*-card-*.json'):
 d=json.loads(path.read_text(encoding='utf8'))
 if d.get('card_id') not in {r['card_id'] for r in rules}:continue
 matches=[match_rule(a['reads'],d['card_id'],catalog,{'rules':rules},d.get('corner_evidence')) for a in d.get('attempts',[])]
 probes.append({'report':path.name,'card_id':d['card_id'],'old_status':d['status'],'old_token':d.get('token'),'matches':matches})
report={'structural_candidate_count':candidate_count,'renderable_cards':len(rules),'renderable_card_ids':[r['card_id'] for r in rules],
        'unsupported':unsupported,'probes':probes,'note':'Text-only probe, NOT accepted memory evidence. Original image reread and existing green/owner guards still required.'}
(output/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
(output/'generated-rules.json').write_text(json.dumps({'rules':rules},ensure_ascii=False,indent=2),encoding='utf8')
print('Structural candidates',candidate_count,'renderable cards',len(rules),'unsupported',len(unsupported))
print('Existing text matches',Counter(p['card_id'] for p in probes if any(m['status']=='matched' for m in p['matches'])))
print('New text matches',Counter(p['card_id'] for p in probes if p['old_status']!='matched' and any(m['status']=='matched' for m in p['matches'])))
