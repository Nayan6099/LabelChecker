"""Rules engine: turns OCR + extracted fields into findings.

Design rule: the OCR/AI layer only READS. Every pass/fail decision is made
here, in plain code, against a rule in rules_data.py. That keeps verdicts
repeatable and auditable.

Finding status values:
  pass     - check passed
  fail     - check failed
  warn     - likely problem, or read with low confidence
  manual   - needs a human to confirm (cannot be judged from the image)
  na       - not applicable / exempt for this product category
"""
import re

import cv2
import numpy as np

from . import rules_data as R

STATUS_ORDER = {"fail": 0, "warn": 1, "manual": 2, "pass": 3, "na": 4}


def _finding(rule, status, detail, box=None, method="automated", measured=None):
    meta = R.RULES[rule]
    return {
        "rule": rule,
        "title": meta["title"],
        "status": status,
        "detail": detail,
        "box": box,
        "method": method,          # automated | approximate | manual | heuristic
        "source": meta["source"],  # see rules_data.SOURCE_LABELS
        "measured": measured,
    }


# ---------- measurement helpers ----------

def mm_per_px(opts, ocr):
    """Scale from the physical panel size the user enters. None if unknown."""
    w_mm, h_mm = opts.get("panel_width_mm"), opts.get("panel_height_mm")
    scales = []
    if w_mm:
        scales.append(w_mm / ocr["width"])
    if h_mm:
        scales.append(h_mm / ocr["height"])
    return sum(scales) / len(scales) if scales else None


def panel_area_cm2(opts):
    w, h = opts.get("panel_width_mm"), opts.get("panel_height_mm")
    return (w * h) / 100.0 if w and h else None


def _rel_lum(v):
    c = v / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def contrast_ratio(img_bgr, box):
    """Contrast between text pixels and background inside a box (WCAG formula)."""
    x, y, w, h = box
    pad = max(2, int(h * 0.15))
    H, W = img_bgr.shape[:2]
    crop = img_bgr[max(0, y - pad):min(H, y + h + pad), max(0, x - pad):min(W, x + w + pad)]
    if crop.size == 0:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    a, b = gray[mask == 255], gray[mask == 0]
    if a.size == 0 or b.size == 0:
        return 1.0
    la, lb = _rel_lum(float(np.median(a))), _rel_lum(float(np.median(b)))
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 2)


def _intersects(a, b):
    return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0] or
                a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])


# ---------- checks ----------

