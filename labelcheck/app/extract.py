"""Extract mandatory declarations from OCR lines (label) or plain text (listing).

Each field keeps its evidence: the text it came from and, for labels, the
pixel box, so the UI can highlight it and the report can show it.
"""
import re
import unicodedata

UNIT_KIND = {
    # mass -> grams
    "kg": ("mass", 1000), "kgs": ("mass", 1000), "किग्रा": ("mass", 1000),
    "g": ("mass", 1), "gm": ("mass", 1), "gms": ("mass", 1), "gram": ("mass", 1),
    "grams": ("mass", 1), "ग्राम": ("mass", 1), "mg": ("mass", 0.001),
    # volume -> millilitres
    "l": ("volume", 1000), "ltr": ("volume", 1000), "litre": ("volume", 1000),
    "liter": ("volume", 1000), "लीटर": ("volume", 1000),
    "ml": ("volume", 1), "मिली": ("volume", 1),
    # length -> centimetres
    "m": ("length", 100), "cm": ("length", 1), "mm": ("length", 0.1),
    # number
    "n": ("number", 1), "no": ("number", 1), "nos": ("number", 1),
    "pcs": ("number", 1), "pieces": ("number", 1), "units": ("number", 1),
    "unit": ("number", 1), "tablets": ("number", 1), "count": ("number", 1),
}
_UNITS = sorted(UNIT_KIND, key=len, reverse=True)
_UNIT_RE = "|".join(re.escape(u) for u in _UNITS)

NUM = r"(\d+(?:[.,]\d+)?)"
QTY_RE = re.compile(NUM + r"\s*(" + _UNIT_RE + r")\.?(?![a-z])", re.I)
NET_KW = re.compile(
    r"\b(net\s*(qty|quantity|wt|weight|content|contents|vol|volume|mass)?|"
    r"n\.?\s*wt)\b|शुद्ध\s*(मात्रा|भार|वजन)|कुल\s*मात्रा", re.I)

MRP_KW = re.compile(r"\bm\.?\s*r\.?\s*p\b|maximum\s+retail\s+price|अधिकतम\s*खुदरा\s*मूल्य|एमआरपी", re.I)
PRICE_RE = re.compile(r"(?:₹|rs\.?|inr|रु\.?|र\.)?\s*" + NUM, re.I)

DATE_KW = re.compile(
    r"\b(mfg|mfd|mfg\.?\s*date|date\s+of\s+(manufacture|mfg|packing|import)|"
    r"month\s+and\s+year|manufactured\s+on|packed\s+on|pkd\s*(on|date)?|"
    r"date\s+of\s+pkg|imported\s+on)\b|निर्माण\s*(तिथि|माह)|पैकिंग\s*(तिथि|माह)|आयात\s*(तिथि|माह)",
    re.I)
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_RE = re.compile(
    r"\b((0?[1-9]|1[0-2])\s*[/\-.]\s*(\d{4}|\d{2}))\b|"
    r"\b((" + MONTHS + r")[a-z]*\.?\s*[\-/,]?\s*(\d{4}|\d{2}))\b", re.I)

ROLE_KW = [
    ("importer", re.compile(r"imported\s*(&|and)?\s*(marketed)?\s*by|importer|आयातक|आयातित", re.I)),
    ("packer", re.compile(r"packed\s*by|pkd\.?\s*by|packer|पैककर्ता|द्वारा\s*पैक", re.I)),
    ("marketer", re.compile(r"marketed\s*by|mkt\.?\s*by|mktd\.?\s*by|विपणक|विपणन", re.I)),
    ("manufacturer", re.compile(r"manufactured\s*by|mfd\.?\s*by|mfg\.?\s*by|manufacturer|निर्माता|निर्मित", re.I)),
]
PIN_RE = re.compile(r"\b\d{3}\s?\d{3}\b")

CARE_KW = re.compile(r"consumer\s*care|customer\s*care|complaint|grievance|"
                     r"contact\s*us|उपभोक्ता|शिकायत", re.I)
PHONE_RE = re.compile(r"(?:\+?91[\s\-]?)?(?:[6-9]\d{4}[\s\-]?\d{5}|1800[\s\-]?\d{3}[\s\-]?\d{3,4}|"
                      r"0\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4})")  # landline, e.g. 022 4000 1234
EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")
ORIGIN_RE = re.compile(r"country\s+of\s+origin\s*[:\-]?\s*([a-z ]+)|made\s+in\s+([a-z ]+)|मूल\s*देश", re.I)


