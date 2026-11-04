"""Reports: annotated image, PDF report and editable Word (.docx) report.

SIH26034 asks for compliance reports in PDF and editable formats.
"""
import io
from xml.sax.saxutils import escape

import cv2
from docx import Document
from docx.shared import Mm, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from . import rules_data as R

STATUS_BGR = {"fail": (40, 40, 220), "warn": (0, 150, 240), "pass": (60, 160, 60),
              "manual": (160, 120, 60), "na": (140, 140, 140)}
STATUS_HEX = {"fail": "#c62828", "warn": "#e08a00", "pass": "#2e7d32",
              "manual": "#3d6fa0", "na": "#808080"}
STATUS_TEXT = {"fail": "FAIL", "warn": "CHECK", "pass": "PASS", "manual": "MANUAL", "na": "N/A"}
VERDICT_TEXT = {
    "issues_found": "Issues found",
    "needs_review": "No automated failures; manual checks pending",
    "no_issues_found": "No issues found in automated checks",
}
REVIEW_TEXT = {"not_submitted": "Not submitted for review", "in_review": "In review",
               "approved": "Approved", "changes_requested": "Changes requested"}
DECISION_TEXT = {"ok": "OK (checked)", "issue": "Confirmed issue"}
DISCLAIMER = ("This report is produced by an assistive tool. It is not legal advice and does "
              "not certify compliance. A qualified person must review it. " + R.RULESET_NOTE)


def _e(v):
    return escape(str(v if v is not None else "-"))


def _review_line(scan):
    line = f"Review status: {REVIEW_TEXT.get(scan.get('review_status'), '-')}"
    if scan.get("review_status") in ("approved", "changes_requested") and scan.get("reviewed_by_name"):
        line += f" by {scan['reviewed_by_name']} on {scan.get('reviewed_at')} UTC"
    return line


def _decision_text(scan, f):
    d = (scan.get("decisions") or {}).get(f.get("key"))
    if not d:
        return ""
    t = f"{DECISION_TEXT[d['decision']]} - {d['user_name']}"
    return t + (f": {d['comment']}" if d.get("comment") else "")