def check_label(ocr, ext, img, opts):
    cat = R.CATEGORIES.get(opts.get("category") or "general", R.CATEGORIES["general"])
    skip = cat["skip"]
    scale = mm_per_px(opts, ocr)
    area = panel_area_cm2(opts)
    embossed = bool(opts.get("embossed"))
    out = []

    def exempt(rule):
        if rule in skip:
            out.append(_finding(rule, "na", skip[rule], method="manual"))
            return True
        return False

    parties = ext["parties"]
    imported = bool(opts.get("imported")) or "importer" in parties
    text = ocr["text"]

    # 6(1)(a) name and complete address
    if not exempt("6(1)(a)"):
        out.append(_party_finding("6(1)(a)", parties, imported))

    # 6(1)(aa) country of origin for imported goods
    if imported and not exempt("6(1)(aa)"):
        o = ext["origin"]
        out.append(_finding("6(1)(aa)", "pass" if o else "fail",
                            f"Country of origin read: {o['value']}." if o else
                            "Imported product but no country of origin, manufacture or assembly read.",
                            o and o["box"]))

    # 6(1)(b) generic name
    name = (opts.get("product_name") or "").strip()
    from .extract import generic_name_present
    present = generic_name_present(name, text)
    if present is None:
        out.append(_finding("6(1)(b)", "manual",
                            "Enter the product's common or generic name to check it is on the label.",
                            method="manual"))
    elif present:
        out.append(_finding("6(1)(b)", "pass", f"'{name}' was found in the label text."))
    else:
        out.append(_finding("6(1)(b)", "fail", f"'{name}' was not found in the label text."))

    # 6(1)(c) net quantity
    nq = ext["net_quantity"]
    if not nq:
        out.append(_finding("6(1)(c)", "fail", "No net quantity with a standard unit was read."))
    elif nq["value"] is None:
        out.append(_finding("6(1)(c)", "warn", nq["note"], nq["box"]))
    elif not nq["keyword_found"]:
        out.append(_finding("6(1)(c)", "warn",
                            f"Quantity '{nq['value']}' read, but no 'Net quantity' / 'Net Wt.' "
                            "wording next to it.", nq["box"]))
    else:
        out.append(_finding("6(1)(c)", "pass", f"Net quantity read: {nq['value']}.", nq["box"]))

    # 12(6) exaggerating words with the quantity
    q = ext.get("quantity_qualifier")
    if q:
        out.append(_finding("12(6)", "warn", f"The word '{q['word']}' appears with the quantity "
                            "declaration; words that exaggerate the quantity are not allowed.", q["box"]))

    # 6(1)(d) month and year of manufacture
    if not exempt("6(1)(d)"):
        d = ext["date"]
        anywhere = " For this category the date may be anywhere on the pack." if cat.get("date_anywhere") else ""
        if not d:
            out.append(_finding("6(1)(d)", "fail", "No month and year of manufacture was read." + anywhere))
        elif not d["value"]:
            out.append(_finding("6(1)(d)", "warn", d.get("note", "Date not readable."), d["box"]))
        elif d["date_kind"] != "manufacture":
            out.append(_finding("6(1)(d)", "warn",
                                f"Only a month/year of {d['date_kind']} ({d['value']}) was read. Since 1 Apr "
                                "2022 the month and year of manufacture is required.", d["box"]))
        else:
            out.append(_finding("6(1)(d)", "pass", f"Month/year of manufacture: {d['value']}.", d["box"]))

    # 6(1)(da) best before / use by - only where the product can become unfit
    if not exempt("6(1)(da)"):
        e = ext.get("expiry")
        if e and e["value"]:
            out.append(_finding("6(1)(da)", "pass", f"{e['wording'].title()} read: {e['value']}.", e["box"]))
        else:
            out.append(_finding("6(1)(da)", "manual",
                                "No best before / use by date read. Needed only if the product can become "
                                "unfit for use after some time; confirm by hand.",
                                e and e["box"], method="manual"))

    # 6(1)(e) MRP inclusive of all taxes
    mrp = ext["mrp"]
    if not exempt("6(1)(e)"):
        if not mrp:
            out.append(_finding("6(1)(e)", "fail", "No MRP / retail sale price declaration was read."))
        elif mrp["amount"] is None:
            out.append(_finding("6(1)(e)", "warn", mrp.get("note", "MRP amount not readable."), mrp["box"]))
        elif not (mrp.get("inclusive_of_taxes") or ext.get("tax_inclusive")):
            out.append(_finding("6(1)(e)", "fail",
                                f"MRP Rs {mrp['amount']:g} read, but no 'inclusive of all taxes' wording.",
                                mrp["box"]))
        else:
            out.append(_finding("6(1)(e)", "pass",
                                f"MRP Rs {mrp['amount']:g}, inclusive of all taxes.", mrp["box"]))

    # 6(1)(f) dimensions
    out.append(_finding("6(1)(f)", "manual", "Only needed when the size of the commodity matters "
                        "(for example textiles, sheets). Confirm by hand.", method="manual"))

    # 6(2) consumer care: name, address, phone AND e-mail
    out.append(_care_finding(ext["consumer_care"]))

    # 6(3) stickers
    out.append(_finding("6(3)", "manual", "Check by hand that no sticker changes a declaration, except a "
                        "lower MRP sticker that does not cover the original MRP.", method="manual"))

    # 6(11) unit sale price
    if not exempt("6(11)"):
        out += _usp_findings(ext, nq, mrp)

    # 7(2) height of numerals and letters (Table-I by panel area)
    out += _heights(ext, nq, scale, area, embossed, ocr["words"])
    out.append(_finding("7(3)", "manual", "Check by hand that letters and numerals are at least one third as "
                        "wide as they are high (narrow, condensed fonts can fail this).", method="manual"))

    # 8(1) principal display panel + clear space
    out.append(_finding("8(1)", "manual", "Confirm the uploaded image is the principal display panel.",
                        method="manual"))
    out.append(_clear_space(nq, ocr))

    # 9(1) contrast for MRP and net quantity numerals
    for label, fld in (("MRP", mrp), ("Net quantity", nq)):
        box = fld and (fld.get("numeral_box") or fld.get("box"))
        if not box:
            continue
        ratio = contrast_ratio(img, box)
        ok = ratio is not None and ratio >= R.MIN_CONTRAST_RATIO
        out.append(_finding(
            "9(1)", "pass" if ok else "fail",
            f"{label} numerals contrast ratio {ratio}:1 (our threshold {R.MIN_CONTRAST_RATIO}:1; "
            "the rule gives no number, so this threshold is our own).",
            box, method="heuristic", measured={"contrast_ratio": ratio}))

    # 9(4) language
    sc = ext["scripts"]
    if sc["latin"] + sc["devanagari"] == 0:
        out.append(_finding("9(4)", "fail", "No English or Hindi (Devanagari) text was read."))
    else:
        langs = [n for n, k in (("English", "latin"), ("Hindi", "devanagari")) if sc[k] > 20]
        out.append(_finding("9(4)", "pass", "Declarations in " + (" and ".join(langs) or "English/Hindi") + "."))

    if cat.get("fssai"):
        out += check_fssai(ocr, ext, img, opts, scale, area, embossed, imported)
    return out