def _norm(s):
    return unicodedata.normalize("NFC", s or "")


def _to_float(s):
    return float(s.replace(",", "."))


def _field(value, line, extra=None):
    out = {"value": value, "evidence": line["text"] if line else None,
           "box": line["box"] if line else None}
    if extra:
        out.update(extra)
    return out


def _word_box_for(line, token):
    """Find the box of the word in a line that contains a token (digits)."""
    if not line or "words" not in line:
        return None
    for w in line["words"]:
        if token in w["text"].replace(",", "."):
            return w["box"]
    return None


def _declaration_span(line, number_text, unit_text, has_kw):
    """Words that make up the quantity declaration itself, e.g. 'Net Wt. 500 g'."""
    words = line.get("words") or []
    num_idx = next((i for i, w in enumerate(words)
                    if number_text in w["text"].replace(",", ".")), None)
    if num_idx is None:
        return None, None
    end = num_idx
    if unit_text.lower() not in words[num_idx]["text"].lower() and num_idx + 1 < len(words):
        end = num_idx + 1  # unit is the next word, e.g. '500' 'g'
    start = num_idx
    if has_kw:
        start = next((i for i, w in enumerate(words[:num_idx + 1])
                      if NET_KW.search(" ".join(x["text"] for x in words[i:num_idx + 1]))), num_idx)
    span = words[start:end + 1]
    xs = [w["box"][0] for w in span]
    ys = [w["box"][1] for w in span]
    x2 = [w["box"][0] + w["box"][2] for w in span]
    y2 = [w["box"][1] + w["box"][3] for w in span]
    return [min(xs), min(ys), max(x2) - min(xs), max(y2) - min(ys)], [w["box"] for w in span]


def find_net_quantity(lines):
    best = None
    for i, ln in enumerate(lines):
        text = _norm(ln["text"])
        m = QTY_RE.search(text)
        if not m:
            continue
        has_kw = bool(NET_KW.search(text)) or (i > 0 and NET_KW.search(lines[i - 1]["text"]))
        value, unit = _to_float(m.group(1)), m.group(2).lower()
        kind, factor = UNIT_KIND.get(unit, (None, None))
        if kind is None:
            continue
        decl_box, decl_words = _declaration_span(ln, m.group(1).replace(",", "."), m.group(2), bool(has_kw))
        cand = _field(f"{m.group(1)} {m.group(2)}", ln, {
            "decl_box": decl_box, "decl_words": decl_words,
            "number": value, "unit": unit, "unit_kind": kind,
            "base_value": value * factor,  # grams, ml, cm or count
            "keyword_found": has_kw,
            "numeral_box": _word_box_for(ln, m.group(1).replace(",", ".")),
        })
        if has_kw:
            return cand
        best = best or cand
    if best:
        return best
    # 'Net' wording with a number but no readable unit (OCR often reads 'g' as 6 or 9).
    # Report it as unreadable rather than missing, so a human checks it.
    for ln in lines:
        text = _norm(ln["text"])
        kw = NET_KW.search(text)
        num = kw and re.search(NUM, text[kw.end():])
        if num:
            return _field(None, ln, {
                "number": _to_float(num.group(1)), "unit": None, "unit_kind": None,
                "base_value": None, "keyword_found": True,
                "numeral_box": _word_box_for(ln, num.group(1).replace(",", ".")),
                "decl_box": None, "decl_words": None,
                "note": f"'Net' wording and the number {num.group(1)} were read, but the unit "
                        "could not be read. Check the label by hand.",
            })
    return None


def find_mrp(lines):
    for i, ln in enumerate(lines):
        text = _norm(ln["text"])
        km = MRP_KW.search(text)
        if not km:
            continue
        # price is usually after the keyword, on the same or the next line
        sources = [(text[km.end():], ln)]
        if i + 1 < len(lines):
            sources.append((lines[i + 1]["text"], lines[i + 1]))
        for src, src_line in sources:
            m = PRICE_RE.search(src)
            if m:
                return _field(m.group(1), src_line, {
                    "amount": _to_float(m.group(1)),
                    "numeral_box": _word_box_for(src_line, m.group(1).replace(",", ".")),
                    "inclusive_of_taxes": bool(re.search(r"incl|all\s*taxes|सभी\s*कर", text, re.I)),
                })
        return _field(None, ln, {"amount": None, "note": "MRP keyword found but no amount read"})
    return None


