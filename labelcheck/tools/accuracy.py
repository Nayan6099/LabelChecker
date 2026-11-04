"""Measure LabelCheck accuracy on real labels.

1. Put label photos / PDFs in a folder.
2. Make the answer sheet:   python -m tools.accuracy template <folder>
   -> <folder>/ground_truth.csv with one row per file. Fill in what is REALLY on each label
      (read it with your own eyes). Leave a cell empty to skip that check.
3. Run:                      python -m tools.accuracy run <folder> [--out report-dir]
   -> report.html + results.csv: field accuracy, rule precision / recall, false FAILs, timing.

Ground-truth columns
  file, product_name, category, panel_width_mm, panel_height_mm, embossed, imported, auto_straighten
  net_quantity   e.g. "500 g", "1 L", "10 N"          (compared by value and unit)
  mrp            e.g. 120 or 120.00                    (compared as a number)
  mfg_date       e.g. 08/2026                          (month/year)
  usp            e.g. "0.24 per g"                     (amount and unit)
  phone          digits, e.g. 18001234567              (last 10 digits compared)
  email          e.g. care@brand.in
  pin            e.g. 201001                           (PIN code of the maker/packer address)
  fssai_licence  14 digits (food only)
  food_symbol    veg / non-veg / none (food only)
  expected_fail  rules that SHOULD fail, separated by ";", e.g. "6(1)(e);7(2)"   ("-" = none)

Precision = of the FAILs the app reported, how many were real. Recall = of the real problems, how many
the app caught. For a compliance tool, recall matters most (a missed problem reaches the market), but
low precision wastes reviewers' time.
"""
import argparse
import csv
import html
import re
import sys
import time
from pathlib import Path

from app import engine, extract, inputs, ocr

FIELDS = ["net_quantity", "mrp", "mfg_date", "usp", "phone", "email", "pin", "fssai_licence", "food_symbol"]
COLUMNS = ["file", "product_name", "category", "panel_width_mm", "panel_height_mm", "embossed", "imported",
           "auto_straighten"] + FIELDS + ["expected_fail"]
EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".pdf"}


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _qty(text):
    q = extract.extract_from_text("Net " + text)["net_quantity"] if text else None
    return (q["unit_kind"], q["base_value"]) if q and q.get("base_value") is not None else None


def _month_year(text):
    m = re.search(r"(\d{1,2})\s*[/\-.]\s*(\d{2,4})", text or "")
    if not m:
        return (text or "").strip().lower() or None
    y = m.group(2)
    return f"{int(m.group(1)):02d}/{'20' + y if len(y) == 2 else y}"


def _digits(s, last=None):
    d = re.sub(r"\D", "", s or "")
    return d[-last:] if last and d else (d or None)


def got_values(ext, img):
    """What the app read, in the same form as the answer sheet."""
    nq, mrp, usp, cc = ext.get("net_quantity"), ext.get("mrp"), ext.get("usp"), ext.get("consumer_care") or {}
    parties = ext.get("parties") or {}
    main = parties.get("manufacturer") or parties.get("packer") or parties.get("importer") or parties.get("marketer")
    pin = None
    if main:
        m = extract.PIN_RE.search(main["value"] or "")
        pin = re.sub(r"\s", "", m.group(0)) if m else None
    from app.symbols import find_food_symbol
    sym = find_food_symbol(img)
    return {
        "net_quantity": (nq["unit_kind"], nq["base_value"]) if nq and nq.get("base_value") is not None else None,
        "mrp": mrp.get("amount") if mrp else None,
        "mfg_date": _month_year((ext.get("date") or {}).get("value")),
        "usp": (usp["amount"], usp["per"]) if usp and usp.get("amount") is not None else None,
        "phone": _digits(cc.get("phone"), 10),
        "email": (cc.get("email") or "").lower() or None,
        "pin": pin,
        "fssai_licence": (ext.get("fssai") or {}).get("licence"),
        "food_symbol": sym["type"] if sym else "none",
    }