def _party_finding(rule, parties, imported):
    if not parties:
        return _finding(rule, "fail", "No 'Manufactured by', 'Packed by', 'Marketed by' or 'Imported by' "
                        "declaration was read.")
    roles = ", ".join(sorted(parties))
    main = parties.get("manufacturer") or parties.get("packer") or parties.get("importer") or \
        parties.get("marketer")
    problems = []
    if imported and "importer" not in parties:
        problems.append("package is imported but no importer name/address read")
    if not main["has_pin_code"]:
        problems.append("no PIN code read; a complete address must include the PIN code")
    if not main["name_after_keyword"]:
        problems.append("keyword found but no name read after it")
    if rule == "6(1)(a)" and "marketer" in parties and not ({"manufacturer", "packer"} & set(parties)):
        problems.append("only a marketer is named; the rule requires the manufacturer (and packer if different)")
    if problems:
        return _finding(rule, "warn", f"Found: {roles}. " + "; ".join(problems) + ".", main["box"])
    return _finding(rule, "pass", f"Found: {roles}, with address and PIN code.", main["box"])


def _care_finding(cc):
    if not cc:
        return _finding("6(2)", "fail", "No consumer complaint contact was read (name, address, phone and "
                        "e-mail are all required).")
    missing = [n for n, ok in (("telephone number", cc["phone"]), ("e-mail address", cc["email"]),
                               ("address with PIN code", cc["has_address"])) if not ok]
    found = [x for x in (cc["phone"] and f"phone {cc['phone']}", cc["email"] and f"e-mail {cc['email']}") if x]
    detail = ("Found " + ", ".join(found) + "." if found else "Contact heading found.")
    if not missing:
        return _finding("6(2)", "pass", detail + " Address with PIN code near the contact.", cc["box"])
    status = "fail" if (not cc["phone"] or not cc["email"]) else "warn"
    return _finding("6(2)", status, detail + " Missing: " + ", ".join(missing) + ".", cc["box"])


