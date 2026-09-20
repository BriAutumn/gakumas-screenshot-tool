"""Distinguish shared P-item art by the small upgrade mark, with abstention."""


def _verify_item_variant_v1(scanner, crop, record):
    np = scanner.np
    candidates = record.get("candidates", [])
    if min(crop.size) < 80 or record.get("method") != "template" or len(candidates) < 3:
        return record
    catalog = scanner.catalog.p_items
    def family(item_id):
        row = catalog[item_id]
        return (row["name"].removesuffix("+"), row.get("pIdolId"), row["mode"], row["plan"])
    ids = [c["id"] for c in candidates[:2]]
    other = next((c for c in candidates[2:] if family(c["id"]) != family(ids[0])), None)
    if (family(ids[0]) != family(ids[1]) or catalog[ids[0]]["upgraded"] == catalog[ids[1]]["upgraded"]
            or other is None or candidates[0]["distance"] >= 1200
            or candidates[0]["distance"] >= other["distance"] * 0.7):
        return record
    templates = scanner.item_templates[[scanner.item_ids.index(i) for i in ids]]
    mark = np.max(abs(templates[0]-templates[1]),axis=2)>70
    count = int(mark.sum())
    if count < 25 or int(mark[:14,32:].sum()) < count*0.95:
        return record
    pixels = np.asarray(crop.resize((48,48)),dtype=np.float32)
    best = None
    # Align shared art without looking at the upgrade corner. Both variants
    # receive the same translation; wrapped pixels are never evidence.
    for dy in range(-2,3):
        for dx in range(-2,3):
            moved = np.roll(templates,(dy,dx),(1,2))
            valid = np.ones((48,48),dtype=bool)
            if dy>0: valid[:dy]=False
            elif dy<0: valid[dy:]=False
            if dx>0: valid[:,:dx]=False
            elif dx<0: valid[:,dx:]=False
            stable = valid.copy()
            stable[:14,32:]=False
            stable[:2]=False; stable[-2:]=False; stable[:,:2]=False; stable[:,-2:]=False
            alignment = float(((moved[0][stable]-pixels[stable])**2).mean())
            mask = np.roll(mark,(dy,dx),(0,1)) & valid
            if int(mask.sum()) < 25:
                continue
            errors = [float(((t[mask]-pixels[mask])**2).mean()) for t in moved]
            if best is None or alignment < best[0]:
                best = (alignment,dy,dx,errors,int(mask.sum()))
    if best is None:
        return record
    alignment,dy,dx,errors,count = best
    winner = min(range(2),key=lambda i:errors[i])
    win,lose = errors[winner],errors[1-winner]
    if alignment < 1200 and win < 2000 and win < lose*0.35:
        record.update(id=ids[winner],method="template-and-upgrade-mark",variant_evidence={
            "rule":"pitem-corner-v1", "id":ids[winner], "distance":round(win,2),
            "alternative_distance":round(lose,2), "alignment_distance":round(alignment,2),
            "pixel_count":count, "shift":[dy,dx], "family_distance":candidates[0]["distance"],
            "other_family_distance":other["distance"]})
    return record


def verify_item_variant(scanner, crop, record):
    result = _verify_item_variant_v1(scanner, crop, record)
    if result.get('method') != 'template' or min(crop.size) < 80:
        return result
    return _verify_scaled_item(scanner, crop, result)


def _verify_scaled_item(scanner, crop, record):
    """Fit shared art scale before comparing the two upgrade corners.

    PC icons include padding/shadows absent from reference assets. Translation
    alone cannot register their corners. Fit only shared art, never the mark;
    competing families get the same transform search and must remain separated.
    """
    from PIL import Image
    np = scanner.np
    catalog = scanner.catalog.p_items
    def family(item):
        return (item['name'].removesuffix('+'), item.get('pIdolId'), item['mode'], item['plan'])
    families = {family(catalog[c['id']]) for c in record.get('candidates', [])}
    if len(families) < 2:
        return record
    pixels = np.asarray(crop.resize((48, 48)), dtype=np.float32)
    ranked = []
    cache = getattr(scanner, '_scaled_item_templates', None)
    if cache is None:
        cache = scanner._scaled_item_templates = {}
    stable = np.zeros((48,48), dtype=bool)
    stable[5:43,5:43] = True
    stable[:16,30:] = False
    for key in families:
        ids = [i for i in scanner.item_ids if family(catalog[i]) == key]
        if len(ids)>2 or not ids:
            return record
        if len(ids)==1:
            # A family without an upgrade must still compete on shared art.
            ids=ids*2
        elif catalog[ids[0]]['upgraded'] == catalog[ids[1]]['upgraded']:
            return record
        cache_key = tuple(ids)
        if cache_key not in cache:
            if len(cache)>=32:
                cache.pop(next(iter(cache)))
            templates = scanner.item_templates[[scanner.item_ids.index(i) for i in ids]]
            variants = []
            for size in (42,44,46,48,50,52,54):
                pair = []
                for template in templates:
                    canvas = Image.new('RGB',(48,48),'white')
                    canvas.paste(Image.fromarray(template.astype(np.uint8)).resize((size,size),Image.Resampling.BILINEAR),
                                 ((48-size)//2,(48-size)//2))
                    pair.append(np.asarray(canvas,dtype=np.float32))
                for dy in range(-2,3):
                    for dx in range(-2,3):
                        moved = np.roll(np.stack(pair),(dy,dx),(1,2))
                        variants.append((size,dy,dx,moved))
            cache[cache_key] = variants
        variants = cache[cache_key]
        distances = [float(((v[3][0][stable]-pixels[stable])**2).mean()) for v in variants]
        index = int(np.argmin(distances))
        ranked.append((distances[index],ids,variants[index]))
    ranked.sort(key=lambda r:r[0])
    if len(ranked)<2 or ranked[0][0]>=1200 or ranked[0][0]>=ranked[1][0]*0.7:
        return record
    distance,ids,(size,dy,dx,pair) = ranked[0]
    mark = np.max(abs(pair[0]-pair[1]),axis=2)>70
    # Wrapped edges and clipping cannot establish absence of a plus.
    valid = np.ones((48,48),dtype=bool)
    if dy>0: valid[:dy]=False
    elif dy<0: valid[dy:]=False
    if dx>0: valid[:,:dx]=False
    elif dx<0: valid[:,dx:]=False
    if (mark & ~valid).any():
        return record
    count = int(mark.sum())
    if count<25 or int(mark[:18,28:].sum())<count*0.95:
        return record
    errors = [float(((t[mark]-pixels[mark])**2).mean()) for t in pair]
    winner = int(np.argmin(errors))
    win,lose=errors[winner],errors[1-winner]
    if win<2000 and win<lose*0.35:
        record.update(id=ids[winner],method='template-and-upgrade-mark',variant_evidence={
            'rule':'pitem-corner-v2','id':ids[winner],'distance':round(win,2),
            'alternative_distance':round(lose,2),'alignment_distance':round(distance,2),
            'pixel_count':count,'shift':[dy,dx],'template_size':size,
            'family_distance':round(distance,2),'other_family_distance':round(ranked[1][0],2)})
    return record
