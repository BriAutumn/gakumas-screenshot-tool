"""Resolve an uncertain slot using its capture-bound tooltip and visible card."""
import copy
from gkshot.recognition.card_names import canonical_name


def support_for(card, entity, catalog):
    name=canonical_name(catalog.skill_cards[entity]['name']).rstrip('+')
    for key in ('label_read','native_label_read'):
        read=card.get(key) or {}
        if (read.get('score') or 0)>=.7 and canonical_name(read.get('text')).rstrip('+・;:')==name:
            distance=card.get('plus_glyph_distance',4000)
            if (bool(catalog.skill_cards[entity]['upgraded']) and distance<3000
                    or not catalog.skill_cards[entity]['upgraded'] and distance>5000):
                return {'kind':'label-and-mark','read':read,'name':catalog.skill_cards[entity]['name'],
                        'upgraded':bool(catalog.skill_cards[entity]['upgraded']),'distance':distance}
    from gkshot.recognition.native_resolution import variant_accepted
    if variant_accepted(card.get('native_retry',{}),entity):
        return {'kind':'native-art','evidence':card['native_retry']}
    score=max((c.get('score',0) for c in card.get('candidates',[]) if c['id']==entity),default=0)
    if score>=.2:return {'kind':'model-candidate','id':entity,'score':score}
    return None


def valid_support(s,entity):
    if not s:return False
    if s.get('kind')=='model-candidate':return s.get('id')==entity and s.get('score',0)>=.2
    if s.get('kind')=='native-art':
        from gkshot.recognition.native_resolution import variant_accepted
        return variant_accepted(s.get('evidence',{}),entity)
    if s.get('kind')=='label-and-mark':
        read=s.get('read',{})
        return ((read.get('score') or 0)>=.7 and canonical_name(read.get('text')).rstrip('+・;:')==canonical_name(s.get('name')).rstrip('+')
                and (s.get('upgraded') is True and s.get('distance',4000)<3000
                     or s.get('upgraded') is False and s.get('distance',4000)>5000))
    return False


def accepted(card):
    p=card.get('bound_identity',{});entity=card.get('id');title=p.get('title',{})
    return (entity is not None and p.get('rule')=='capture-bound-card-title-v1' and p.get('id')==entity
            and p.get('slot')==card.get('slot') and p.get('owner_slot')==card.get('slot')
            and p.get('header_rms',999)>-1 and p.get('header_rms',999)<=15
            and p.get('background_rms',999)<=15 and bool(p.get('source_sha256')) and bool(p.get('owner_sha256'))
            and title.get('rule')=='two-tooltip-title-reads-v1' and title.get('card_id')==entity
            and title.get('consistent') is True and len(title.get('reads',[]))==2
            and all(r.get('score',0)>=.7 and canonical_name(r.get('text'))==canonical_name(p.get('name')) for r in title['reads'])
            and valid_support(p.get('page_support'),entity) and valid_support(p.get('owner_support'),entity))


def refine(report,session,images,scanner,tooltip):
    from gkshot.recognition.native_resolution import card_accepted
    unresolved={slot for slot in range(6) if report['draft']['card_ids'][slot]!=0
                and not any(card_accepted(c) for p in report['pages'] for c in p['cards'] if c['slot']==slot)}
    cache={};proposals={};header=None
    for index,image in enumerate(images):
        if image['role']!='card-tooltip' or image['slot']-1 not in unresolved:continue
        slot=image['slot']-1
        observations=[c for p in report['pages'] for c in p['cards'] if c['slot']==slot]
        if not observations or image['metadata'].get('binding_check')!='same-header-and-overview-before-and-after':continue
        before=[i for i in images[:index] if i['role']=='card-before' and i['slot']==slot+1
                and i['metadata'].get('try')==image['metadata'].get('try')]
        if not before:continue
        try:
            owner_path=session.source(before[-1])
            detail=tooltip.scan(session.source(image),expected_card_id=None,client_area=True,owner_path=owner_path)
            cache[index]=detail
            entity=detail.get('card_id');title=detail.get('identity_evidence',{})
            if entity is None or not title.get('consistent'):continue
            if any(canonical_name(str(detail['identity'][k]))!=canonical_name(str(report['identity'][k])) for k in ('title','idol_name','power')):continue
            ownership=detail.get('ownership_evidence') or {}
            if ownership.get('sha256')!=before[-1]['sha256'] or ownership.get('background_rms',999)>15:continue
            if header is None:
                from pathlib import Path
                overview,_=scanner.normalize(Path(report['pages'][0]['source_path']),client_area=True)
                header=scanner.np.asarray(scanner.signature(overview,scanner.layout['header_signature']),dtype=float)
            observed=scanner.np.asarray(detail['header_signature'],dtype=float)
            if observed.shape!=header.shape:continue
            rms=float(scanner.np.sqrt(((header-observed)**2).mean()))
            if rms>15:continue
            owner,_=scanner.normalize(owner_path,client_area=True)
            boxes=[box for number,box in scanner.boxes(owner) if number==slot]
            if len(boxes)!=1:continue
            box=boxes[0];x1,y1,x2,y2=box
            owner_card=scanner.card(owner.crop(box))
            bottom=min(y2+52,owner.height-scanner.layout['navigation_exclusion_bottom'])
            if bottom-y2>=40:owner_card=scanner.verify_card_label(owner_card,owner.crop((max(0,x1-5),y2,min(owner.width,x2+15),bottom)))
            owner_support=support_for(owner_card,entity,scanner.catalog)
            page_support=next((s for c in observations if (s:=support_for(c,entity,scanner.catalog))),None)
            if not owner_support or not page_support:continue
            proof={'rule':'capture-bound-card-title-v1','id':entity,'name':scanner.catalog.skill_cards[entity]['name'],
                   'slot':slot,'owner_slot':slot,'source_sha256':detail['sha256'],'owner_sha256':ownership['sha256'],
                   'header_rms':rms,'background_rms':ownership['background_rms'],'title':title,
                   'page_support':page_support,'owner_support':owner_support}
            proposals.setdefault(slot,[]).append((entity,proof,detail))
        except (ValueError,OSError,TypeError):
            continue
    for slot,values in proposals.items():
        if len({v[0] for v in values})!=1:continue
        entity,proof,detail=values[0]
        candidate={'id':entity,'slot':slot,'bound_identity':proof}
        if not accepted(candidate):continue
        report.setdefault('identity_sources',[]).extend([copy.deepcopy(detail),copy.deepcopy(detail['ownership_evidence'])])
        for page in report['pages']:
            for card in page['cards']:
                if card['slot']==slot:
                    card['identity_before_tooltip']={'id':card['id'],'method':card['method']}
                    card.update(id=entity,method='bound-tooltip-identity',bound_identity=proof)
        field=f'card_ids.{slot}'
        report['draft']['card_ids'][slot]=entity
        for key in ('conflicts','missing_fields'):
            report[key]=[f for f in report[key] if f!=field]
        report['evidence'][field]=[{'sha256':detail['sha256'],'value':entity,'method':'bound-tooltip-identity'}]
    ids={scanner.catalog.skill_cards[i].get('pIdolId') for i in report['draft']['card_ids'] if i in scanner.catalog.skill_cards}
    ids.discard('');ids.discard(None)
    report['draft']['p_idol_id']=next(iter(ids)) if len(ids)==1 else None
    return cache