def annotated_png(image_key, findings, max_side=1400):
    import numpy as np
    from .storage import store
    try:
        img = cv2.imdecode(np.frombuffer(store().get(image_key), np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None
    if img is None:
        return None
    thick = max(2, int(max(img.shape[:2]) / 400))
    for i, f in enumerate(findings, 1):
        if not f.get("box"):
            continue
        x, y, w, h = f["box"]
        col = STATUS_BGR[f["status"]]
        cv2.rectangle(img, (x, y), (x + w, y + h), col, thick)
        cv2.putText(img, str(i), (x, max(12, y - 4)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5 * thick, col, thick)
    scale = min(1.0, max_side / max(img.shape[:2]))
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes() if ok else None


def _field_rows(extracted):
    def v(x, key="value"):
        return (x or {}).get(key) or "not read"
    parties = extracted.get("parties") or {}
    rows = [["Net quantity", v(extracted.get("net_quantity"))],
            ["MRP", v(extracted.get("mrp"))],
            ["Month/year", v(extracted.get("date"))]]
    for role, p in parties.items():
        rows.append([role.capitalize(), p["value"][:120]])
    cc = extracted.get("consumer_care") or {}
    rows.append(["Consumer care", ", ".join(x for x in (cc.get("phone"), cc.get("email")) if x)
                 or "not read"])
    return rows


def pdf_report(scan):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Label compliance report #{scan['id']}")
    ss = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8.5, leading=11)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=8.5, leading=10.5)
    res = scan["result"]
    story = [
        Paragraph(f"Label compliance report #{scan['id']}", ss["Title"]),
        Paragraph(f"<b>Product:</b> {_e(scan['product'])} &nbsp; <b>SKU:</b> {_e(scan.get('sku'))} "
                  f"&nbsp; <b>Version:</b> {_e(scan.get('version'))} &nbsp; <b>Category:</b> "
                  f"{_e(scan['category'])} &nbsp; <b>File:</b> {_e(scan['filename'])} &nbsp; "
                  f"<b>Scanned:</b> {_e(scan['created_at'])} UTC", small),
        Paragraph(f"<b>Result:</b> {VERDICT_TEXT[res['summary']['verdict']]} &nbsp; "
                  f"<b>Rule set:</b> {_e(res['ruleset']['version'])} &nbsp; <b>{_e(_review_line(scan))}</b>",
                  small),
    ]
    for n in scan.get("prep") or []:
        story.append(Paragraph(f"Image preparation: {_e(n)}", small))
    story.append(Spacer(1, 4 * mm))
    png = annotated_png(scan["image_path"], res["findings"])
    if png:
        img = RLImage(io.BytesIO(png))
        ratio = img.imageHeight / img.imageWidth
        img.drawWidth = 120 * mm
        img.drawHeight = min(110 * mm, 120 * mm * ratio)
        img.drawWidth = img.drawHeight / ratio
        story += [img, Spacer(1, 4 * mm)]

    story.append(Paragraph("Declarations read", ss["Heading3"]))
    t = Table([[Paragraph(_e(a), cell), Paragraph(_e(b), cell)] for a, b in _field_rows(scan["extracted"])],
              colWidths=[35 * mm, 140 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t, Spacer(1, 4 * mm), Paragraph("Findings", ss["Heading3"])]

    rows = [[Paragraph(f"<b>{h}</b>", cell) for h in ("#", "Status", "Rule", "Finding", "Basis", "Reviewer")]]
    for i, f in enumerate(res["findings"], 1):
        basis = R.SOURCE_LABELS.get(f["source"], f["source"])
        if f["method"] in ("approximate", "heuristic"):
            basis += f", {f['method']}"
        rows.append([Paragraph(str(i), cell),
                     Paragraph(f"<font color='{STATUS_HEX[f['status']]}'><b>{STATUS_TEXT[f['status']]}</b></font>", cell),  # noqa: E501
                     Paragraph(f"{_e(f['rule'])}<br/>{_e(f['title'])}", cell),
                     Paragraph(_e(f["detail"]), cell), Paragraph(_e(basis), cell),
                     Paragraph(_e(_decision_text(scan, f)), cell)])
    ft = Table(rows, colWidths=[7 * mm, 15 * mm, 34 * mm, 64 * mm, 25 * mm, 33 * mm], repeatRows=1)
    ft.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [ft, Spacer(1, 5 * mm)]
    if scan.get("comments"):
        story.append(Paragraph("Review comments", ss["Heading3"]))
        for c in scan["comments"]:
            story.append(Paragraph(f"<b>{_e(c['user_name'])}</b> ({_e(c['created_at'])} UTC): {_e(c['text'])}", small))
        story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(_e(DISCLAIMER), small))
    doc.build(story)
    return buf.getvalue()


def docx_report(scan):
    res = scan["result"]
    d = Document()
    for s in d.sections:
        s.left_margin = s.right_margin = Mm(16)
    d.add_heading(f"Label compliance report #{scan['id']}", 0)
    p = d.add_paragraph()
    p.add_run(f"Product: {scan['product'] or '-'}    SKU: {scan.get('sku') or '-'}    "
              f"Version: {scan.get('version') or '-'}    Category: {scan['category']}    "
              f"File: {scan['filename']}    Scanned: {scan['created_at']} UTC\n")
    p.add_run(f"Result: {VERDICT_TEXT[res['summary']['verdict']]}    "
              f"Rule set: {res['ruleset']['version']}    {_review_line(scan)}").bold = True
    for n in scan.get("prep") or []:
        d.add_paragraph(f"Image preparation: {n}")

    png = annotated_png(scan["image_path"], res["findings"])
    if png:
        d.add_picture(io.BytesIO(png), width=Mm(120))

    d.add_heading("Declarations read", 2)
    t = d.add_table(rows=0, cols=2)
    t.style = "Table Grid"
    for a, b in _field_rows(scan["extracted"]):
        r = t.add_row().cells
        r[0].text, r[1].text = a, str(b)

    d.add_heading("Findings", 2)
    t = d.add_table(rows=1, cols=5)
    t.style = "Table Grid"
    for c, h in zip(t.rows[0].cells, ("#", "Status", "Rule", "Finding", "Reviewer note"), strict=False):
        c.text = h
    for i, f in enumerate(res["findings"], 1):
        r = t.add_row().cells
        r[0].text = str(i)
        run = r[1].paragraphs[0].add_run(STATUS_TEXT[f["status"]])
        run.bold = True
        run.font.color.rgb = RGBColor.from_string(STATUS_HEX[f["status"]][1:])
        r[2].text = f"{f['rule']} {f['title']}"
        r[3].text = f["detail"]
        r[4].text = _decision_text(scan, f)  # editable column for the reviewer
    for row in t.rows:
        for c in row.cells:
            for par in c.paragraphs:
                for run in par.runs:
                    run.font.size = Pt(8.5)
    if scan.get("comments"):
        d.add_heading("Review comments", 2)
        for c in scan["comments"]:
            d.add_paragraph(f"{c['user_name']} ({c['created_at']} UTC): {c['text']}")
    d.add_paragraph().add_run(DISCLAIMER).italic = True
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
