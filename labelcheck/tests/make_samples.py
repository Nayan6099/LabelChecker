"""Generate synthetic label images for testing.

These are clean, computer-drawn labels. They test that the pipeline works
end to end; they do NOT show real-world accuracy. Real accuracy needs a test
set of photographed labels (glossy packs, curves, glare).

Labels are drawn at 12 px per mm, so a 1200 x 900 px image = a 100 x 75 mm panel (75 cm2,
Table-I minimum text height 1.5 mm).
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "samples"
EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
EN_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
HI = "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
HI_B = "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"


def font(path, size):
    return ImageFont.truetype(path, size)


def draw_lines(d, items):
    for xy, text, fnt, fill in items:
        d.text(xy, text, font=fnt, fill=fill)


def good_label():
    img = Image.new("RGB", (1200, 900), "white")
    d = ImageDraw.Draw(img)
    draw_lines(d, [
        ((60, 40), "SUNRISE Roasted Peanuts", font(EN_B, 64), "black"),
        # clear space: numeral height (~5 mm) above/below, twice that left/right
        ((60, 190), "Net Wt. 500 g", font(EN_B, 56), "black"),
        ((60, 320), "MRP Rs. 120.00 (Incl. of all taxes)", font(EN, 34), "black"),
        ((60, 368), "Unit sale price: Rs. 0.24 per g", font(EN, 28), "black"),
        ((60, 412), "Mfg. Date: 08/2026", font(EN, 28), "black"),
        ((60, 470), "Manufactured by: Sunrise Foods Pvt Ltd", font(EN, 28), "black"),
        ((60, 510), "Plot 12, Industrial Area, Ghaziabad, UP 201001", font(EN, 28), "black"),
        ((60, 610), "Consumer Care: Quality Manager, Sunrise Foods Pvt Ltd", font(EN, 26), "black"),
        ((60, 650), "Plot 12, Industrial Area, Ghaziabad, UP 201001", font(EN, 26), "black"),
        ((60, 690), "Ph: 1800 123 4567  Email: care@sunrisefoods.in", font(EN, 26), "black"),
    ])
    img.save(OUT / "good_label.png")


def bad_label():
    img = Image.new("RGB", (1200, 900), "white")
    d = ImageDraw.Draw(img)
    draw_lines(d, [
        ((60, 40), "SUNRISE Roasted Peanuts", font(EN_B, 64), "black"),
        # small net quantity with text crowded next to it
        ((60, 160), "Net Wt. 500 g", font(EN, 22), "black"),  # numerals ~1.6 mm, needs 2 mm
        ((225, 160), "Best taste guaranteed", font(EN, 22), "black"),
        # no MRP at all
        ((60, 320), "Mfg. Date: 08/2026", font(EN, 30), "black"),
        ((60, 400), "Marketed by: Sunrise Brands", font(EN, 28), "black"),
        ((60, 440), "Ghaziabad", font(EN, 28), "black"),
        ((60, 540), "For complaints write to care@sunrisefoods.in", font(EN, 26), "black"),
    ])
    img.save(OUT / "bad_label.png")


def low_contrast_label():
    """Large but faint net quantity: tests contrast only. (Small AND faint text
    is often not read by OCR at all, so the two problems are tested separately.)"""
    img = Image.new("RGB", (1200, 900), "white")
    d = ImageDraw.Draw(img)
    draw_lines(d, [
        ((60, 40), "SUNRISE Roasted Peanuts", font(EN_B, 64), "black"),
        ((60, 190), "Net Wt. 500 g", font(EN_B, 56), (150, 150, 150)),  # ~2.9:1 contrast
        ((60, 330), "MRP Rs. 120.00 (Incl. of all taxes)", font(EN, 34), "black"),
        ((60, 390), "Mfg. Date: 08/2026", font(EN, 30), "black"),
    ])
    img.save(OUT / "low_contrast_label.png")


def bilingual_label():
    img = Image.new("RGB", (1200, 1000), "white")
    d = ImageDraw.Draw(img)
    draw_lines(d, [
        ((60, 30), "SUNRISE Mustard Oil", font(EN_B, 60), "black"),
        ((60, 120), "सरसों का तेल", font(HI_B, 56), "black"),
        ((60, 220), "Net Vol. 1 L", font(EN_B, 64), "black"),
        ((60, 330), "MRP Rs. 210.00 (Incl. of all taxes)", font(EN, 34), "black"),
        ((60, 390), "अधिकतम खुदरा मूल्य रु. 210.00", font(HI, 34), "black"),
        ((60, 460), "Pkd. Date: 09/2026", font(EN, 30), "black"),
        ((60, 530), "Packed by: Sunrise Oils Ltd", font(EN, 28), "black"),
        ((60, 570), "Sector 5, Kota, Rajasthan 324005", font(EN, 28), "black"),
        ((60, 660), "Customer Care: Sunrise Oils Ltd, Sector 5, Kota 324005", font(EN, 26), "black"),
        ((60, 700), "Phone: 98765 43210  care@sunriseoils.in", font(EN, 26), "black"),
    ])
    img.save(OUT / "bilingual_label.png")


def food_label():
    """A complete FSSAI food label, 2400 x 1800 px at 12 px/mm = 200 x 150 mm panel (300 cm2):
    Legal Metrology declarations need 2.5 mm, FSSAI text 2 mm."""
    img = Image.new("RGB", (2400, 1800), "white")
    d = ImageDraw.Draw(img)
    # veg symbol: green square outline with a filled green circle (about 12 mm square)
    d.rectangle((2160, 60, 2304, 204), outline=(0, 140, 60), width=12)
    d.ellipse((2196, 96, 2268, 168), fill=(0, 140, 60))
    L = font(EN, 44)
    draw_lines(d, [
        ((70, 50), "SUNRISE Roasted Peanuts", font(EN_B, 110), "black"),
        ((70, 215), "Net Wt. 500 g", font(EN_B, 100), "black"),
        ((70, 460), "MRP Rs. 120.00 (Incl. of all taxes)", L, "black"),
        ((70, 520), "Unit sale price: Rs. 0.24 per g", L, "black"),
        ((70, 580), "Mfg. Date: 08/2026   Use By: 05/2027", L, "black"),
        ((70, 640), "Batch No: PN2608A", L, "black"),
        ((70, 730), "Ingredients: Peanuts (95%), Edible vegetable oil (Palm), Iodised salt.", font(EN, 36), "black"),
        ((70, 780), "Allergen information: Contains peanuts.", font(EN, 36), "black"),
        ((70, 860), "Nutritional Information per 100 g (approx.)   %RDA per 30 g serve", font(EN_B, 36), "black"),
        ((70, 910), "Energy 590 kcal  Protein 25 g  Carbohydrate 16 g  Total Sugars 4 g", font(EN, 36), "black"),
        ((70, 960), "Added Sugars 0 g  Total Fat 49 g  Saturated Fat 7 g  Trans Fat 0 g  Sodium 400 mg", font(EN, 36), "black"),
        ((70, 1050), "Manufactured by: Sunrise Foods Pvt Ltd", L, "black"),
        ((70, 1110), "Plot 12, Industrial Area, Ghaziabad, UP 201001", L, "black"),
        ((70, 1190), "fssai  Lic. No. 10012345678901", font(EN_B, 44), "black"),
        ((70, 1280), "Consumer Care: Quality Manager, Sunrise Foods Pvt Ltd", L, "black"),
        ((70, 1340), "Plot 12, Industrial Area, Ghaziabad, UP 201001", L, "black"),
        ((70, 1400), "Ph: 1800 123 4567  Email: care@sunrisefoods.in", L, "black"),
    ])
    img.save(OUT / "food_label.png")


def good_label_pdf():
    """Vector PDF artwork of the good label, page = 120 x 90 mm (like a printer's file)."""
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    pdfmetrics.registerFont(TTFont("DejaVu", EN))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", EN_B))
    c = canvas.Canvas(str(OUT / "good_label.pdf"), pagesize=(100 * mm, 75 * mm))
    pt = lambda px: px / 12 * mm  # sample px (12 px = 1 mm) -> PDF points
    rows = [(40, "SUNRISE Roasted Peanuts", "DejaVu-Bold", 64), (190, "Net Wt. 500 g", "DejaVu-Bold", 56),
            (320, "MRP Rs. 120.00 (Incl. of all taxes)", "DejaVu", 34),
            (368, "Unit sale price: Rs. 0.24 per g", "DejaVu", 28), (412, "Mfg. Date: 08/2026", "DejaVu", 28),
            (470, "Manufactured by: Sunrise Foods Pvt Ltd", "DejaVu", 28),
            (510, "Plot 12, Industrial Area, Ghaziabad, UP 201001", "DejaVu", 28),
            (610, "Consumer Care: Quality Manager, Sunrise Foods Pvt Ltd", "DejaVu", 26),
            (650, "Plot 12, Industrial Area, Ghaziabad, UP 201001", "DejaVu", 26),
            (690, "Ph: 1800 123 4567  Email: care@sunrisefoods.in", "DejaVu", 26)]
    for top_px, text, fnt, px in rows:
        c.setFont(fnt, pt(px))
        c.drawString(5 * mm, 75 * mm - pt(top_px) - pt(px) * 0.95, text)
    c.showPage()
    c.save()


def tilted_label():
    """Good label photographed slightly tilted, with mild blur: tests auto-straighten."""
    img = Image.open(OUT / "good_label.png").rotate(-3, expand=False, fillcolor="white",
                                                     resample=Image.BICUBIC)
    from PIL import ImageFilter
    img.filter(ImageFilter.GaussianBlur(0.8)).save(OUT / "tilted_label.jpg", "JPEG", quality=90)


LISTING_MATCH = """SUNRISE Roasted Peanuts
Net Wt. 500 g
MRP Rs. 120.00 (Incl. of all taxes)
Mfg. Date: 08/2026
Manufactured by: Sunrise Foods Pvt Ltd, Plot 12, Industrial Area, Ghaziabad, UP 201001
Consumer Care: Ph 1800 123 4567, care@sunrisefoods.in, Ghaziabad 201001
Country of origin: India
"""

LISTING_MISMATCH = """SUNRISE Roasted Peanuts
Net Wt. 400 g
MRP Rs. 99
Seller: Sunrise Foods
"""

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    good_label()
    bad_label()
    low_contrast_label()
    bilingual_label()
    good_label_pdf()
    food_label()
    tilted_label()
    (OUT / "listing_match.txt").write_text(LISTING_MATCH)
    (OUT / "listing_mismatch.txt").write_text(LISTING_MISMATCH)
    print("Samples written to", OUT)
