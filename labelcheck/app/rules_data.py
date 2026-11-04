"""Versioned rule set: Legal Metrology (Packaged Commodities) Rules, 2011 AS AMENDED up to
GSR 826(E) of 21 Sep 2026, plus FSSAI Labelling and Display Regulations, 2020 (food only).

Every check in the engine points to an entry here, so a finding can always show which
rule it comes from and how much we trust that source.

SOURCE STATUS
- verified_2011       read from the original 2011 Gazette text.
- verified_amendment  read from the official amendment notification (consumeraffairs.gov.in),
                      see docs/research/legal-metrology-amendments.md.
- unverified_amendment  amendment known, official text not obtained (shown as advisory, never FAIL).
- fssai_2020          FSSAI Labelling & Display Regulations 2020, compendium Version VIII
                      (09.09.2025) + amendment of 24.03.2026; see docs/research/fssai-labelling.md.
- heuristic           our own engineering threshold where the rule gives no number.
"""

RULESET_VERSION = "LM-PCR-2011-amended-2026.09-r2 + FSSAI-LD-2020-v8-r2"
RULESET_NOTE = (
    "Legal Metrology (Packaged Commodities) Rules, 2011 as amended up to GSR 826(E) "
    "(21 Sep 2026); for food, FSSAI Labelling and Display Regulations 2020 (compendium "
    "Version VIII, Sep 2025). Rule texts checked against the official notifications on "
    "2-3 Nov 2026 (see docs/research)."
)

SOURCE_LABELS = {
    "verified_2011": "2011 rules text",
    "verified_amendment": "Amendment text (read)",
    "unverified_amendment": "Amendment (secondary source)",
    "fssai_2020": "FSSAI Labelling & Display Regs 2020",
    "heuristic": "Engineering threshold",
}


def _r(title, text, source):
    return {"title": title, "text": text, "source": source}


