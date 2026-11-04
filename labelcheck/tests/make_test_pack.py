"""Generate a test pack of labels (PNG, JPG, PDF) for manual testing in the UI and for the
accuracy harness (python -m tools.accuracy run <folder>).

Labels are drawn at 12 px per mm: a 1200 x 900 px image is a 100 x 75 mm panel (75 cm2,
Table-I minimum 1.5 mm). cases.csv says what to enter and what to expect; ground_truth.csv
is the answer sheet for tools/accuracy.py.

Usage: python tests/make_test_pack.py [output_dir]
"""
import csv
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
EN_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
HI = "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
HI_B = "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"
F = lambda p, s: ImageFont.truetype(p, s)  # noqa: E731
PX_PER_MM = 12
ADDR = "Plot 12, Industrial Area, Ghaziabad, UP 201001"
CARE = ["Consumer Care: Quality Manager, Sunrise Foods Pvt Ltd", ADDR,
        "Ph: 1800 123 4567  Email: care@sunrisefoods.in"]
MRP = "MRP Rs. 120.00 (Incl. of all taxes)"
USP = "Unit sale price: Rs. 0.24 per g"


def canvas(w=1200, h=900, bg="white"):
    img = Image.new("RGB", (w, h), bg)
    return img, ImageDraw.Draw(img)


def block(d, x, y, lines, size=26, fill="black", gap=40):
    for i, t in enumerate(lines):
        d.text((x, y + i * gap), t, font=F(EN, size), fill=fill)


def full_label(d, title, qty, mrp=MRP, usp=USP, date="Mfg. Date: 08/2026",
               maker=("Manufactured by: Sunrise Foods Pvt Ltd", ADDR), care=CARE, fill="black"):
    d.text((60, 40), title, font=F(EN_B, 60), fill=fill)
    if qty:
        d.text((60, 190), qty, font=F(EN_B, 56), fill=fill)
    if mrp:
        d.text((60, 320), mrp, font=F(EN, 34), fill=fill)
    if usp:
        d.text((60, 368), usp, font=F(EN, 28), fill=fill)
    if date:
        d.text((60, 412), date, font=F(EN, 28), fill=fill)
    if maker:
        block(d, 60, 470, list(maker), 28, fill)
    if care:
        block(d, 60, 610, list(care), 26, fill)


def c01():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g"); return img


def c02():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g", mrp=None); return img


def c03():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "")
    d.text((60, 200), "Net Wt. 500 g", font=F(EN, 20), fill="black")  # numerals about 1.2 mm, need 1.5
    return img


def c04():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "")
    d.text((60, 190), "Net Wt. 500 g", font=F(EN_B, 56), fill=(150, 150, 150))  # about 2.9:1 contrast
    return img


def c05():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g")
    d.text((520, 200), "NEW! Extra crunchy", font=F(EN_B, 40), fill="black")  # inside the clear zone
    return img


def c06():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g", care=None); return img


def c07():
    img, d = canvas()
    full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g", maker=("Marketed by: Sunrise Brands", "Ghaziabad"))
    return img


def c08():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g", date=None); return img


def c09():
    img, d = canvas(1200, 1000)
    d.text((60, 30), "SUNRISE Mustard Oil", font=F(EN_B, 60), fill="black")
    d.text((60, 120), "सरसों का तेल", font=F(HI_B, 56), fill="black")
    d.text((60, 220), "Net Vol. 1 L", font=F(EN_B, 64), fill="black")
    d.text((60, 340), "MRP Rs. 210.00 (Incl. of all taxes)", font=F(EN, 34), fill="black")
    d.text((60, 395), "अधिकतम खुदरा मूल्य रु. 210.00", font=F(HI, 34), fill="black")
    d.text((60, 460), "Mfg. Date: 09/2026", font=F(EN, 30), fill="black")
    block(d, 60, 530, ["Packed by: Sunrise Oils Ltd", "Sector 5, Kota, Rajasthan 324005"], 28)
    block(d, 60, 660, ["Customer Care: Sunrise Oils Ltd, Sector 5, Kota 324005",
                       "Phone: 98765 43210  care@sunriseoils.in"], 26)
    return img