def _usp_findings(ext, nq, mrp):
    usp = ext.get("usp")
    if usp and usp["amount"] is not None:
        out = [_finding("6(11)", "pass", f"Unit sale price read: {usp['value']}.", usp["box"])]
        if nq and nq.get("unit_kind") in ("mass", "volume", "length") and nq.get("base_value"):
            v, kind = nq["base_value"], nq["unit_kind"]
            small_u, big_u, one = {"mass": ("g", "kg", 1000), "volume": ("ml", "l", 1000),
                                   "length": ("cm", "m", 100)}[kind]
            # GSR 226(E): per g below 1 kg, per kg above 1 kg. Exactly 1 kg is not covered by
            # the text, so either unit is accepted there (our reading).
            allowed = {small_u, big_u} if v == one else ({small_u} if v < one else {big_u})
            if usp["per"] not in allowed:
                out.append(_finding("6(11)-format", "fail",
                                    f"Unit sale price is per {usp['per']}; for {nq['value']} the rule requires "
                                    f"per {' or per '.join(sorted(allowed))} (GSR 226(E)).", usp["box"]))
        return out
    if usp:
        return [_finding("6(11)", "warn", usp["note"], usp["box"])]
    if nq and nq.get("base_value") in (1000.0,) and nq.get("unit_kind") in ("mass", "volume") or \
            (nq and nq.get("unit_kind") == "number" and nq.get("base_value") == 1):
        return [_finding("6(11)", "pass", f"Pack is exactly one unit ({nq['value']}), so the unit sale price "
                         "equals the MRP and is not required.")]
    if not mrp:
        return [_finding("6(11)", "manual", "No MRP read, so the unit sale price was not checked.",
                         method="manual")]
    return [_finding("6(11)", "fail", "No unit sale price read (for example 'Rs 0.24 per g'). Required "
                     "unless the MRP equals it, or the pack is a combination / group / multi-piece pack.")]


_MEASURABLE = re.compile(r"^[A-Za-z0-9.,:/\-]+$")
_TALL = re.compile(r"[A-Zbdfhklt0-9]")
_DESC = re.compile(r"[gjpqyQ,]")


def _glyph_height(words, box):
    """Cap / numeral height (px) of text inside a declaration box. Uses words made of capitals,
    digits or tall letters without descenders (g, j, p, q, y), whose ink box = letter height.
    Returns None when no such word is found."""
    if not box:
        return None
    x, y, w, h = box
    hs = []
    for wd in words:
        bx, by, bw, bh = wd["box"]
        cx, cy = bx + bw / 2, by + bh / 2
        if not (x <= cx <= x + w and y <= cy <= y + h):
            continue
        t = wd["text"].strip(".,:")
        if len(t) >= 2 and _MEASURABLE.match(t) and _TALL.search(t) and not _DESC.search(t):
            hs.append(bh)
    return min(hs) if hs else None


def _decl_heights(ext, words):
    """(label, height_px, box, exact) for each declaration found."""
    hs = []
    fields = [(k.replace("_", " "), ext.get(k)) for k in ("net_quantity", "mrp", "date", "usp", "expiry")]
    fields += list(ext["parties"].items()) + [("consumer care", ext.get("consumer_care"))]
    for label, f in fields:
        if not f or not f.get("box"):
            continue
        g = _glyph_height(words, f["box"])
        if g:
            hs.append((label, g, f["box"], True))
        else:
            line_h = f.get("min_line_height") or f["box"][3]
            hs.append((label, line_h * 0.75, f["box"], False))  # rough: line box includes descenders
    return hs