def find_date(lines):
    for i, ln in enumerate(lines):
        text = _norm(ln["text"])
        if not DATE_KW.search(text):
            continue
        for src_ln in (ln, lines[i + 1] if i + 1 < len(lines) else None):
            if src_ln is None:
                continue
            m = DATE_RE.search(src_ln["text"])
            if m:
                kind = ("import" if re.search(r"import|आयात", text, re.I) else
                        "packing" if re.search(r"pkd|pack|पैकिंग", text, re.I) else "manufacture")
                return _field(m.group(0), src_ln, {"date_kind": kind})
        return _field(None, ln, {"note": "Date keyword found but no month/year read"})
    return None


def _union(block):
    """Bounding box around several lines; None for listing text (no boxes)."""
    boxes = [b["box"] for b in block if b.get("box")]
    if not boxes:
        return None
    x1 = min(b[0] for b in boxes)
    y1 = min(b[1] for b in boxes)
    x2 = max(b[0] + b[2] for b in boxes)
    y2 = max(b[1] + b[3] for b in boxes)
    return [x1, y1, x2 - x1, y2 - y1]


def find_parties(lines):
    """Find manufacturer / packer / marketer / importer blocks with address."""
    found = {}
    for i, ln in enumerate(lines):
        text = _norm(ln["text"])
        for role, rx in ROLE_KW:
            if role in found or not rx.search(text):
                continue
            block = [ln]
            for nxt in lines[i + 1:i + 4]:  # address usually runs 1-3 lines below
                if any(r.search(nxt["text"]) for _, r in ROLE_KW) or CARE_KW.search(nxt["text"]):
                    break
                block.append(nxt)
            joined = " ".join(b["text"] for b in block)
            found[role] = {
                "value": joined,
                "evidence": joined,
                "box": _union(block),
                "has_pin_code": bool(PIN_RE.search(joined)),
                "name_after_keyword": len(rx.sub("", joined).strip(" :.-")) > 3,
                "min_line_height": min((b["box"][3] for b in block if b["box"]), default=None),
            }
    return found


def find_consumer_care(lines, full_text):
    for i, ln in enumerate(lines):
        if not CARE_KW.search(ln["text"]):
            continue
        block = lines[i:i + 4]
        joined = " ".join(b["text"] for b in block)
        return {
            "value": joined, "evidence": joined,
            "box": _union(block),
            "phone": (PHONE_RE.search(joined) or PHONE_RE.search(full_text) or [None])[0],
            "email": (EMAIL_RE.search(joined) or [None])[0],
            "has_address": bool(PIN_RE.search(joined)),
            "min_line_height": min((b["box"][3] for b in block if b["box"]), default=None),
        }
    phone = PHONE_RE.search(full_text)
    email = EMAIL_RE.search(full_text)
    if phone or email:
        return {"value": None, "evidence": None, "box": None,
                "phone": phone[0] if phone else None, "email": email[0] if email else None,
                "has_address": False, "note": "Contact found but no consumer-care heading"}
    return None


def find_origin(lines):
    for ln in lines:
        m = ORIGIN_RE.search(_norm(ln["text"]))
        if m:
            return _field((m.group(1) or m.group(2) or "").strip() or None, ln)
    return None


def script_profile(text):
    latin = sum(1 for c in text if "a" <= c.lower() <= "z")
    deva = sum(1 for c in text if "ऀ" <= c <= "ॿ")
    other = sum(1 for c in text if c.isalpha() and not ("a" <= c.lower() <= "z")
                and not ("ऀ" <= c <= "ॿ"))
    return {"latin": latin, "devanagari": deva, "other": other}


def generic_name_present(name, full_text):
    if not name:
        return None
    hay = re.sub(r"\s+", " ", _norm(full_text).lower())
    tokens = [t for t in re.split(r"\W+", name.lower()) if len(t) > 2]
    if not tokens:
        return None
    hits = sum(1 for t in tokens if t in hay)
    return hits / len(tokens) >= 0.5


# ---------------- amendments (2017-2023) and FSSAI fields ----------------

TAX_INCL_RE = re.compile(r"incl\w*\.?\s*(of\s*)?(all\s*)?tax|inclusive\s+of\s+all\s+taxes|"
                         r"सभी\s*करों?\s*सहित|सभी\s*कर\s*शामिल", re.I)
USP_KW = re.compile(r"unit\s*sale\s*price|\busp\b|इकाई\s*विक्रय\s*मूल्य", re.I)
USP_UNITS = {"g": "g", "gm": "g", "gram": "g", "kg": "kg", "ml": "ml", "l": "l", "ltr": "l", "litre": "l",
             "liter": "l", "cm": "cm", "m": "m", "unit": "unit", "pc": "unit", "pcs": "unit", "piece": "unit",
             "n": "unit", "no": "unit", "nos": "unit", "number": "unit"}