def c10():
    img, d = canvas(bg=(150, 20, 30))
    full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g", fill="white"); return img


IMPORT_MAKER = ("Imported by: Alpina Trading Pvt Ltd", "Unit 4, Andheri East, Mumbai 400069")
IMPORT_CARE = ["Consumer Care: Alpina Trading Pvt Ltd, Mumbai 400069",
               "Ph: 022 4000 1234  Email: care@alpina.in"]


def c11():
    img, d = canvas(1200, 1000)
    full_label(d, "ALPINA Dark Chocolate", "Net Wt. 100 g", usp="Unit sale price: Rs. 3.50 per g",
               maker=IMPORT_MAKER, care=IMPORT_CARE)
    d.text((60, 860), "Country of Origin: Switzerland", font=F(EN, 28), fill="black")
    return img


def c12():
    img, d = canvas()
    full_label(d, "ALPINA Dark Chocolate", "Net Wt. 100 g", usp="Unit sale price: Rs. 3.50 per g",
               maker=IMPORT_MAKER, care=IMPORT_CARE)
    return img


def c13():
    img, d = canvas()
    full_label(d, "SUNRISE Paper Cups", "Net Qty. 50 N", usp="Unit sale price: Rs. 2.40 per unit",
               maker=("Manufactured by: Sunrise Packaging Ltd", ADDR))
    return img


def c14():
    img, d = canvas()
    full_label(d, "SUNRISE Bidi", "Net Qty. 25 pcs", mrp=None, usp=None,
               maker=("Manufactured by: Sunrise Bidi Works", ADDR))
    return img


def c16(base):
    """Compliant label made to look like a phone photo: tilt, blur, noise, uneven light."""
    img = base.rotate(2.5, expand=False, fillcolor="white", resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(1.1))
    a = np.array(img).astype(np.int16)
    rnd = np.random.default_rng(7)
    h = a.shape[0]
    shade = (25 * np.arange(h) / h).astype(np.int16)[:, None, None]
    a = a + rnd.integers(-12, 13, a.shape[:2])[:, :, None] - shade
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def c17():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g", mrp="MRP Rs. 120.00"); return img


def c18():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g", usp=None); return img


def c19():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g", date="Pkd. Date: 08/2026"); return img


def c20():
    img, d = canvas(); full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g Jumbo"); return img


def c21():
    img, d = canvas()
    full_label(d, "SUNRISE Roasted Peanuts", "Net Wt. 500 g",
               care=["Consumer Care: Quality Manager, Sunrise Foods Pvt Ltd", ADDR, "Ph: 1800 123 4567"])
    return img