def _heights(ext, nq, scale, area, embossed, words):
    if not scale or not area:
        return [_finding("7(2)", "manual", "Enter the panel width and height in mm (or upload PDF artwork) "
                         "so text heights can be measured against Table-I.", method="manual")]
    need, row = R.lm_min_height_mm(area, embossed)
    kind = "blown/moulded" if embossed else "printed"
    out = []
    if nq and nq.get("numeral_box"):
        got = round(nq["numeral_box"][3] * scale, 2)
        st = "pass" if got >= need else ("warn" if got >= need * 0.9 else "fail")
        out.append(_finding("7(2)", st,
                            f"Net quantity numerals about {got} mm high; Table-I ({row}, {area:.0f} cm2, {kind}) "
                            f"requires at least {need} mm." + (" Within 10% of the limit; re-measure."
                                                               if st == "warn" else ""),
                            nq["numeral_box"], method="approximate",
                            measured={"height_mm": got, "required_mm": need}))
    hs = _decl_heights(ext, words)
    if hs:
        label, px, box, exact = min(hs, key=lambda x: x[1])
        got = round(px * scale, 2)
        st = "pass" if got >= need else ("warn" if got >= need * 0.9 else "fail")
        how = "capital / numeral height" if exact else "estimated from the line height"
        out.append(_finding("7(2)", st,
                            f"Smallest declaration text ({label}) about {got} mm ({how}); Table-I requires "
                            f"{need} mm for all declarations." + (" Close to the limit; re-measure."
                                                                  if st == "warn" else ""),
                            box, method="approximate", measured={"height_mm": got, "required_mm": need}))
    if not out:
        out.append(_finding("7(2)", "manual", "No declarations located to measure.", method="manual"))
    return out


def _clear_space(nq, ocr):
    if not nq or not nq.get("box") or not nq.get("numeral_box") or nq.get("value") is None:
        return _finding("8(1)", "manual", "Net quantity not fully read, so clear space not checked.",
                        method="manual")
    x, y, w, h = nq.get("decl_box") or nq["box"]
    nh = nq["numeral_box"][3]
    zone = [x - 2 * nh, y - nh, w + 4 * nh, h + 2 * nh]
    own = {tuple(b) for b in (nq.get("decl_words") or [])}
    intruders = [wd["text"] for wd in ocr["words"]
                 if tuple(wd["box"]) not in own and _intersects(wd["box"], zone)]
    if intruders:
        return _finding("8(1)", "fail", "Other printed text inside the clear zone around the net quantity: "
                        + ", ".join(f"'{t}'" for t in intruders[:6]) + ".", zone, method="approximate")
    return _finding("8(1)", "pass", "Clear zone around the net quantity (numeral height above and below, "
                    "twice left and right) is free of other text.", zone, method="approximate")


# ---------------- FSSAI (food) ----------------

def cat_symbol_exempt(opts):
    return bool(R.CATEGORIES.get(opts.get("category") or "", {}).get("symbol_exempt"))