def want_values(row):
    w = {}
    if row.get("net_quantity"):
        w["net_quantity"] = _qty(row["net_quantity"])
    if row.get("mrp"):
        w["mrp"] = _num(row["mrp"])
    if row.get("mfg_date"):
        w["mfg_date"] = _month_year(row["mfg_date"])
    if row.get("usp"):
        m = re.match(r"\s*(?:rs\.?\s*)?([\d.]+)\s*(?:/|per)\s*(\w+)", row["usp"], re.I)
        w["usp"] = (float(m.group(1)), extract.USP_UNITS.get(m.group(2).lower(), m.group(2).lower())) if m else row["usp"]
    if row.get("phone"):
        w["phone"] = _digits(row["phone"], 10)
    if row.get("email"):
        w["email"] = row["email"].strip().lower()
    if row.get("pin"):
        w["pin"] = _digits(row["pin"])
    if row.get("fssai_licence"):
        w["fssai_licence"] = _digits(row["fssai_licence"])
    if row.get("food_symbol"):
        w["food_symbol"] = row["food_symbol"].strip().lower()
    return w


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(float(a) - float(b)) < 0.011
    if isinstance(a, tuple) and isinstance(b, tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=False))
    return a == b


def check_one(path, row):
    opts = {"product_name": row.get("product_name") or "", "category": row.get("category") or "general",
            "panel_width_mm": _num(row.get("panel_width_mm") or ""), "panel_height_mm": _num(row.get("panel_height_mm") or ""),
            "embossed": _bool(row.get("embossed")), "imported": _bool(row.get("imported")),
            "auto_straighten": _bool(row.get("auto_straighten"))}
    t0 = time.time()
    img, updates, notes, _ = inputs.prepare(path.read_bytes(), opts)
    opts.update(updates)
    o = ocr.read_label(img)
    ext = extract.extract_from_ocr(o)
    res = engine.run(o, ext, img, opts)
    secs = time.time() - t0
    got = got_values(ext, img)
    want = want_values(row)
    fields = {k: {"want": want[k], "got": got[k], "ok": _same(want[k], got[k])} for k in want}
    app_fail = {f["rule"] for f in res["findings"] if f["status"] == "fail"}
    exp = row.get("expected_fail", "").strip()
    expected = None if exp == "" else ({r.strip() for r in exp.split(";") if r.strip() and r.strip() != "-"})
    return {"file": path.name, "secs": round(secs, 2), "fields": fields, "app_fail": app_fail,
            "expected_fail": expected, "verdict": res["summary"]["verdict"]}


def template(folder):
    folder = Path(folder)
    files = sorted(p.name for p in folder.iterdir() if p.suffix.lower() in EXTS)
    out = folder / "ground_truth.csv"
    if out.exists():
        sys.exit(f"{out} already exists; not overwriting.")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for name in files:
            w.writerow({"file": name, "category": "general", "auto_straighten": "yes"})
    print(f"Wrote {out} with {len(files)} rows. Fill it in, then run: python -m tools.accuracy run {folder}")