RULES = {
    # ---------- Legal Metrology ----------
    "6(1)(a)": _r("Name and complete address of manufacturer / packer / importer",
                  "Name and complete address (including PIN code, Rule 10 as amended 2017) of the "
                  "manufacturer; of the packer too if different; of the importer for imported packages.",
                  "verified_amendment"),
    "6(1)(aa)": _r("Country of origin (imported goods)",
                   "Name of the country of origin or manufacture or assembly for imported products "
                   "(GSR 629(E), from 1 Jan 2018).", "verified_amendment"),
    "6(1)(b)": _r("Common or generic name of the commodity",
                  "Common or generic name of the commodity.", "verified_2011"),
    "6(1)(c)": _r("Net quantity in standard units",
                  "Net quantity in standard units of weight or measure, or number/unit/piece/pair/set.",
                  "verified_amendment"),
    "6(1)(d)": _r("Month and year of manufacture",
                  "Month and year in which the commodity is manufactured (GSR 779(E): 'pre-packed or "
                  "imported' removed from 1 Apr 2022).", "verified_amendment"),
    "6(1)(da)": _r("Best before / use by date (where applicable)",
                   "'Best before or use by' date, month and year where the commodity may become unfit "
                   "for consumption after a period (GSR 629(E)).", "verified_amendment"),
    "6(1)(e)": _r("MRP inclusive of all taxes, in Indian currency",
                  "Retail sale price: must clearly say it is the maximum retail price inclusive of all "
                  "taxes, in Indian currency (GSR 629(E), 779(E)).", "verified_amendment"),
    "6(1)(f)": _r("Dimensions (where relevant)",
                  "Where size is relevant, the dimensions of the commodity.", "verified_2011"),
    "6(2)": _r("Consumer complaint contact",
               "Name, address, telephone number and e-mail address of the person or office to contact "
               "for consumer complaints (GSR 385(E), from 1 Jan 2016).", "verified_amendment"),
    "6(3)": _r("No stickers over declarations",
               "No sticker may cover or change a declaration, except a lower MRP sticker.",
               "verified_2011"),
    "6(11)": _r("Unit sale price",
                "Unit sale price, e.g. Rs per g below 1 kg and Rs per kg above (per ml / litre, cm / m, "
                "per unit). Not needed where it equals the MRP, or for combination / group / "
                "multi-piece packs, or alcoholic beverages (State Excise law applies) (GSR 779(E), 226(E), "
                "722(E)).", "verified_amendment"),
    "6(11)-format": _r("Unit sale price unit",
                       "Per gram where net quantity is less than one kilogram and per kilogram where it is "
                       "more than one kilogram; the same for ml / litre and cm / metre (GSR 226(E)). "
                       "Exactly 1 kg / 1 L / 1 m: either unit accepted (the text does not say).",
                       "verified_amendment"),
    "7(2)": _r("Minimum height of numerals and letters (Table-I)",
               "Height of every numeral and letter in the declarations as per Table-I, by principal "
               "display panel area (GSR 629(E), from 1 Jan 2018).", "verified_amendment"),
    "7(3)": _r("Letter width at least one third of height",
               "Width of a letter or numeral not less than one third of its height (except 1, i, I, l).",
               "verified_amendment"),
    "8(1)": _r("Declarations on principal display panel; clear space",
               "Declarations on the principal display panel. Area around the quantity declaration "
               "free of print: numeral height above and below, twice the numeral height left and right.",
               "verified_2011"),
    "9(1)": _r("Legible; MRP and net quantity in contrasting colour",
               "Declarations legible and prominent; MRP and net quantity numerals in a colour "
               "contrasting conspicuously with background.", "verified_2011"),
    "9(4)": _r("Language: Hindi (Devanagari) or English",
               "Declarations in Hindi in Devanagari script or in English; other languages may be added.",
               "verified_2011"),
    "12(6)": _r("No exaggerating words with the quantity",
                "The quantity declaration shall not contain any word likely to exaggerate the quantity "
                "(GSR 784(E), from 1 Jul 2012).", "verified_amendment"),
    "6(10)": _r("E-commerce listing shows the declarations",
                "E-commerce entities must display the mandatory declarations of Rule 6(1) (except month "
                "and year of manufacture) on the listing (GSR 629(E)).", "verified_amendment"),
    # ---------- FSSAI (food) ----------
    "FSS 5(1)": _r("Name of the food", "Name of the food indicating its true nature.", "fssai_2020"),
    "FSS 5(2)": _r("List of ingredients",
                   "Ingredients in descending order by weight or volume; not needed for single-ingredient "
                   "foods; added water declared.", "fssai_2020"),
    "FSS 5(3)": _r("Nutritional information",
                   "Energy, protein, carbohydrate, total sugars, added sugars, total fat, saturated fat, "
                   "trans fat, sodium (cholesterol for animal fat) per 100 g/ml, plus per-serve %RDA.",
                   "fssai_2020"),
    "FSS 5(4)": _r("Veg / non-veg symbol",
                   "Green filled circle in a green-outlined square (veg) or brown filled triangle in a "
                   "brown-outlined square (non-veg) on the principal display panel near the name.",
                   "fssai_2020"),
    "FSS 5(5)": _r("Food additives declared",
                   "Additives declared by functional class with specific name or INS number.", "fssai_2020"),
    "FSS 5(6)": _r("Name and complete address (food)",
                   "Name and complete address of the brand owner with 'Manufactured by / Marketed by / "
                   "Packed by'; importer in India for imported food.", "fssai_2020"),
    "FSS 5(7)": _r("FSSAI logo and licence number",
                   "FSSAI logo and licence number of the brand owner (and importer), contrasting colour. "
                   "Licence / registration numbers have 14 digits (FoSCoS).", "fssai_2020"),
    "FSS 5(9)": _r("Lot / batch / code number", "A batch, code or lot number.", "fssai_2020"),
    "FSS 5(10)": _r("Date of manufacture or packing; expiry / use by",
                    "'Date of manufacture or packaging' and 'Expiry / Use by' are mandatory; 'Best before' "
                    "alone is not enough.", "fssai_2020"),
    "FSS 5(11)": _r("Country of origin (imported food)", "Country of origin for imported food.", "fssai_2020"),
    "FSS 5(14)": _r("Allergen declaration",
                    "Known allergens declared separately, e.g. 'Contains: milk, peanuts'.", "fssai_2020"),
    "FSS 6(3)": _r("FSSAI minimum font size", "Minimum letter height by principal display panel area "
                   "(FSSAI Table I).", "fssai_2020"),
}

# Rule 7(2) Table-I (GSR 629(E)), checked visually against the official PDF on 2 Nov 2026.
# (upper_limit_cm2_inclusive, printed_mm, blown_formed_moulded_mm); None = no upper limit.
LM_TABLE_I = [(50, 1.0, 1.5), (100, 1.5, 3.0), (500, 2.5, 4.0), (2500, 4.0, 6.0), (None, 6.0, 6.0)]

# FSSAI Reg 6(3) Table I (in force 1 Jul 2022).
FSSAI_FONT = [(200, 1.0, 2.0), (500, 2.0, 4.0), (2500, 3.0, 5.0), (None, 6.0, 8.0)]

