"""Shared PC card badge rule: a clear absent green counter means no customization."""


def detect_badge(tile, np):
    result = {"state": "unknown", "rule": "pc-green-counter-v3"}
    if min(tile.size) < 90 or not 0.9 < tile.width/tile.height < 1.1:
        return dict(result, reason="small-or-distorted-card")
    tile = tile.convert("RGB")
    colors = np.asarray(tile).astype(int)
    if float(((colors.max(2)-colors.min(2)) > 40).mean()) < 0.05:
        return dict(result, reason="card-color-unavailable")
    region = [0.36, 0.64, 0.65, 0.90]
    box = [round(v*(tile.width if i%2 == 0 else tile.height)) for i,v in enumerate(region)]
    pixels = np.asarray(tile.crop(box).resize((64,64))).astype(int)
    if pixels.max() < 180 or float(pixels.std()) < 12:
        return dict(result, reason="unreadable-badge-region")
    r,g,b = pixels[:,:,0],pixels[:,:,1],pixels[:,:,2]
    ratio = float(((g > 95) & (g-r > 25) & (g-b > 12)).mean())
    result.update(region=region, green_ratio=round(ratio,5))
    # The counter is a fixed green circle with a white glyph, not arbitrary green
    # within the lower card artwork. Cost icons and art can enter the broad ROI.
    card = np.asarray(tile.resize((200, 200))).astype(int)
    yy, xx = np.ogrid[:200, :200]
    radius = np.sqrt((xx-100.0)**2 + (yy-152.0)**2)
    ring, center = (radius > 10) & (radius < 15), radius < 9
    r, g, b = card[:, :, 0], card[:, :, 1], card[:, :, 2]
    green = (g > 95) & (g-r > 25) & (g-b > 12)
    white = (card.min(2) > 205) & ((card.max(2)-card.min(2)) < 35)
    ring_ratio = float(green[ring].mean())
    glyph_ratio = float(white[center].mean())
    result.update(green_ring_ratio=round(ring_ratio, 5), white_center_ratio=round(glyph_ratio, 5))
    if ring_ratio >= 0.7 and glyph_ratio >= 0.03:
        return dict(result, state="present", reason="green-circle-with-white-glyph")
    if ring_ratio < 0.25:
        return dict(result, state="absent", reason="no-green-circle-at-counter-position")
    return dict(result, reason="uncertain-counter-shape-or-glyph")