def run(folder, out_dir=None):
    folder = Path(folder)
    rows = list(csv.DictReader(open(folder / "ground_truth.csv", encoding="utf-8-sig")))
    out_dir = Path(out_dir or folder / "accuracy-report")
    out_dir.mkdir(parents=True, exist_ok=True)
    results, errors = [], []
    for row in rows:
        p = folder / row["file"]
        if not p.exists():
            errors.append((row["file"], "file not found"))
            continue
        try:
            results.append(check_one(p, row))
            print(f"  {row['file']}: {results[-1]['secs']} s")
        except Exception as e:  # unreadable file etc.
            errors.append((row["file"], str(e)))

    field_stats = {k: [0, 0] for k in FIELDS}
    tp = fp = fn = 0
    false_fails, missed = [], []
    for r in results:
        for k, v in r["fields"].items():
            field_stats[k][1] += 1
            field_stats[k][0] += v["ok"]
        if r["expected_fail"] is not None:
            tp += len(r["app_fail"] & r["expected_fail"])
            for rule in sorted(r["app_fail"] - r["expected_fail"]):
                fp += 1
                false_fails.append((r["file"], rule))
            for rule in sorted(r["expected_fail"] - r["app_fail"]):
                fn += 1
                missed.append((r["file"], rule))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    avg = sum(r["secs"] for r in results) / len(results) if results else 0

    with open(out_dir / "results.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "field", "expected", "app_read", "correct"])
        for r in results:
            for k, v in r["fields"].items():
                w.writerow([r["file"], k, v["want"], v["got"], v["ok"]])

    pct = lambda a, b: f"{100 * a / b:.0f}% ({a}/{b})" if b else "-"  # noqa: E731
    fmt = lambda x: "-" if x is None else f"{100 * x:.0f}%"  # noqa: E731
    e = html.escape
    rows_html = "".join(f"<tr><td>{k}</td><td>{pct(*field_stats[k])}</td></tr>" for k in FIELDS if field_stats[k][1])
    detail = "".join(
        f"<tr><td>{e(r['file'])}</td><td>{e(k)}</td><td>{e(str(v['want']))}</td><td>{e(str(v['got']))}</td>"
        f"<td class={'ok' if v['ok'] else 'bad'}>{'yes' if v['ok'] else 'NO'}</td></tr>"
        for r in results for k, v in r["fields"].items())
    page = f"""<!doctype html><meta charset="utf-8"><title>LabelCheck accuracy report</title>
<style>body{{font:14px system-ui;max-width:1000px;margin:24px auto;padding:0 16px}}table{{border-collapse:collapse;width:100%;margin:8px 0 20px}}
td,th{{border-bottom:1px solid #ddd;padding:5px 8px;text-align:left}}.ok{{color:#2e7d32}}.bad{{color:#c62828;font-weight:600}}</style>
<h1>LabelCheck accuracy report</h1>
<p>{len(results)} labels checked, {len(errors)} could not be processed. Average time {avg:.1f} s per label.</p>
<h2>Rules (FAIL decisions)</h2>
<table><tr><td>Precision (FAILs that were real)</td><td>{fmt(precision)}</td></tr>
<tr><td>Recall (real problems caught)</td><td>{fmt(recall)}</td></tr>
<tr><td>False FAILs</td><td>{e(', '.join(f'{a} {b}' for a, b in false_fails) or 'none')}</td></tr>
<tr><td>Missed problems</td><td>{e(', '.join(f'{a} {b}' for a, b in missed) or 'none')}</td></tr></table>
<h2>Fields read correctly</h2><table>{rows_html}</table>
<h2>Errors</h2><p>{e('; '.join(f'{a}: {b}' for a, b in errors) or 'none')}</p>
<h2>Every field</h2><table><tr><th>File</th><th>Field</th><th>Expected</th><th>App read</th><th>Correct</th></tr>{detail}</table>"""
    (out_dir / "report.html").write_text(page)
    print(f"\nLabels: {len(results)}  errors: {len(errors)}  avg {avg:.1f} s")
    for k in FIELDS:
        if field_stats[k][1]:
            print(f"  {k:14} {pct(*field_stats[k])}")
    print(f"  FAIL precision {fmt(precision)}  recall {fmt(recall)}")
    print(f"Report: {out_dir / 'report.html'}")
    return {"precision": precision, "recall": recall, "fields": field_stats, "errors": errors,
            "false_fails": false_fails, "missed": missed}


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m tools.accuracy")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("template")
    t.add_argument("folder")
    r = sub.add_parser("run")
    r.add_argument("folder")
    r.add_argument("--out")
    a = p.parse_args(argv)
    if a.cmd == "template":
        template(a.folder)
    else:
        run(a.folder, a.out)


if __name__ == "__main__":
    main()