USP_RE = re.compile(r"(?:₹|rs\.?|inr|रु\.?)\s*(\d+(?:[.,]\d+)?)\s*(?:/|per)\s*(?:1\s*)?"
                    r"(kg|gm|gram|g|ml|ltr|litre|liter|l|cm|m|unit|pcs|pc|piece|nos|no|number|n)\b\.?", re.I)
EXPIRY_KW = re.compile(r"best\s*before|use\s*by|expiry|expires?|\bexp\b\.?|बेहतर\s*उपयोग|उपयोग\s*की\s*अंतिम", re.I)
FSSAI_WORD = re.compile(r"\bfssai\b|\bf\s*s\s*s\s*a\s*i\b", re.I)
FSSAI_LIC = re.compile(r"(?<!\d)(\d(?:\s?\d){13})(?!\d)")  # 14 digits (FoSCoS); first digit not checked
INGREDIENTS_KW = re.compile(r"\bingredients?\b|सामग्री|अवयव", re.I)
NUTRIENTS = {
    "energy": re.compile(r"\benergy\b|\bkcal\b|ऊर्जा", re.I),
    "protein": re.compile(r"\bprotein", re.I),
    "carbohydrate": re.compile(r"carbohydrate", re.I),
    "total sugars": re.compile(r"\bsugars?\b", re.I),
    "added sugars": re.compile(r"added\s*sugars?", re.I),
    "total fat": re.compile(r"\b(total\s*)?fat\b", re.I),
    "saturated fat": re.compile(r"saturated", re.I),
    "trans fat": re.compile(r"\btrans\b", re.I),
    "sodium": re.compile(r"\bsodium\b", re.I),
}
NUTRITION_KW = re.compile(r"nutrition(al)?\s*(information|facts|value)|पोषण", re.I)
PER100_RE = re.compile(r"per\s*100\s*(g|gm|ml)|/\s*100\s*(g|ml)|100\s*(g|ml)\s*(serving)?", re.I)
RDA_RE = re.compile(r"%\s*rda|rda\s*%|\brda\b", re.I)
ALLERGENS = {"gluten / cereals": r"\b(wheat|gluten|barley|rye|oats?|spelt)\b", "milk": r"\b(milk|dairy|whey|casein|butter|cheese|ghee)\b",  # noqa: E501
             "egg": r"\beggs?\b", "fish": r"\bfish\b", "crustacean": r"\b(shrimp|prawn|crab|lobster|crustacean)",
             "peanut": r"\b(peanuts?|groundnuts?)\b", "tree nuts": r"\b(almond|cashew|walnut|pistachio|hazelnut)s?\b",
             "soy": r"\b(soy|soya|soybean)\b", "sulphite": r"\bsulph?ites?\b|\bINS\s*22[0-8]\b"}
CONTAINS_RE = re.compile(r"\b(allergen|contains?\s*[:\-]|may\s*contains?)", re.I)
BATCH_RE = re.compile(r"\b(batch|lot)\s*(no\.?|number|code)?\s*[:\-.]?\s*([A-Z0-9][A-Z0-9\-/]{1,})|\bB\.?\s*No\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]+)", re.I)  # noqa: E501
EXAGGERATE_RE = re.compile(r"\b(jumbo|giant|mega|bumper|king\s*size|super\s*size|huge|big)\b", re.I)


def _find_line(lines, rx):
    for ln in lines:
        if rx.search(_norm(ln["text"])):
            return ln
    return None


def find_tax_inclusive(lines):
    ln = _find_line(lines, TAX_INCL_RE)
    return _field(True, ln) if ln else None


def find_usp(lines):
    for ln in lines:
        t = _norm(ln["text"])
        m = USP_RE.search(t)
        if m:
            return _field(m.group(0), ln, {"amount": _to_float(m.group(1)),
                                           "per": USP_UNITS.get(m.group(2).lower(), m.group(2).lower()),
                                           "keyword": bool(USP_KW.search(t))})
    ln = _find_line(lines, USP_KW)
    if ln:
        return _field(None, ln, {"amount": None, "per": None, "keyword": True,
                                 "note": "'Unit sale price' wording found but no 'Rs x per unit' read"})
    return None