def check_fssai(ocr, ext, img, opts, scale, area, embossed, imported):
    out = []
    small = area is not None and area <= R.FSSAI_SMALL_PACK_CM2

    def small_exempt(rule):
        return False  # decided after the check: see the end of this function

    # 5(1) name of food
    name = (opts.get("product_name") or "").strip()
    from .extract import generic_name_present
    p = generic_name_present(name, ocr["text"])
    out.append(_finding("FSS 5(1)", "manual" if p is None else ("pass" if p else "fail"),
                        "Enter the name of the food to check it." if p is None else
                        (f"'{name}' found on the label." if p else f"'{name}' not found on the label."),
                        method="manual" if p is None else "automated"))

    # 5(2) ingredients
    ing = ext.get("ingredients")
    if not small_exempt("FSS 5(2)"):
        if ing:
            out.append(_finding("FSS 5(2)", "pass", "List of ingredients found. Check by hand: descending order, "
                                "added water, specific oil names, % of highlighted ingredients.", ing["box"]))
        else:
            out.append(_finding("FSS 5(2)", "fail", "No 'Ingredients' list read. Not needed only for "
                                "single-ingredient foods; mark OK if that applies."))

    # 5(3) nutrition
    if not small_exempt("FSS 5(3)"):
        n = ext.get("nutrition")
        need = ["energy", "protein", "carbohydrate", "total sugars", "added sugars", "total fat",
                "saturated fat", "trans fat", "sodium"]
        if not n:
            out.append(_finding("FSS 5(3)", "fail", "No nutritional information read. Exempt only for some "
                                "foods (e.g. single-ingredient, salt, tea, coffee, drinking water)."))
        else:
            missing = [x for x in need if x not in n["nutrients"]]
            extra = []
            if not n["per_100"]:
                extra.append("values per 100 g / 100 ml not found")
            if not n["rda"]:
                extra.append("per-serve %RDA not found")
            if missing or extra:
                out.append(_finding("FSS 5(3)", "warn", "Nutrition table found but " +
                                    "; ".join(([f"missing: {', '.join(missing)}"] if missing else []) + extra) +
                                    ".", n["box"]))
            else:
                out.append(_finding("FSS 5(3)", "pass", "Nutrition table with all required nutrients, per 100 "
                                    "g/ml and %RDA.", n["box"]))

    # 5(4) veg / non-veg symbol
    from .symbols import find_food_symbol
    sym = find_food_symbol(img)
    if cat_symbol_exempt(opts):
        out.append(_finding("FSS 5(4)", "na", "Veg / non-veg symbol not required for drinking water, carbonated "
                            "water, alcoholic beverages, liquid milk, milk powder and honey.", method="manual"))
    elif not sym:
        out.append(_finding("FSS 5(4)", "warn", "Veg (green) or non-veg (brown) symbol not detected. Check by "
                            "hand that it is on the principal display panel near the name."))
    else:
        detail = f"{'Veg (green)' if sym['type'] == 'veg' else 'Non-veg (brown)'} symbol found."
        st = "pass"
        if scale and area:
            req, row = R.fssai_symbol_mm(area)
            side = round(min(sym["box"][2], sym["box"][3]) * scale, 1)
            detail += f" Square about {side} mm; minimum {req['square']} mm ({row})."
            if side < req["square"] * 0.9:
                st = "fail"
        out.append(_finding("FSS 5(4)", st, detail, sym["box"], method="approximate"))

    out.append(_finding("FSS 5(5)", "manual", "Check by hand that additives are declared by class with name or "
                        "INS number, e.g. 'Emulsifier (INS 322)'.", method="manual"))

    # 5(6) name and address
    out.append(_party_finding("FSS 5(6)", ext["parties"], imported))

    # 5(7) FSSAI logo + licence number
    if not small_exempt("FSS 5(7)"):
        f = ext.get("fssai")
        if not f:
            out.append(_finding("FSS 5(7)", "fail", "No FSSAI logo text or licence number read."))
        elif not f["licence"]:
            out.append(_finding("FSS 5(7)", "fail", "'FSSAI' read but no 14-digit licence number.", f["box"]))
        else:
            out.append(_finding("FSS 5(7)", "pass" if f["word_found"] else "warn",
                                f"Licence number {f['licence']} read." +
                                ("" if f["word_found"] else " FSSAI logo not read; check it by hand."), f["box"]))

    # 5(9) lot / batch
    if not small_exempt("FSS 5(9)"):
        b = ext.get("batch")
        out.append(_finding("FSS 5(9)", "pass" if b else "fail",
                            f"Batch / lot read: {b['value']}." if b else "No batch, lot or code number read.",
                            b and b["box"]))

    # 5(10) date of manufacture/packing + expiry/use by
    d, e = ext.get("date"), ext.get("expiry")
    problems = []
    if not d or not d.get("value"):
        problems.append("date of manufacture or packing not read")
    if not e or not e.get("value"):
        problems.append("expiry / use by date not read")
    elif e["kind"] == "best before":
        problems.append("only 'Best before' read; FSSAI needs 'Expiry' or 'Use by' (best before is optional extra)")
    if problems:
        out.append(_finding("FSS 5(10)", "fail" if len(problems) > 1 or not e else "warn",
                            "; ".join(problems).capitalize() + ". Expiry is not needed for a few foods "
                            "(e.g. vinegar, salt, sugar, chewing gum).", (e or d or {}).get("box")))
    else:
        out.append(_finding("FSS 5(10)", "pass", f"Date {d['value']} and {e['wording']} {e['value']} read.",
                            e["box"]))

    if imported:
        o = ext["origin"]
        out.append(_finding("FSS 5(11)", "pass" if o else "fail",
                            f"Country of origin: {o['value']}." if o else "Imported food without country of origin.",
                            o and o["box"]))

    # 5(14) allergens
    al = ext.get("allergens") or {}
    if al.get("in_ingredients"):
        out.append(_finding("FSS 5(14)", "pass" if al["statement"] else "fail",
                            f"Ingredients include {', '.join(al['in_ingredients'])}; " +
                            ("an allergen / 'Contains' statement was read." if al["statement"] else
                             "no separate 'Contains: ...' allergen statement read.")))
    else:
        out.append(_finding("FSS 5(14)", "manual", "No common allergen read in the ingredients. Confirm by hand.",
                            method="manual"))

    # 6(3) FSSAI minimum font size
    if scale and area:
        need, row = R.fssai_min_font_mm(area, embossed)
        hs = _decl_heights(ext, ocr["words"])
        if hs:
            label, px, box, _ = min(hs, key=lambda x: x[1])
            got = round(px * scale, 2)
            st = "pass" if got >= need else ("warn" if got >= need * 0.9 else "fail")
            out.append(_finding("FSS 6(3)", st, f"Smallest declaration text ({label}) about {got} mm; FSSAI "
                                f"Table I ({row}) needs {need} mm. Measured from OCR boxes.", box,
                                method="approximate", measured={"height_mm": got, "required_mm": need}))

    # Reg 8(1): packages with a surface area of 100 cm2 or less are exempt from ingredients, lot, nutrition,
    # additives, licence number and logo, importer address and instructions; below 30 cm2 also from the
    # dates (8(1)(a)) - if the multi-unit pack carries them. We only know the front panel, so a missing
    # item on a small panel becomes a manual check, never N/A.
    if small:
        small_rules = {"FSS 5(2)", "FSS 5(3)", "FSS 5(7)", "FSS 5(9)"}
        if area < 30:
            small_rules.add("FSS 5(10)")
        for f in out:
            if f["rule"] in small_rules and f["status"] in ("fail", "warn"):
                f["status"], f["method"] = "manual", "manual"
                f["detail"] += (f" Front panel is about {area:.0f} cm2: if the whole package surface is "
                                f"{'under 30' if f['rule'] == 'FSS 5(10)' else '100 cm2 or less'} this item is exempt "
                                "(Reg 8(1)) provided it is on the multi-unit pack.")
    return out


