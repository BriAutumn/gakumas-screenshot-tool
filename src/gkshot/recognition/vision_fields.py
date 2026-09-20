"""Explicit assistant readings of unresolved fields, with archived pixel provenance.

These are authored observations, not OCR confidence and not human reviews.
Import checks ownership and catalog consistency; it does not independently
measure the accuracy of the assistant's image interpretation.
"""
import copy
import json
from datetime import datetime, timezone

from gkshot.recognition.vision_evidence import digest, validate_observation

METHOD = 'assistant-vision-field-check-v1'


def field_value(draft, field):
    kind, key = field.split('.')
    if kind == 'parameters' and key in ('vocal', 'dance', 'visual', 'stamina'):
        return draft[kind][key]
    if kind in ('customizations', 'item_ids') and key.isdigit():
        slot = int(key)
        if 0 <= slot < (6 if kind == 'customizations' else 4):
            return draft[kind][slot]
    raise ValueError('不支持的视觉核对字段。')


def validate(row, report, catalog):
    field = row['field']; kind, key = field.split('.')
    field_value(report['draft'], field)
    if not isinstance(row.get('observation'), str) or not row['observation'].strip():
        raise ValueError('缺少直接识图观察。')
    box = row.get('box')
    if (not isinstance(box, list) or len(box) != 4 or
            not all(type(x) is int for x in box) or not 0 <= box[0] < box[2] or not 0 <= box[1] < box[3]):
        raise ValueError('缺少原图像素范围。')
    if kind == 'customizations':
        if row['card_id'] != report['draft']['card_ids'][int(key)] or row['value'] != row['token']:
            raise ValueError('强化观察与卡槽不一致。')
        validate_observation(row, catalog)
    else:
        if type(row['value']) is not int or row['value'] < 0:
            raise ValueError('属性和道具必须是非负整数。')
        if kind == 'parameters':
            if row.get('visible_number') != str(row['value']):
                raise ValueError('属性读数与观察不一致。')
        elif kind == 'item_ids':
            item = catalog.p_items.get(row['value'])
            if not item or item != row.get('catalog_item') or item['mode'] != 'stage':
                raise ValueError('道具观察与固定舞台目录不一致。')


def bound_source(report, row):
    kind, key = row['field'].split('.')
    for page in report['pages']:
        if page['sha256'] != row['source_sha256']:
            continue
        if kind == 'customizations' and not any(c['slot'] == int(key) for c in page['cards']):
            raise ValueError('原图没有目标卡槽。')
        if kind == 'parameters' and not page.get('parameters'):
            raise ValueError('原图没有属性区域。')
        if kind == 'item_ids' and not page.get('items'):
            raise ValueError('原图没有道具区域。')
        return page, None
    if kind == 'customizations':
        for detail in report.get('card_details', []) + report.get('vision_source_details', []):
            if (detail['sha256'] == row['source_sha256'] and detail['slot'] == int(key)
                    and detail.get('ownership_evidence')):
                return detail, detail['ownership_evidence']
    raise ValueError('视觉观察不是此记录此字段已存档的原图。')


def supported(report, evidence, field, catalog):
    try:
        item = next(v for v in report.get('vision_fields', []) if v['id'] == evidence.get('vision_id'))
        payload = item['payload']; row = payload['observation']
        if (digest(payload) != item['id'] or payload['method'] != METHOD
                or payload['revision'] != catalog.manifest['revision'] or row['field'] != field
                or row['value'] != field_value(report['draft'], field) or row['value'] != evidence.get('value')):
            return False
        validate(row, report, catalog)
        bound_source(report, row)
        return True
    except (ValueError, KeyError, TypeError, StopIteration, IndexError):
        return False