def find_expiry(lines):
    for i, ln in enumerate(lines):
        t = _norm(ln["text"])
        m = EXPIRY_KW.search(t)
        if not m:
            continue
        kind = ("use by" if re.search(r"use\s*by|expir|\bexp\b", m.group(0), re.I) else "best before")
        # the date AFTER the keyword (a line can hold 'Mfg. Date: 08/2026  Use By: 05/2027')
        for src, txt in ((ln, t[m.end():]), (lines[i + 1] if i + 1 < len(lines) else None, None)):
            if src is None:
                continue
            txt = txt if txt is not None else _norm(src["text"])
            dm = DATE_RE.search(txt) or re.search(r"\b\d{1,2}\s*months?\b", txt, re.I)
            if dm:
                return _field(dm.group(0), src, {"kind": kind, "wording": m.group(0)})
        return _field(None, ln, {"kind": kind, "wording": m.group(0)})
    return None


def find_fssai(lines, text):
    word = _find_line(lines, FSSAI_WORD)
    lic, lic_line = None, None
    for ln in lines:
        t = ln["text"]
        m = FSSAI_LIC.search(t)
        if m and (FSSAI_WORD.search(t) or re.search(r"lic|licen[cs]e|reg", t, re.I) or word):
            lic, lic_line = re.sub(r"\s", "", m.group(1)), ln
            break
    if not word and not lic:
        return None
    ln = lic_line or word
    return _field(lic, ln, {"licence": lic, "word_found": bool(word),
                            "word_box": word["box"] if word else None})


def find_ingredients(lines):
    for i, ln in enumerate(lines):
        if INGREDIENTS_KW.search(_norm(ln["text"])):
            block = [ln] + lines[i + 1:i + 6]
            txt = " ".join(b["text"] for b in block)
            return _field(txt[:600], ln, {"water_word": bool(re.search(r"\bwater\b|जल|पानी", txt, re.I))})
    return None


def find_nutrition(lines, text):
    head = _find_line(lines, NUTRITION_KW)
    found = [k for k, rx in NUTRIENTS.items() if rx.search(text)]
    if not head and len(found) < 3:
        return None
    return _field(", ".join(found), head, {"nutrients": found, "heading": bool(head),
                                            "per_100": bool(PER100_RE.search(text)),
                                            "rda": bool(RDA_RE.search(text))})


def find_allergens(text, ingredients):
    src = (ingredients or {}).get("value") or ""
    present = [k for k, rx in ALLERGENS.items() if re.search(rx, src, re.I)]
    stmt = CONTAINS_RE.search(text)
    return {"in_ingredients": present, "statement": bool(stmt)}


def find_batch(lines):
    for ln in lines:
        m = BATCH_RE.search(ln["text"])
        if m:
            return _field(m.group(0), ln)
    return None


def find_quantity_qualifier(nq):
    """Rule 12(6): words that exaggerate the quantity next to the declaration."""
    if not nq or not nq.get("evidence"):
        return None
    m = EXAGGERATE_RE.search(nq["evidence"])
    return {"word": m.group(0), "box": nq.get("box")} if m else None


def _amended_fields(lines, text, nq):
    ing = find_ingredients(lines)
    return {
        "tax_inclusive": find_tax_inclusive(lines),
        "usp": find_usp(lines),
        "expiry": find_expiry(lines),
        "fssai": find_fssai(lines, text),
        "ingredients": ing,
        "nutrition": find_nutrition(lines, text),
        "allergens": find_allergens(text, ing),
        "batch": find_batch(lines),
        "quantity_qualifier": find_quantity_qualifier(nq),
    }


def extract_from_ocr(ocr):
    lines = ocr["lines"]
    text = ocr["text"]
    nq = find_net_quantity(lines)
    return {
        **_amended_fields(lines, text, nq),
        "net_quantity": nq,
        "mrp": find_mrp(lines),
        "date": find_date(lines),
        "parties": find_parties(lines),
        "consumer_care": find_consumer_care(lines, text),
        "origin": find_origin(lines),
        "scripts": script_profile(text),
    }


def extract_from_text(text):
    """Listing text (no boxes): treat each non-empty line as a 'line'."""
    lines = [{"text": t.strip(), "box": None, "words": []}
             for t in (text or "").splitlines() if t.strip()]
    full = "\n".join(ln_["text"] for ln_ in lines)
    nq = find_net_quantity(lines)
    return {
        "text": full,
        **_amended_fields(lines, full, nq),
        "net_quantity": nq,
        "mrp": find_mrp(lines),
        "date": find_date(lines),
        "parties": find_parties(lines),
        "consumer_care": find_consumer_care(lines, full),
        "origin": find_origin(lines),
        "scripts": script_profile(full),
    }