def _food(full=True):
    img, d = canvas(2400, 1800)
    L = F(EN, 44)
    rows = [((70, 50), "SUNRISE Roasted Peanuts", F(EN_B, 110)), ((70, 215), "Net Wt. 500 g", F(EN_B, 100)),
            ((70, 460), "MRP Rs. 120.00 (Incl. of all taxes)", L), ((70, 520), "Unit sale price: Rs. 0.24 per g", L),
            ((70, 580), "Mfg. Date: 08/2026" + ("   Use By: 05/2027" if full else ""), L),
            ((70, 1050), "Manufactured by: Sunrise Foods Pvt Ltd", L), ((70, 1110), ADDR, L),
            ((70, 1280), "Consumer Care: Quality Manager, Sunrise Foods Pvt Ltd", L), ((70, 1340), ADDR, L),
            ((70, 1400), "Ph: 1800 123 4567  Email: care@sunrisefoods.in", L)]
    if full:
        d.rectangle((2160, 60, 2304, 204), outline=(0, 140, 60), width=12)
        d.ellipse((2196, 96, 2268, 168), fill=(0, 140, 60))
        S, SB = F(EN, 36), F(EN_B, 36)
        rows += [((70, 640), "Batch No: PN2608A", L),
                 ((70, 730), "Ingredients: Peanuts (95%), Edible vegetable oil (Palm), Iodised salt.", S),
                 ((70, 780), "Allergen information: Contains peanuts.", S),
                 ((70, 860), "Nutritional Information per 100 g (approx.)   %RDA per 30 g serve", SB),
                 ((70, 910), "Energy 590 kcal  Protein 25 g  Carbohydrate 16 g  Total Sugars 4 g", S),
                 ((70, 960), "Added Sugars 0 g  Total Fat 49 g  Saturated Fat 7 g  Trans Fat 0 g  Sodium 400 mg", S),
                 ((70, 1190), "fssai  Lic. No. 10012345678901", F(EN_B, 44))]
    for xy, t, fnt in rows:
        d.text(xy, t, font=fnt, fill="black")
    return img


def c22():
    return _food(True)


def c23():
    return _food(False)


def c25(base):
    """The compliant label lying on a dark table, photographed at an angle."""
    lab = cv2.cvtColor(np.array(base), cv2.COLOR_RGB2BGR)
    H, W = lab.shape[:2]
    bg = np.full((1500, 1900, 3), (60, 45, 35), np.uint8)
    bg = cv2.add(bg, np.random.default_rng(1).integers(0, 15, bg.shape, dtype=np.uint8))
    dst = np.float32([[300, 260], [1560, 330], [1500, 1260], [260, 1180]])
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [W, 0], [W, H], [0, H]]), dst)
    warped = cv2.warpPerspective(lab, M, (1900, 1500))
    mask = cv2.warpPerspective(np.full_like(lab, 255), M, (1900, 1500))
    return Image.fromarray(cv2.cvtColor(np.where(mask > 0, warped, bg), cv2.COLOR_BGR2RGB))


def write_pdf(path):
    """Vector PDF artwork of the compliant label (100 x 75 mm page)."""
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas as pdfcanvas
    pdfmetrics.registerFont(TTFont("DejaVu", EN))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", EN_B))
    c = pdfcanvas.Canvas(str(path), pagesize=(100 * mm, 75 * mm))
    pt = lambda px: px / PX_PER_MM * mm  # noqa: E731
    rows = [(40, "SUNRISE Roasted Peanuts", "DejaVu-Bold", 60), (190, "Net Wt. 500 g", "DejaVu-Bold", 56),
            (320, MRP, "DejaVu", 34), (368, USP, "DejaVu", 28), (412, "Mfg. Date: 08/2026", "DejaVu", 28),
            (470, "Manufactured by: Sunrise Foods Pvt Ltd", "DejaVu", 28), (510, ADDR, "DejaVu", 28),
            (610, CARE[0], "DejaVu", 26), (650, CARE[1], "DejaVu", 26), (690, CARE[2], "DejaVu", 26)]
    for top, text, fnt, px in rows:
        c.setFont(fnt, pt(px))
        c.drawString(5 * mm, 75 * mm - pt(top) - pt(px) * 0.95, text)
    c.showPage()
    c.save()


# answer-sheet values shared by most cases
STD = {"net_quantity": "500 g", "mrp": "120", "mfg_date": "08/2026", "usp": "0.24 per g",
       "phone": "18001234567", "email": "care@sunrisefoods.in", "pin": "201001"}

