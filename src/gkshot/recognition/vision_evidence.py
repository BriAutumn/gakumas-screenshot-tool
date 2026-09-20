"""Import explicitly authored assistant image readings, never an OCR score.

Pixel binding and catalog checks do not independently prove the semantic reading.
The full observation and its separate provenance remain attached to the record.
No cloud service is called by this module.
"""
import copy
import hashlib
import json
from datetime import datetime, timezone

METHOD = 'assistant-vision-catalog-check-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def validate_observation(row, catalog):
    from gkshot.recognition.catalog_candidates import candidates, load_tree
    card_id = row['card_id']; token = row['token']
    if type(card_id) is not int or not isinstance(token, str):
        raise ValueError('视觉结果必须明确给出卡片与强化，不能把未知当成空强化。')
    catalog.validate_entities([], [[card_id]], [[token]])
    card = catalog.skill_cards[card_id]
    if row.get('catalog_card') != card:
        raise ValueError('视觉判断对应的卡片目录已变化。')
    selected = [dict(id=int(t.split('x')[0]), level=int(t.split('x')[1]),
                     definition=catalog.customizations[int(t.split('x')[0])])
                for t in token.split('e') if t]
    if row.get('selected_customizations') != selected:
        raise ValueError('视觉判断对应的强化定义不一致。')
    if type(row.get('badge')) is not int or row['badge'] != sum(r['level'] for r in selected):
        raise ValueError('目视强化计数与所选等级不一致。')
    if (type(row.get('corner_cost')) is not int or row['corner_cost'] < 0
            or not isinstance(row.get('text'), str) or not row['text'].splitlines()
            or row['text'].splitlines()[0] != card['name']
            or not isinstance(row.get('green'), list) or not row.get('reason')):
        raise ValueError('缺少完整视觉文字、角标或判断依据。')
    expected = next((r['effects'] for r in candidates(card_id, catalog, load_tree())
                     if r['token'] == token), None)
    if expected is None or expected != row.get('derived_effect_tree'):
        raise ValueError('强化组合与固定数据库推导不一致。')


def supported(report, evidence, slot, catalog):
    """Check stored evidence linkage; full archived pixels are checked on import."""
    try:
        item = next(v for v in report.get('vision_details', [])
                    if v['id'] == evidence.get('vision_id'))
        payload = item['payload']; row = payload['observation']
        if (item['id'] != digest(payload) or payload['method'] != METHOD
                or payload['revision'] != catalog.manifest['revision']
                or row['slot'] != slot + 1 or row['card_id'] != report['draft']['card_ids'][slot]
                or row['token'] != evidence.get('value')
                or row['token'] != report['draft']['customizations'][slot]):
            return False
        validate_observation(row, catalog)
        return any(d['sha256'] == row['source_sha256'] and d['slot'] == slot
                   and d.get('status') != 'conflict'
                   and d.get('ownership_evidence', {}).get('sha256') == row['owner']['sha256']
                   for d in report.get('card_details', []))
    except (KeyError, TypeError, ValueError, StopIteration, OSError):
        return False


def add_visual_detail(library, memory_id, row, *, revision, catalog):
    from gkshot.memory_library import assess_automatic, fingerprint
    import numpy as np
    from PIL import Image
    from gkshot.catalog import PROJECT_ROOT
    if revision != catalog.manifest['revision'] or row.get('memory_id') != memory_id:
        raise ValueError('视觉结果的目录版本或目标记忆不一致。')
    validate_observation(row, catalog)
    record = library.get(memory_id); report = copy.deepcopy(record['report'])
    if record['review']:
        raise ValueError('已有人工审核，不允许视觉结果覆盖。')
    if type(row.get('slot')) is not int or not 1 <= row['slot'] <= 6:
        raise ValueError('视觉结果缺少明确卡槽。')
    slot = row['slot'] - 1; field = f'customizations.{slot}'
    if report['draft']['card_ids'][slot] != row['card_id'] or field in report.get('conflicts', []):
        raise ValueError('目标卡槽不一致或已有冲突。')
    detail = next((d for d in report.get('card_details', [])
                   if d['sha256'] == row['source_sha256'] and d['slot'] == slot), None)
    if detail is None or detail.get('status') == 'conflict':
        raise ValueError('视觉结果不是此槽位已绑定的详情原图。')
    owner = detail.get('ownership_evidence') or {}
    if owner.get('sha256') != row.get('owner', {}).get('sha256'):
        raise ValueError('展开前原图不一致。')
    if detail.get('recognition_revision') != revision or detail['identity'] != report['identity']:
        raise ValueError('详情页头或目录版本与记忆不一致。')

    def normalized(source):
        with Image.open(library.source_path(source)) as im:
            frame = source['frame']
            return im.convert('RGB').crop((frame['left'], frame['top'], frame['right'], im.height)).resize(
                tuple(frame['normalized_size']), Image.Resampling.LANCZOS)

    # Hash-checked archive sources, not caller-controlled image paths.
    tooltip = normalized(detail); before = normalized(dict(owner, frame=detail['frame']))
    if tooltip.size != before.size:
        raise ValueError('详情与展开前尺寸不一致。')
    x1,y1,x2,y2 = detail['popup_box']
    if not (0 <= x1 < x2 <= tooltip.width and 0 <= y1 < y2 <= tooltip.height):
        raise ValueError('详情边界无效。')
    mask = np.ones((tooltip.height, tooltip.width), bool)
    mask[max(0,y1-24):y2+24,max(0,x1-24):x2+24] = False
    if mask.mean() < .2:
        raise ValueError('详情外归属证据不足。')
    rms = float(np.sqrt(((np.asarray(tooltip,dtype=float)-np.asarray(before,dtype=float))[mask]**2).mean()))
    layout = json.loads((PROJECT_ROOT/'configs/layouts/memory-detail-pc-v1.json').read_text(encoding='utf8'))
    header = layout['header_signature']
    reference = normalized(report['pages'][0]).crop(header).resize((32,16))
    observed = before.crop(header).resize((32,16))
    header_rms = float(np.sqrt(((np.asarray(reference,dtype=float)-np.asarray(observed,dtype=float))**2).mean()))
    if rms > 15 or header_rms > 15:
        raise ValueError('原图与当前记忆的像素归属核验失败。')
    payload = dict(method=METHOD, revision=revision, observation=copy.deepcopy(row),
                   binding=dict(background_rms=rms, header_rms=header_rms))
    key = digest(payload)
    if any(d['id'] == key for d in report.get('vision_details', [])):
        return dict(applied=False, reason='already-attached', memory_id=memory_id, slot=slot)
    previous = report['draft']['customizations'][slot]
    if previous is not None:
        raise ValueError('目标已有明确强化，不能覆盖；同证据可重复导入。')
    report.setdefault('vision_details', []).append(dict(id=key, payload=payload))
    report['draft']['customizations'][slot] = row['token']
    report.setdefault('evidence', {}).setdefault(field, []).append(
        dict(method=METHOD, vision_id=key, value=row['token']))
    report['status'] = assess_automatic(report, catalog)
    now = datetime.now(timezone.utc).isoformat()
    with library.connect() as db:
        result = db.execute('UPDATE memories SET report_json=?,status=?,content_key=?,updated_at=? WHERE memory_id=? AND updated_at=?',
            (json.dumps(report,ensure_ascii=False),report['status'],fingerprint(report['draft']),now,memory_id,record['updated_at']))
        if result.rowcount != 1:
            raise ValueError('记忆在导入期间发生变化，未覆盖。')
    return dict(applied=True, memory_id=memory_id, slot=slot, token=row['token'], status=report['status'], method=METHOD)
