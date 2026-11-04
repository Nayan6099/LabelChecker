"""Turn an upload into the image that gets checked.

- Validates the file by its content (magic bytes), not by its name.
- PDF artwork: renders one page with pypdfium2 (Apache/BSD licence, safe for a
  commercial product; PyMuPDF is AGPL so it is avoided). The PDF page has a real
  physical size, so the panel size in mm is known without the user typing it.
- Photos: optional auto-straighten = find the panel edges and flatten it
  (perspective warp), then correct small text tilt (deskew).
Every change made to the image is recorded in `notes`, so the reviewer can see it.
"""
import io

import cv2
import numpy as np
from PIL import Image

MAX_PIXELS = 40_000_000          # refuse decompression bombs (e.g. 100k x 100k PNG)
MAX_PDF_PAGES = 50
PDF_DPI = 300
PDF_MAX_SIDE = 7000              # lower the DPI for very large artwork
IMAGE_TYPES = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpg", b"RIFF": "webp", b"BM": "bmp",
               b"II*\x00": "tif", b"MM\x00*": "tif"}


class InputError(ValueError):
    pass


def sniff(data: bytes):
    """Return 'pdf', 'png', 'jpg', ... from the file content, or None."""
    if data[:5] == b"%PDF-":
        return "pdf"
    for magic, kind in IMAGE_TYPES.items():
        if data.startswith(magic):
            if kind == "webp" and data[8:12] != b"WEBP":
                continue
            return kind
    return None


def decode_image(data: bytes):
    try:
        with Image.open(io.BytesIO(data)) as im:   # reads only the header
            w, h = im.size
    except Exception:
        raise InputError("Could not read the image. Upload a JPG, PNG or PDF file.") from None
    if w * h > MAX_PIXELS:
        raise InputError(f"Image is {w}x{h} pixels; the limit is {MAX_PIXELS // 1_000_000} megapixels.")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)  # applies EXIF rotation
    if img is None:
        raise InputError("Could not read the image. Upload a JPG, PNG or PDF file.")
    return img


def render_pdf(data: bytes, page_no=1):
    """Render one PDF page. Returns (image, width_mm, height_mm, notes)."""
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:
        raise InputError("Could not open the PDF (damaged or password protected).") from None
    try:
        n = len(pdf)
        if n == 0:
            raise InputError("The PDF has no pages.")
        if n > MAX_PDF_PAGES:
            raise InputError(f"The PDF has {n} pages; the limit is {MAX_PDF_PAGES}.")
        if not 1 <= page_no <= n:
            raise InputError(f"Page {page_no} does not exist; the PDF has {n} page(s).")
        page = pdf[page_no - 1]
        w_pt, h_pt = page.get_size()
        w_mm, h_mm = w_pt / 72 * 25.4, h_pt / 72 * 25.4
        dpi = min(PDF_DPI, PDF_MAX_SIDE / (max(w_pt, h_pt) / 72))
        pil = page.render(scale=dpi / 72, fill_color=(255, 255, 255, 255)).to_pil().convert("RGB")
    finally:
        pdf.close()
    img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    notes = [f"PDF page {page_no} of {n} rendered at {dpi:.0f} dpi; page size "
             f"{w_mm:.1f} x {h_mm:.1f} mm."]
    return img, round(w_mm, 1), round(h_mm, 1), notes


# ---------------- auto-straighten ----------------

def _order(pts):
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
                    dtype=np.float32)  # top-left, top-right, bottom-right, bottom-left


def find_panel(img):
    """Find a 4-cornered panel (label or pack face) that clearly stands out from the
    background. Returns 4 corner points or None. Ignores shapes under 25% of the image
    (likely a box drawn on the label) and over 92% (already cropped)."""
    h, w = img.shape[:2]
    k = 900 / max(h, w)
    small = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else img.copy()
    k = min(k, 1.0)
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(gray, 40, 120), np.ones((3, 3), np.uint8), iterations=2)
    cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    area_img = small.shape[0] * small.shape[1]
    for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5]:
        a = cv2.contourArea(c)
        if not 0.25 * area_img <= a <= 0.92 * area_img:
            continue
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return _order(approx.reshape(4, 2).astype(np.float32) / k)
    return None


def warp(img, quad):
    tl, tr, br, bl = quad
    w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(quad, dst), (w, h),
                               flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _profile_score(binary, angle):
    h, w = binary.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rot = cv2.warpAffine(binary, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    rows = rot.sum(axis=1, dtype=np.float64)
    return float(np.var(rows))  # sharp peaks/gaps between text lines = straight text


def text_angle(img, limit=10.0):
    """Estimate text tilt in degrees with a projection profile (works on dense text)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    k = 1000 / max(gray.shape)
    if k < 1:
        gray = cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    _, binary = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if binary.mean() > 0.5:          # make text = 1 whatever the polarity
        binary = 1 - binary
    if binary.sum() < 50:
        return 0.0
    coarse = max(np.arange(-limit, limit + 0.01, 0.5), key=lambda a: _profile_score(binary, a))
    fine = max(np.arange(coarse - 0.5, coarse + 0.51, 0.1), key=lambda a: _profile_score(binary, a))
    return round(float(fine), 1)


def rotate(img, angle):
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def straighten(img):
    """Crop/flatten to the panel if one is found, then fix tilt. Returns (img, notes, cropped)."""
    notes, cropped = [], False
    quad = find_panel(img)
    if quad is not None:
        img = warp(img, quad)
        cropped = True
        notes.append(f"Panel edges detected; image cropped and flattened to {img.shape[1]}x"
                     f"{img.shape[0]} px. Enter the size of this panel.")
    angle = text_angle(img)
    if abs(angle) >= 0.5:
        img = rotate(img, angle)
        notes.append(f"Text was tilted; image rotated by {-angle:+.1f} degrees.")
    if not notes:
        notes.append("Auto-straighten: no panel edge or tilt found; image used as uploaded.")
    return img, notes, cropped


def prepare(data: bytes, opts):
    """Main entry. Returns (image, opts_updates, notes, source_type)."""
    kind = sniff(data)
    if kind is None:
        raise InputError("Unsupported file. Upload a JPG, PNG, WEBP, BMP, TIFF or PDF.")
    updates, notes = {}, []
    if kind == "pdf":
        img, w_mm, h_mm, notes = render_pdf(data, int(opts.get("pdf_page") or 1))
        if not (opts.get("panel_width_mm") or opts.get("panel_height_mm")):
            updates = {"panel_width_mm": w_mm, "panel_height_mm": h_mm}
            notes.append("Panel size taken from the PDF page size. If the page has bleed or "
                         "several panels, enter the panel size yourself.")
        return img, updates, notes, "pdf"
    img = decode_image(data)
    if opts.get("auto_straighten"):
        img, n, _ = straighten(img)
        notes += n
    # One dimension given: work out the other from the image proportions.
    w, h = opts.get("panel_width_mm"), opts.get("panel_height_mm")
    ih, iw = img.shape[:2]
    if w and not h:
        updates["panel_height_mm"] = round(w * ih / iw, 1)
        notes.append(f"Panel height {updates['panel_height_mm']} mm worked out from the width and "
                     "the image proportions (correct only if the image shows exactly the panel).")
    elif h and not w:
        updates["panel_width_mm"] = round(h * iw / ih, 1)
        notes.append(f"Panel width {updates['panel_width_mm']} mm worked out from the height and "
                     "the image proportions (correct only if the image shows exactly the panel).")
    return img, updates, notes, "image"