# name, builder, product, category, imported, auto_straighten, what it tests, expected (human),
# expected_fail (rules), answer-sheet overrides
CASES = [
    ("01_compliant.png", c01, "Roasted Peanuts", "general", False, False, "Baseline: all declarations present",
     "No FAIL; MANUAL items only", "-", {}),
    ("02_missing_mrp.jpg", c02, "Roasted Peanuts", "general", False, False, "Rule 6(1)(e)",
     "FAIL 6(1)(e) no MRP", "6(1)(e)", {"mrp": ""}),
    ("03_small_numerals.png", c03, "Roasted Peanuts", "general", False, False, "Rule 7(2) Table-I",
     "FAIL 7(2): numerals about 1.2 mm, 1.5 mm needed", "7(2)", {}),
    ("04_low_contrast.jpg", c04, "Roasted Peanuts", "general", False, False, "Rule 9(1) contrast",
     "FAIL 9(1) contrast about 2.9:1", "9(1)", {}),
    ("05_crowded_quantity.png", c05, "Roasted Peanuts", "general", False, False, "Rule 8(1) clear space",
     "FAIL 8(1) text inside clear zone", "8(1)", {}),
    ("06_no_consumer_care.jpg", c06, "Roasted Peanuts", "general", False, False, "Rule 6(2)",
     "FAIL 6(2) no contact", "6(2)", {"phone": "", "email": ""}),
    ("07_marketer_only.png", c07, "Roasted Peanuts", "general", False, False, "Rule 6(1)(a)",
     "CHECK 6(1)(a) marketer only, no PIN", "-", {"pin": ""}),
    ("08_no_date.png", c08, "Roasted Peanuts", "general", False, False, "Rule 6(1)(d)",
     "FAIL 6(1)(d) no month/year", "6(1)(d)", {"mfg_date": ""}),
    ("09_bilingual_hindi.jpg", c09, "Mustard Oil", "general", False, False, "Hindi + English, 1 L pack (no USP needed)",
     "No FAIL; 6(11) PASS (1 L = unit)", "-",
     {"net_quantity": "1 L", "mrp": "210", "mfg_date": "09/2026", "usp": "", "phone": "9876543210",
      "email": "care@sunriseoils.in", "pin": "324005"}),
    ("10_white_on_dark.png", c10, "Roasted Peanuts", "general", False, False, "Light text on dark background",
     "No FAIL", "-", {}),
    ("11_imported_with_origin.png", c11, "Dark Chocolate", "general", True, False, "Importer + country of origin",
     "PASS 6(1)(a) importer; PASS 6(1)(aa)", "-",
     {"net_quantity": "100 g", "usp": "3.50 per g", "phone": "02240001234", "email": "care@alpina.in", "pin": "400069"}),
    ("12_imported_no_origin.jpg", c12, "Dark Chocolate", "general", True, False, "Rule 6(1)(aa)",
     "FAIL 6(1)(aa) no country of origin", "6(1)(aa)",
     {"net_quantity": "100 g", "usp": "3.50 per g", "phone": "02240001234", "email": "care@alpina.in", "pin": "400069"}),
    ("13_sold_by_number.png", c13, "Paper Cups", "general", False, False, "Sold by number; USP per unit",
     "No FAIL", "-", {"net_quantity": "50 N", "usp": "2.40 per unit"}),
    ("14_bidi_no_mrp.png", c14, "Bidi", "bidi", False, False, "Category exemption",
     "6(1)(e) and 6(11) N/A for bidi", "-", {"net_quantity": "25 pcs", "mrp": "", "usp": ""}),
    ("15_food_category.png", c01, "Roasted Peanuts", "food", False, False, "Food on a small panel (75 cm2)",
     "6(1)(a)/(d) N/A (FSSAI applies); FSS 5(2)/(3)/(7)/(9) MANUAL (small pack); FAIL FSS 5(10) no use-by; CHECK FSS 5(4) no veg symbol",
     "FSS 5(10)", {}),
    ("16_phone_photo.jpg", None, "Roasted Peanuts", "general", False, True, "Tilt, blur, noise, shading",
     "Straightened; no FAIL", "-", {}),
    ("17_no_tax_wording.png", c17, "Roasted Peanuts", "general", False, False, "Rule 6(1)(e) 'inclusive of all taxes'",
     "FAIL 6(1)(e) no tax wording", "6(1)(e)", {}),
    ("18_no_unit_sale_price.png", c18, "Roasted Peanuts", "general", False, False, "Rule 6(11) unit sale price",
     "FAIL 6(11)", "6(11)", {"usp": ""}),
    ("19_packed_date_only.png", c19, "Roasted Peanuts", "general", False, False, "Rule 6(1)(d) since 2022",
     "CHECK 6(1)(d) only packing date", "-", {}),
    ("20_jumbo_quantity.png", c20, "Roasted Peanuts", "general", False, False, "Rule 12(6) exaggerating word",
     "CHECK 12(6) 'Jumbo'; FAIL 8(1) (the word is inside the clear zone)", "8(1)", {}),
    ("21_no_email.png", c21, "Roasted Peanuts", "general", False, False, "Rule 6(2) e-mail required",
     "FAIL 6(2) e-mail missing", "6(2)", {"email": ""}),
    ("22_food_complete.png", c22, "Roasted Peanuts", "food", False, False, "FSSAI: complete food label (300 cm2)",
     "No FAIL; FSS 5(2)-(14) PASS", "-", {"fssai_licence": "10012345678901", "food_symbol": "veg"}),
    ("23_food_missing_fssai.jpg", c23, "Roasted Peanuts", "food", False, False, "FSSAI items missing (300 cm2)",
     "FAIL FSS 5(2), 5(3), 5(7), 5(9), 5(10); CHECK FSS 5(4)",
     "FSS 5(2);FSS 5(3);FSS 5(7);FSS 5(9);FSS 5(10)", {"food_symbol": "none"}),
    ("24_artwork.pdf", "pdf", "Roasted Peanuts", "general", False, False, "PDF artwork: size from the page",
     "No FAIL; panel 100 x 75 mm read from the PDF", "-", {}),
    ("25_label_on_table.jpg", None, "Roasted Peanuts", "general", False, True, "Photo at an angle on a table",
     "Panel detected, cropped and flattened; no FAIL", "-", {}),
]
SIZES = {"09_bilingual_hindi.jpg": (100, 83.3), "11_imported_with_origin.png": (100, 83.3),
         "22_food_complete.png": (200, 150), "23_food_missing_fssai.jpg": (200, 150), "24_artwork.pdf": ("", "")}


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    built, rows, truth = {}, [], []
    for name, fn, product, cat, imported, straighten, tests, expect, exp_fail, over in CASES:
        path = out / name
        if fn == "pdf":
            write_pdf(path)
        else:
            if name.startswith("16_"):
                img = c16(built["01_compliant.png"])
            elif name.startswith("25_"):
                img = c25(built["01_compliant.png"])
            else:
                img = fn()
            built[name] = img
            if path.suffix == ".jpg":
                img.save(path, "JPEG", quality=88)
            else:
                img.save(path)
        w, h = SIZES.get(name, (100, 75))
        rows.append({"file": name, "product_name": product, "category": cat, "panel_width_mm": w,
                     "panel_height_mm": h, "imported": "yes" if imported else "no",
                     "auto_straighten": "yes" if straighten else "no", "tests": tests, "expected": expect})
        t = {"file": name, "product_name": product, "category": cat, "panel_width_mm": w, "panel_height_mm": h,
             "embossed": "no", "imported": "yes" if imported else "no",
             "auto_straighten": "yes" if straighten else "no", **STD, **over, "expected_fail": exp_fail}
        truth.append(t)
    with open(out / "cases.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    from tools.accuracy import COLUMNS
    with open(out / "ground_truth.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(truth)
    print(f"Wrote {len(rows)} labels to {out}")
    return rows


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    main(sys.argv[1] if len(sys.argv) > 1 else "test-pack")
