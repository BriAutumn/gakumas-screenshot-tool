"""Spatial reading order for fragmented OCR lines."""


def order_text_rows(rows):
    # Fixed anchor intervals prevent a tall later fragment from joining two
    # separate lines transitively. Keep the caller's evidence objects intact.
    positioned = []
    for index, row in enumerate(rows):
        xs, ys = zip(*row['box'])
        positioned.append((min(ys), max(ys), min(xs), index, row))
    bands = []
    for entry in sorted(positioned, key=lambda p: p[:4]):
        top, bottom, _, _, _ = entry
        height = bottom - top
        for anchor_top, anchor_bottom, members in bands:
            anchor_height = anchor_bottom - anchor_top
            if (height > 0 and anchor_height > 0
                    and min(bottom, anchor_bottom) - max(top, anchor_top)
                    >= .5 * min(height, anchor_height)):
                members.append(entry)
                break
        else:
            bands.append((top, bottom, [entry]))
    return [entry[4] for _, _, members in bands
            for entry in sorted(members, key=lambda p: (p[2], p[0], p[3]))]
