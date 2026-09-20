"""Supplement colored components with complete card borders on the PC layout."""


def complete_card_frames(image, found, *, top, bottom):
    import cv2
    import numpy as np
    pixels = np.asarray(image.convert('RGB'))
    edges = cv2.Canny(cv2.cvtColor(pixels,cv2.COLOR_RGB2GRAY),60,140)
    edges[:top]=0
    edges[bottom:]=0
    edges = cv2.morphologyEx(edges,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    candidates=[]
    for contour in cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0]:
        x,y,w,h=cv2.boundingRect(contour)
        if not (190<w<235 and 190<h<235 and abs(w-h)<=6
                and y>top+3 and y+h<bottom-3 and cv2.contourArea(contour)>w*h*.9):
            continue
        if min(abs(x-column) for column in (77,313,549,784))>12:
            continue
        interior=pixels[y+12:y+h-12,x+12:x+w-12].astype(int)
        # An empty gray slot or an outlined blank rectangle is not a card.
        if interior.std()<20 or float(((interior.max(2)-interior.min(2)>40)&(interior.max(2)>150)).mean())<.06:
            continue
        candidates.append((x,y,w,h))
    result=list(found)
    for x,y,w,h in sorted(candidates,key=lambda b:b[2]*b[3],reverse=True):
        if any(abs(x+w/2-(a+c/2))<15 and abs(y+h/2-(b+d/2))<15 for a,b,c,d in result):
            continue
        # Match the crop convention of the existing colored component detector.
        result.append((x+2,y+4,w-4,h-8))
    # Large score digits can interrupt a white card's contour at a corner.
    # Existing cards establish the row geometry; require actual edge evidence
    # on all four sides and a nonblank colored interior for each missing cell.
    rows=[]
    for box in sorted(result,key=lambda b:(b[1],b[0])):
        if not rows or abs(box[1]-rows[-1][0][1])>12:rows.append([])
        rows[-1].append(box)
    for row in rows:
        if len(row)<2:continue
        y,w,h=(round(float(np.median([b[i] for b in row]))) for i in (1,2,3))
        for column in (77,313,549,784):
            x=column+2
            if any(abs(b[0]-x)<15 for b in row):continue
            if y-5<=top or y+h+5>=bottom:continue
            xa,xb=x+round(w*.3),x+round(w*.8)
            ya,yb=y+round(h*.25),y+round(h*.75)
            sides=(edges[y-5:y+3,xa:xb].max(0),edges[y+h-2:y+h+6,xa:xb].max(0),
                   edges[ya:yb,x-5:x+3].max(1),edges[ya:yb,x+w-2:x+w+6].max(1))
            if not all(float((side>0).mean())>=.85 for side in sides):continue
            interior=pixels[y+12:y+h-12,x+12:x+w-12].astype(int)
            if interior.std()<20 or float(((interior.max(2)-interior.min(2)>40)&(interior.max(2)>150)).mean())<.06:continue
            result.append((x,y,w,h))
    return result


def detail_at_bottom(image):
    """Default PC detail scrollbar; absent/obscured evidence stays unknown."""
    import numpy as np
    pixels = np.asarray(image.convert('RGB').crop((round(image.width*.981),round(image.height*.515),
                                                  round(image.width*.996),round(image.height*.906)))).astype(int)
    dark = ((pixels.mean(2)>90)&(pixels.mean(2)<190)&((pixels.max(2)-pixels.min(2))<30)).mean(1)
    rows = np.flatnonzero(dark>.2)
    return bool(3<len(rows)<len(dark)*.95 and rows[-1]>=len(dark)-max(6,round(len(dark)*.02)))


def complete_card_grid(image, rectangles, *, at_bottom, read_heading):
    """Prove a packed 1..6 card grid, never infer empty slots from detector misses.

    Short grids require the section heading, bottom scrollbar and bright blank
    trailing space. A clipped/unrecognized/obscured card is not an empty slot.
    Coordinates refer to the canonical 1080-pixel-wide PC client image.
    """
    import numpy as np
    import unicodedata
    columns = (77,313,549,784)
    groups = []
    for box in sorted(rectangles,key=lambda b:(b[1],b[0])):
        x,y,w,h = box
        col = min(range(4),key=lambda c:abs(x-columns[c]))
        if abs(x-columns[col])>15:
            continue
        if not groups or abs(y-groups[-1][0][1][1])>20:
            groups.append([])
        groups[-1].append((col,box))
    for start, first in enumerate(groups):
        rows = [first]
        if start+1<len(groups) and 250<groups[start+1][0][1][1]-first[0][1][1]<300:
            rows.append(groups[start+1])
        if any(sorted(c for c,_ in row)!=list(range(len(row))) for row in rows):
            continue
        if len(rows)==2 and (len(first)!=4 or len(rows[1])>2):
            continue
        ordered = [box for row in rows for _,box in sorted(row)]
        count = len(ordered)
        if not 1<=count<=6:
            continue
        if count==6:
            return {'rule':'pc-packed-card-grid-v1','count':count,'boxes':ordered,'empty_slots':[]}
        if not at_bottom or start!=0 or len(rows)!=len(groups):
            continue
        top = min(b[1] for b in ordered)
        bottom = image.height-170
        last_bottom = max(y+h for x,y,w,h in ordered)
        if top-75<round(image.height*.51) or last_bottom+42>bottom:
            continue
        def blank(box):
            pixels = np.asarray(image.crop(box).convert('RGB')).astype(int)
            return bool(pixels.size and ((pixels.min(2)>225)&(pixels.max(2)-pixels.min(2)<25)).mean()>.995)
        # Include borders and the entire remaining row, not just the card centers.
        trailing = len(rows[-1])
        if trailing<4 and not blank((columns[trailing]-6,rows[-1][0][1][1]-4,1020,last_bottom+3)):
            continue
        if bottom-last_bottom-55>8 and not blank((65,last_bottom+55,1020,bottom)):
            continue
        heading = read_heading(image.crop((60,top-75,440,top-12)))
        if isinstance(heading,dict):heading=[heading]
        if not any('スキルカード' in ''.join(unicodedata.normalize('NFKC',r.get('text') or '').split())
                   and r.get('score',0)>=.65 for r in heading):
            continue
        return {'rule':'pc-packed-card-grid-v1','count':count,'boxes':ordered,
                'empty_slots':list(range(count,6)), 'bottom_confirmed':True,
                'heading_confirmed':True,'trailing_blank_confirmed':True}
    return None
