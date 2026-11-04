"""OCR layer: reads a label image and returns words and lines with boxes.

Uses Tesseract (English + Hindi). Boxes are in ORIGINAL image pixels, so
they can be drawn on the uploaded image and measured for font height.
"""
from dataclasses import dataclass, field, asdict

import cv2
import numpy as np
import pytesseract

LANGS = "eng+hin"
MIN_SIDE = 1600  # upscale small images; OCR is much better on larger text


@dataclass
class Word:
    text: str
    conf: float
    box: list  # [x, y, w, h] in original pixels


@dataclass
class Line:
    text: str
    box: list
    words: list = field(default_factory=list)


def _prepare(img_bgr):
    """Grayscale + upscale. Returns (prepared_image, scale_factor)."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    scale = 1.0
    if min(h, w) < MIN_SIDE:
        scale = MIN_SIDE / min(h, w)
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.bilateralFilter(gray, 7, 50, 50)  # reduce noise, keep edges
    return gray, scale


def _boost(gray):
    """Local contrast boost + binarise, so faint text becomes readable.
    Denoise first: boosting a noisy photo turns grain into fake 'words'."""
    clean = cv2.fastNlMeansDenoising(gray, None, 12, 7, 21)
    clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
    eq = clahe.apply(clean)
    return cv2.adaptiveThreshold(eq, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 41, 10)


def _run(gray, scale, invert=False):
    img = 255 - gray if invert else gray
    data = pytesseract.image_to_data(
        img, lang=LANGS, config="--oem 1 --psm 11",
        output_type=pytesseract.Output.DICT,
    )
    words = []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        conf = float(data["conf"][i])
        if not text or conf < 30:
            continue
        x, y, w, h = (int(round(data[k][i] / scale)) for k in ("left", "top", "width", "height"))
        words.append(Word(text, conf, [x, y, w, h]))
    return words


def _dedupe(words):
    """Merge results of normal and inverted passes, dropping overlaps."""
    kept = []
    for w in sorted(words, key=lambda w: -w.conf):
        if not any(_iou(w.box, k.box) > 0.4 for k in kept):
            kept.append(w)
    return kept


def _iou(a, b):
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0


def _plausible(w, min_conf):
    """Drop noise: low confidence, no letters/digits, or a lone low-confidence character."""
    if w.conf < min_conf:
        return False
    alnum = sum(ch.isalnum() for ch in w.text)
    if alnum == 0:
        return False
    if len(w.text) == 1 and w.conf < 80:
        return False
    return True


def _drop_specks(words):
    """Photo noise is read as tiny, short 'words' (often with high confidence, so
    confidence cannot filter them). Drop words under 40% of the typical text height
    that have 3 or fewer letters/digits. Longer words are kept: real fine print."""
    if len(words) < 5:
        return words
    median_h = float(np.median([w.box[3] for w in words]))  # typical text height
    return [w for w in words
            if w.box[3] >= 0.4 * median_h or sum(c.isalnum() for c in w.text) > 3]


def _touch(a, b):
    return not (a[0] + a[2] < b[0] or b[0] + b[2] < a[0] or
                a[1] + a[3] < b[1] or b[1] + b[3] < a[1])


def _group_lines(words):
    """Group words into lines by vertical overlap, then sort left to right."""
    lines = []
    # Left to right, so the gap to a line's right edge is always measured correctly.
    for w in sorted(words, key=lambda w: w.box[0]):
        cy = w.box[1] + w.box[3] / 2
        for ln in lines:
            lx, ly, lw, lh = ln.box
            same_row = abs(cy - (ly + lh / 2)) < max(lh, w.box[3]) * 0.5
            near = w.box[0] - (lx + lw) < max(lh, w.box[3]) * 3
            if same_row and near:
                ln.words.append(w)
                xs = [v.box[0] for v in ln.words]
                ys = [v.box[1] for v in ln.words]
                x2 = [v.box[0] + v.box[2] for v in ln.words]
                y2 = [v.box[1] + v.box[3] for v in ln.words]
                ln.box = [min(xs), min(ys), max(x2) - min(xs), max(y2) - min(ys)]
                break
        else:
            lines.append(Line(w.text, list(w.box), [w]))
    for ln in lines:
        ln.words.sort(key=lambda v: v.box[0])
        ln.text = " ".join(v.text for v in ln.words)
    return sorted(lines, key=lambda ln_: (ln_.box[1], ln_.box[0]))


_UNIT_LOOKALIKES = {"6", "9", "q", "a", "६", "७", "९"}  # small 'g' is often read as these


def _reread_units(img_bgr, words):
    """A lone small 'g' after a number or 'per' is often misread as 6, 9 or a Hindi digit.
    Re-read just that character: crop, enlarge, English only, one character,
    unit letters allowed. Only replaces the word if Tesseract is confident."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape
    for w in words:
        if w.text not in _UNIT_LOOKALIKES:
            continue
        x, y, bw, bh = w.box
        prev = [v for v in words if v is not w
                and (v.text.replace(".", "").isdigit() or v.text.lower() in ("per", "/"))
                and abs((v.box[1] + v.box[3] / 2) - (y + bh / 2)) < bh
                and 0 <= x - (v.box[0] + v.box[2]) < 2.5 * bh]
        if not prev:
            continue
        pad = max(4, bh // 3)
        crop = gray[max(0, y - pad):min(H, y + bh + pad), max(0, x - pad):min(W, x + bw + pad)]
        crop = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        crop = cv2.copyMakeBorder(crop, 20, 20, 20, 20, cv2.BORDER_REPLICATE)
        d = pytesseract.image_to_data(
            crop, lang="eng", output_type=pytesseract.Output.DICT,
            config="--oem 1 --psm 8 -c tessedit_char_whitelist=gkmlLNsc")
        best = max(((t.strip(), float(c)) for t, c in zip(d["text"], d["conf"], strict=False) if t.strip()),
                   key=lambda p: p[1], default=None)
        if best and best[1] >= 60:
            w.text = best[0]
    return words


def read_label(img_bgr):
    """Run OCR. Returns dict with words, lines, full text and image size."""
    gray, scale = _prepare(img_bgr)
    primary = [w for w in _dedupe(_run(gray, scale)) if _plausible(w, 30)]  # dark on light
    extra = (_run(gray, scale, invert=True)         # light text on dark
             + _run(_boost(gray), scale))           # faint / low-contrast text
    # Extra passes turn photo noise into fake words, so they must be confident.
    extra = [w for w in extra if _plausible(w, 60)]
    # Extra passes only fill gaps: drop anything touching a primary word,
    # otherwise they add stray characters inside already-read words.
    extra = [w for w in extra if not any(_iou(w.box, p.box) > 0 or _touch(w.box, p.box)
                                         for p in primary)]
    words = _drop_specks(primary + _dedupe(extra))
    words = _reread_units(img_bgr, words)
    lines = _group_lines(words)
    h, w = img_bgr.shape[:2]
    return {
        "width": w,
        "height": h,
        "words": [asdict(x) for x in words],
        "lines": [{"text": ln_.text, "box": ln_.box,
                   "words": [asdict(x) for x in ln_.words]} for ln_ in lines],
        "text": "\n".join(ln_.text for ln_ in lines),
    }


def load_image(data: bytes):
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not read the image. Upload a JPG or PNG file.")
    return img