def check_listing(listing_ext, label_ext, opts):
    """Rule 6(10): the listing must show the Rule 6(1) declarations (except month/year of manufacture)
    and match the label."""
    out = []
    imported = bool(opts.get("imported")) or "importer" in (label_ext.get("parties") or {})
    need = [("Net quantity", listing_ext.get("net_quantity")), ("MRP", listing_ext.get("mrp")),
            ("Manufacturer / packer / importer name and address", listing_ext.get("parties"))]
    if imported:
        need.append(("Country of origin", listing_ext.get("origin")))
    name = (opts.get("product_name") or "").strip()
    if name:
        from .extract import generic_name_present
        need.append((f"Generic name '{name}'", generic_name_present(name, listing_ext.get("text") or "")))
    for label, v in need:
        if not v:
            out.append(_finding("6(10)", "fail", f"{label} not found in the listing."))
    if not listing_ext.get("consumer_care"):
        out.append(_finding("6(10)", "warn", "Consumer care details not found in the listing (expected in "
                            "practice; whether Rule 6(10) requires them is not settled).", method="heuristic"))
    a, b = listing_ext.get("mrp"), label_ext.get("mrp")
    if a and b and a.get("amount") and b.get("amount") and abs(a["amount"] - b["amount"]) > 0.01:
        out.append(_finding("6(10)", "fail", f"MRP differs: listing Rs {a['amount']:g}, label Rs {b['amount']:g}."))
    a, b = listing_ext.get("net_quantity"), label_ext.get("net_quantity")
    if a and b and a.get("base_value") is not None and b.get("base_value") is not None \
            and abs(a["base_value"] - b["base_value"]) > 1e-6:
        out.append(_finding("6(10)", "fail", f"Net quantity differs: listing {a['value']}, label {b['value']}."))
    if not out:
        out.append(_finding("6(10)", "pass", "Listing shows the checked declarations and matches the label."))
    return out


