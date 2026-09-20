"""Cross-check exact card labels on distinct, already grouped overview pages."""
import copy
from gkshot.recognition.card_names import canonical_name


def accepted(card):
    p=card.get('label_consensus',{});reads=p.get('observations',[])
    if (p.get('rule')!='two-overview-card-labels-v1' or p.get('id')!=card.get('id')
            or p.get('slot')!=card.get('slot') or len(reads)<2
            or len({r.get('sha256') for r in reads})<2):return False
    for r in reads:
        read=r.get('read',{});best=r.get('best_distance',0);distance=r.get('target_distance',99999)
        if (not r.get('sha256') or r.get('slot')!=card['slot'] or (read.get('score') or 0)<.8
                or canonical_name(read.get('text'))!=canonical_name(p.get('name'))
                or r.get('target_id')!=card['id'] or distance>=3500 or distance>best*1.1):return False
        mark=r.get('plus_distance',4000)
        if not (p.get('upgraded') is True and mark<3000 or p.get('upgraded') is False and mark>5000):return False
    return True


def refine(pages,catalog):
    from gkshot.recognition.native_resolution import card_accepted
    names={}
    for entity,c in catalog.skill_cards.items():names.setdefault(canonical_name(c['name']),[]).append(entity)
    for slot in range(6):
        cards=[(p,c) for p in pages for c in p['cards'] if c['slot']==slot]
        if len(cards)<2 or any(card_accepted(c) for _,c in cards):continue
        observed={canonical_name(c.get('label_read',{}).get('text')) for _,c in cards}
        if len(observed)!=1:continue
        ids=names.get(next(iter(observed)),[])
        if len(ids)!=1:continue
        entity=ids[0];readings=[]
        for page,card in cards:
            candidates=card.get('template_candidates',[])
            target=next((c for c in candidates if c['id']==entity),None)
            if not target:break
            readings.append({'sha256':page['sha256'],'slot':slot,'read':copy.deepcopy(card['label_read']),
                'target_id':entity,'target_distance':target['distance'],'best_distance':candidates[0]['distance'],
                'plus_distance':card.get('plus_glyph_distance',4000)})
        else:
            proof={'rule':'two-overview-card-labels-v1','id':entity,'slot':slot,
                'name':catalog.skill_cards[entity]['name'],'upgraded':bool(catalog.skill_cards[entity]['upgraded']),
                'observations':readings}
            if accepted({'id':entity,'slot':slot,'label_consensus':proof}):
                for page,card in cards:
                    card['identity_before_consensus']={'id':card['id'],'method':card['method']}
                    card.update(id=entity,method='multi-page-card-label',label_consensus=proof)