# FSSAI Reg 5(4) veg/non-veg symbol: (upper cm2, circle diameter, triangle side, square side) in mm.
FSSAI_SYMBOL = [(100, 3.0, 2.5, 6.0), (500, 4.0, 3.5, 8.0), (2500, 6.0, 5.0, 12.0), (None, 8.0, 7.0, 16.0)]

FSSAI_SMALL_PACK_CM2 = 100  # Reg 8(1): exempt from ingredients, nutrition, lot, licence no., etc.

# Heuristic: rule 9(1) says "contrasts conspicuously" but gives no number.
MIN_CONTRAST_RATIO = 3.0

FOOD_LM_NOTE = "For food, this is covered by the FSSAI regulations (Explanation III / proviso to Rule 6(1))."

CATEGORIES = {
    "general": {"label": "General packaged goods", "skip": {}},
    "food": {"label": "Food (FSSAI checks added)", "fssai": True,
             "skip": {"6(1)(a)": FOOD_LM_NOTE + " See FSS 5(6).",
                      "6(1)(d)": FOOD_LM_NOTE + " See FSS 5(10).",
                      "6(1)(da)": FOOD_LM_NOTE + " See FSS 5(10).",
                      "6(1)(aa)": FOOD_LM_NOTE + " See FSS 5(11)."}},
    "cosmetics": {"label": "Cosmetics",
                  "skip": {"6(1)(d)": "For cosmetics, the Drugs and Cosmetics Rules apply "
                                      "(proviso to Rule 6(1)(d))."}},
    "electronics": {"label": "Electronics / spare parts / accessories",
                    "skip": {}, "date_anywhere": True},
    "seeds": {"label": "Seeds (labelled under Seeds Act)",
              "skip": {"6(1)(d)": "Seeds labelled under the Seeds Act are covered by that Act."}},
    "bidi": {"label": "Bidi",
             "skip": {"6(1)(e)": "No retail sale price declaration is required for bidi packages.",
                      "6(11)": "No unit sale price without a retail sale price."}},
    "lpg": {"label": "Domestic LPG cylinder (administered price)",
            "skip": {"6(1)(e)": "No retail sale price declaration for domestic LPG cylinders under "
                                "administered pricing.",
                     "6(11)": "No unit sale price without a retail sale price."}},
    "food_no_symbol": {"label": "Food: drinking / carbonated water, liquid milk, milk powder, honey",
                       "fssai": True, "symbol_exempt": True,
                       "skip": {"6(1)(a)": FOOD_LM_NOTE + " See FSS 5(6).",
                                "6(1)(d)": FOOD_LM_NOTE + " See FSS 5(10).",
                                "6(1)(da)": FOOD_LM_NOTE + " See FSS 5(10).",
                                "6(1)(aa)": FOOD_LM_NOTE + " See FSS 5(11)."}},
    "alcohol": {"label": "Alcoholic beverage",
                "skip": {"6(11)": "Unit sale price for alcoholic beverages follows the State Excise law "
                                  "(first proviso to Rule 6(11))."}},
    "multipack": {"label": "Combination / group / multi-piece pack",
                  "skip": {"6(11)": "No unit sale price for combination, group or multi-piece packages "
                                    "(GSR 722(E))."}},
}


def _row(table, area):
    for row in table:
        if row[0] is None or area <= row[0]:
            return row
    return table[-1]


def _row_label(table, row):
    idx = table.index(row)
    lower = 0 if idx == 0 else table[idx - 1][0]
    if row[0] is None:
        return f"panel above {lower} cm2"
    if idx == 0:
        return f"panel up to {row[0]} cm2"
    return f"panel above {lower} and up to {row[0]} cm2"


def lm_min_height_mm(panel_area_cm2, embossed):
    """Rule 7(2) Table-I: (required_mm, row_label) by panel area."""
    row = _row(LM_TABLE_I, panel_area_cm2)
    return row[2 if embossed else 1], _row_label(LM_TABLE_I, row)


def fssai_min_font_mm(panel_area_cm2, embossed):
    row = _row(FSSAI_FONT, panel_area_cm2)
    return row[2 if embossed else 1], _row_label(FSSAI_FONT, row)


def fssai_symbol_mm(panel_area_cm2):
    row = _row(FSSAI_SYMBOL, panel_area_cm2)
    return {"circle": row[1], "triangle": row[2], "square": row[3]}, _row_label(FSSAI_SYMBOL, row)
