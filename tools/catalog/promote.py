"""Install generated rules only for explicitly image-validated card IDs.

Usage: python tools/catalog/promote.py 99 167 297
The generator has no card-ID-specific behavior; this is a validation gate.
"""
import json,sys,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from gkshot.catalog import Catalog
from gkshot.recognition.catalog_candidates import load_tree,compile_card
from gkshot.recognition.card_detail import rule_fingerprint
catalog=Catalog();tree=load_tree();path=Path('configs/recognition/card-detail-rules.json')
profiles=json.loads(path.read_text(encoding='utf8'))
for card_id in map(int,sys.argv[1:]):
 if any(r['card_id']==card_id for r in profiles['rules']):raise ValueError('Refusing to overwrite an existing rule')
 card=catalog.skill_cards[card_id];effects=[catalog.customizations[int(i)] for i in card['availableCustomizations'].split(',')]
 cases=compile_card(card_id,catalog,tree)
 profiles['rules'].append(dict(card_id=card_id,catalog_fingerprint=rule_fingerprint(card,effects),
  complete_text_cases=cases,min_line_score=.7,fallback_scales=[1.5,1.75,2],small_fragments=True,green_ocr_border=2,
  corner_cost_required=any('corner_cost' in c for c in cases),
  generated_from={'tool':'tools/catalog/promote.py','tree_sha256':hashlib.sha256(Path('assets/recognition/catalog-effects.json').read_bytes()).hexdigest(),
  'generator_sha256':hashlib.sha256(Path('src/gkshot/recognition/catalog_candidates.py').read_bytes()).hexdigest()},
  validation='Catalog-derived complete candidates. See docs/catalog-candidates-validation.md; unpictured states are not claimed image-validated.'))
path.write_text(json.dumps(profiles,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