def summarize(findings):
    counts = {k: 0 for k in STATUS_ORDER}
    for f in findings:
        counts[f["status"]] += 1
    if counts["fail"]:
        verdict = "issues_found"
    elif counts["warn"] or counts["manual"]:
        verdict = "needs_review"
    else:
        verdict = "no_issues_found"
    return {"counts": counts, "verdict": verdict}


def _assign_keys(findings):
    """Stable id per finding (rule + occurrence), used to store reviewer decisions
    and to compare versions. Assigned before sorting, so it follows check order."""
    seen = {}
    for f in findings:
        n = seen.get(f["rule"], 0) + 1
        seen[f["rule"]] = n
        f["key"] = f"{f['rule']}~{n}"


def listing_unavailable(reason):
    f = _finding("6(10)", "manual", f"Listing could not be fetched: {reason}", method="manual")
    f["key"] = "6(10)~fetch"
    return f


def run(ocr, ext, img, opts, listing_ext=None):
    findings = check_label(ocr, ext, img, opts)
    if listing_ext is not None:
        findings += check_listing(listing_ext, ext, opts)
    _assign_keys(findings)
    findings.sort(key=lambda f: STATUS_ORDER[f["status"]])
    return {"findings": findings, "summary": summarize(findings),
            "ruleset": {"version": R.RULESET_VERSION, "note": R.RULESET_NOTE}}


# ---------- version comparison ----------

FIELDS = [("Net quantity", lambda e: (e.get("net_quantity") or {}).get("value")),
          ("MRP", lambda e: (e.get("mrp") or {}).get("value")),
          ("Month / year", lambda e: (e.get("date") or {}).get("value")),
          ("Consumer care phone", lambda e: (e.get("consumer_care") or {}).get("phone")),
          ("Consumer care e-mail", lambda e: (e.get("consumer_care") or {}).get("email"))]


def _worst(findings):
    """Worst status per rule, e.g. {'7(2)': 'fail'}."""
    out = {}
    for f in findings:
        cur = out.get(f["rule"])
        if cur is None or STATUS_ORDER[f["status"]] < STATUS_ORDER[cur["status"]]:
            out[f["rule"]] = f
    return out


def compare(old, new):
    """What changed between two scans of the same product (old = earlier version)."""
    a, b = _worst(old["result"]["findings"]), _worst(new["result"]["findings"])
    rows = []
    for rule in sorted(set(a) | set(b), key=lambda r: (r not in b, r)):
        fa, fb = a.get(rule), b.get(rule)
        sa, sb = (fa or {}).get("status"), (fb or {}).get("status")
        if sa == sb:
            change = "unchanged"
        elif sa in ("fail", "warn") and sb in ("pass", "na", None):
            change = "fixed"
        elif sb in ("fail", "warn") and sa not in ("fail", "warn"):
            change = "new_issue"
        elif sa == "fail" and sb == "warn":
            change = "improved"
        elif sa == "warn" and sb == "fail":
            change = "worse"
        else:
            change = "changed"
        f = fb or fa
        rows.append({"rule": rule, "title": f["title"], "old": sa, "new": sb, "change": change,
                     "old_detail": (fa or {}).get("detail"), "new_detail": (fb or {}).get("detail")})
    fields = []
    for name, get in FIELDS:
        va, vb = get(old["extracted"] or {}), get(new["extracted"] or {})
        fields.append({"field": name, "old": va, "new": vb, "changed": va != vb})
    summary = {k: sum(1 for r in rows if r["change"] == k)
               for k in ("fixed", "new_issue", "improved", "worse", "unchanged", "changed")}
    return {"old": {"id": old["id"], "version": old["version"], "verdict": old["verdict"]},
            "new": {"id": new["id"], "version": new["version"], "verdict": new["verdict"]},
            "rules": rows, "fields": fields, "summary": summary}