def add(library, memory_id, row, *, revision, catalog):
    from PIL import Image
    import numpy as np
    from gkshot.catalog import PROJECT_ROOT
    from gkshot.memory_library import assess_automatic, fingerprint
    if revision != catalog.manifest['revision'] or row.get('memory_id') != memory_id:
        raise ValueError('视觉观察的版本或目标记录不一致。')
    record = library.get(memory_id); report = copy.deepcopy(record['report'])
    if record['review']:
        raise ValueError('已有人工审核，不能覆盖。')
    validate(row, report, catalog)
    try:
        source, owner = bound_source(report, row)
    except ValueError:
        # Previously rejected OCR reports were not attached by the ordinary
        # importer. They may be attached as RAW evidence after fresh checks,
        # retaining their original failed status rather than rewriting it.
        source=copy.deepcopy(row.get('additional_detail'))
        if (not source or source.get('kind')!='card-detail' or source.get('sha256')!=row['source_sha256']
                or not row['field'].startswith('customizations.') or source.get('recognition_revision')!=revision):
            raise ValueError('没有已绑定来源或可验证的原始详情报告。')
        from gkshot.recognition.card_detail import canonical
        if any(canonical(str(source['identity'][k]))!=canonical(str(report['identity'][k])) for k in ('title','idol_name','power')):
            raise ValueError('新详情的身份与目标记录不一致。')
        owner=source.get('ownership_evidence')
        if not owner:raise ValueError('新详情缺少展开前原图。')
        source['archive_path']=library.archive(source);owner['archive_path']=library.archive(owner)
        slot=int(row['field'].split('.')[1]);source['slot']=slot
        page=next(p for p in reversed(report['pages']) if any(c['slot']==slot for c in p['cards']))
        card=next(c for c in page['cards'] if c['slot']==slot)
        from gkshot.recognition.native_resolution import native_crop
        with Image.open(library.source_path(page)) as im:native= native_crop(im.convert('RGB'),page['frame'],card['box'])[0]
        with Image.open(library.source_path(owner)) as im:observed=native_crop(im.convert('RGB'),source['frame'],card['box'])[0].resize(native.size,Image.Resampling.LANCZOS)
        if float(np.sqrt(((np.asarray(native,dtype=float)-np.asarray(observed,dtype=float))**2).mean()))>15:
            raise ValueError('新详情展开前的卡槽与记忆原图不一致。')
        report.setdefault('vision_source_details',[]).append(source)
    with Image.open(library.source_path(source)) as raw:
        raw.load()
        x1,y1,x2,y2 = row['box']
        if x2 > raw.width or y2 > raw.height:
            raise ValueError('视觉区域超出原图。')
        from gkshot.recognition.native_resolution import native_crop
        layout=json.loads((PROJECT_ROOT/'configs/layouts/memory-detail-pc-v1.json').read_text(encoding='utf8'))
        kind,key=row['field'].split('.')
        if owner:
            popup=source.get('popup_box')
            if not popup:
                from types import SimpleNamespace
                from gkshot.recognition.card_detail import CardDetailScanner
                helper=CardDetailScanner.__new__(CardDetailScanner);helper.memory=SimpleNamespace(np=np)
                f=source['frame'];im=raw.convert('RGB').crop((f['left'],f['top'],f['right'],raw.height)).resize(tuple(f['normalized_size']),Image.Resampling.LANCZOS)
                boxes=helper.boxes(im)
                if len(boxes)!=1:raise ValueError('没有唯一详情范围。')
                popup=boxes[0]
            expected=native_crop(raw,source['frame'],popup)[1]
        elif kind=='customizations':
            card=next(c for c in source['cards'] if c['slot']==int(key))
            expected=native_crop(raw,source['frame'],card['box'])[1]
        else:
            box=layout['parameters'][key] if kind=='parameters' else layout['item_slots'][int(key)]
            offset=source.get('content_offset',0)
            box=[box[0],box[1]+offset,box[2],box[3]+offset]
            expected=native_crop(raw,source['frame'],box)[1]
        if list(expected)!=row['box']:raise ValueError('视觉区域与目标字段实际位置不一致。')
    binding = dict(source_sha256=source['sha256'])
    if owner:
        # Even a failed OCR title must retain real before/after ownership.
        def normalized(s):
            with Image.open(library.source_path(s)) as im:
                frame=source['frame']
                return im.convert('RGB').crop((frame['left'],frame['top'],frame['right'],im.height)).resize(tuple(frame['normalized_size']),Image.Resampling.LANCZOS)
        before=normalized(owner);after=normalized(source)
        frame=source['frame'];sx=after.width/(frame['right']-frame['left']);sy=after.height/(raw.height-frame['top'])
        box=[int((x1-frame['left'])*sx),int((y1-frame['top'])*sy),int((x2-frame['left'])*sx),int((y2-frame['top'])*sy)]
        mask=np.ones((after.height,after.width),bool);a,b,c,d=box
        mask[max(0,b-24):d+24,max(0,a-24):c+24]=False
        if mask.mean()<.2:raise ValueError('详情范围过大，无法核对归属。')
        rms=float(np.sqrt(((np.asarray(before,dtype=float)-np.asarray(after,dtype=float))[mask]**2).mean()))
        layout=json.loads((PROJECT_ROOT/'configs/layouts/memory-detail-pc-v1.json').read_text(encoding='utf8'))
        header=layout['header_signature'];page=report['pages'][0]
        with Image.open(library.source_path(page)) as im:
            f=page['frame'];reference=im.convert('RGB').crop((f['left'],f['top'],f['right'],im.height)).resize(tuple(f['normalized_size']),Image.Resampling.LANCZOS).crop(header).resize((32,16))
        header_rms=float(np.sqrt(((np.asarray(reference,dtype=float)-np.asarray(before.crop(header).resize((32,16)),dtype=float))**2).mean()))
        if rms>15 or header_rms>15:raise ValueError('详情像素归属不一致。')
        binding.update(background_rms=rms,header_rms=header_rms,owner_sha256=owner['sha256'])
    payload=dict(method=METHOD,revision=revision,observation=copy.deepcopy(row),binding=binding)
    uid=digest(payload)
    if any(v['id']==uid for v in report.get('vision_fields', [])):
        return dict(applied=False,reason='already-attached')
    field=row['field'];previous=field_value(report['draft'],field)
    assess_automatic(report,catalog)
    if field not in report['automatic_check']['unresolved'] or field in report.get('conflicts',[]) or field in report.get('missing_fields',[]):
        raise ValueError('仅允许补足有原图且无归属冲突的待核对字段。')
    if row.get('old_value') != previous:
        raise ValueError('字段已变化，拒绝覆盖。')
    kind,key=field.split('.')
    report['draft'][kind][key if kind=='parameters' else int(key)]=row['value']
    report.setdefault('vision_fields',[]).append(dict(id=uid,payload=payload))
    report.setdefault('evidence',{}).setdefault(field,[]).append(dict(method=METHOD,vision_id=uid,value=row['value']))
    report['status']=assess_automatic(report,catalog)
    now=datetime.now(timezone.utc).isoformat()
    with library.connect() as db:
        changed=db.execute('UPDATE memories SET report_json=?,status=?,content_key=?,updated_at=? WHERE memory_id=? AND updated_at=?',
            (json.dumps(report,ensure_ascii=False),report['status'],fingerprint(report['draft']),now,memory_id,record['updated_at']))
        if changed.rowcount!=1:raise ValueError('导入期间记录已变化。')
    return dict(applied=True,memory_id=memory_id,field=field,old_value=previous,value=row['value'],status=report['status'])
