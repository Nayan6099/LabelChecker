"""Find the FSSAI veg / non-veg symbol: a coloured square outline with a filled shape in the
middle (green circle = veg, brown triangle = non-veg). Pure colour + shape rules (OpenCV),
no model. Returns None when not found; the engine then asks for a manual check.
"""
import cv2
import numpy as np

# OpenCV HSV ranges (H 0-180). Brown = dark orange/red with medium value.
COLOURS = {
    "veg": [((35, 80, 50), (90, 255, 255))],
    "non-veg": [((0, 80, 30), (22, 255, 200)), ((165, 80, 30), (180, 255, 200))],
}


def _mask(hsv, ranges):
    m = None
    for lo, hi in ranges:
        part = cv2.inRange(hsv, np.array(lo), np.array(hi))
        m = part if m is None else cv2.bitwise_or(m, part)
    return m


def _looks_like_symbol(m, x, y, w, h):
    """Square outline + filled centre + mostly empty ring between them."""
    if w < 12 or h < 12 or not 0.75 <= w / h <= 1.33:
        return False
    roi = m[y:y + h, x:x + w] > 0
    border = max(1, int(min(w, h) * 0.12))
    edge = np.concatenate([roi[:border].ravel(), roi[-border:].ravel(),
                           roi[:, :border].ravel(), roi[:, -border:].ravel()]).mean()
    cy0, cy1, cx0, cx1 = int(h * .38), int(h * .62), int(w * .38), int(w * .62)
    centre = roi[cy0:cy1, cx0:cx1].mean()
    ring = roi[int(h * .18):int(h * .82), int(w * .18):int(w * .82)].copy()
    rh, rw = ring.shape
    ring[int(rh * .25):int(rh * .75), int(rw * .25):int(rw * .75)] = False  # drop the centre
    ring_fill = ring.mean()
    return edge > 0.6 and centre > 0.6 and ring_fill < 0.35


def find_food_symbol(img_bgr):
    h, w = img_bgr.shape[:2]
    k = min(1.0, 1600 / max(h, w))
    small = cv2.resize(img_bgr, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else img_bgr
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    best = None
    for kind, ranges in COLOURS.items():
        m = _mask(hsv, ranges)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, bw, bh = cv2.boundingRect(c)
            if bw * bh > 0.25 * small.shape[0] * small.shape[1]:
                continue  # a big coloured area, not a symbol
            if _looks_like_symbol(m, x, y, bw, bh):
                box = [int(x / k), int(y / k), int(bw / k), int(bh / k)]
                if best is None or bw * bh > best["box"][2] * best["box"][3] * k * k:
                    best = {"type": kind, "box": box}
    return best
