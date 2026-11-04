"""Bulk upload: many files + optional CSV, processed by the background workers."""
import csv
import io
import os
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from .. import auth, db, reports
from .common import audit, paginate_response
from .scans import check_file, make_listing, make_opts, report_scan, reserve

router = APIRouter(prefix="/api/batches", tags=["batches"])
MAX_BATCH_FILES = 200
MANIFEST_COLS = ["file", "product_name", "sku", "category", "panel_width_mm", "panel_height_mm",
                 "embossed", "imported", "auto_straighten", "pdf_page", "listing_text"]


def _read_manifest(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "Manifest must be a UTF-8 CSV file.") from None
    rows = list(csv.DictReader(io.StringIO(text)))
    if rows and "file" not in rows[0]:
        raise HTTPException(400, "Manifest needs a 'file' column (see the template).")
    return {(r.get("file") or "").strip(): r for r in rows if (r.get("file") or "").strip()}


@router.get("/template.csv")
def manifest_template():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(MANIFEST_COLS)
    w.writerow(["peanuts_500g.jpg", "Roasted Peanuts", "SKU-001", "general", "100", "75", "no", "no",
                "yes", "1", ""])
    w.writerow(["oil_1l_artwork.pdf", "Mustard Oil", "SKU-002", "general", "", "", "no", "no", "no", "1",
                "Mustard Oil 1 Litre MRP Rs. 180"])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="labelcheck-batch-template.csv"'})


@router.post("")
async def create_batch(
    request: Request,
    files: List[UploadFile] = File(...),
    manifest: Optional[UploadFile] = File(None),
    name: str = Form(""), category: str = Form("general"),
    panel_width_mm: Optional[str] = Form(None), panel_height_mm: Optional[str] = Form(None),
    imported: str = Form("false"), auto_straighten: str = Form("false"),
    user=Depends(auth.require("uploader")),
):
    if not files:
        raise HTTPException(400, "Add at least one file.")
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(400, f"Too many files: {len(files)} (limit {MAX_BATCH_FILES} per batch).")
    rows = _read_manifest(await manifest.read()) if manifest is not None and manifest.filename else {}
    loaded, errors = [], []
    for f in files:
        data = await f.read()
        fname = Path(f.filename or "upload").name[:255]
        try:
            kind = check_file(data, fname)
            r = rows.get(fname, {})
            # No name given: leave it empty (6(1)(b) becomes a manual check) rather than
            # using the file name, which would make every label fail the generic-name rule.
            opts = make_opts(r.get("product_name") or "", r.get("sku"),
                             (r.get("category") or category).strip() or "general",
                             r.get("panel_width_mm") or panel_width_mm, r.get("panel_height_mm") or panel_height_mm,
                             r.get("embossed") or "false", r.get("imported") or imported,
                             r.get("auto_straighten") or auto_straighten, r.get("pdf_page") or "1")
        except HTTPException as e:
            errors.append({"file": fname, "error": e.detail})
            continue
        loaded.append((fname, kind, data, opts, make_listing(r.get("listing_text"), r.get("listing_url"))))
    if not loaded:
        raise HTTPException(400, {"message": "No valid files in the batch.", "errors": errors})
    reserve(user, len(loaded))
    missing = sorted(set(rows) - {x[0] for x in loaded} - {e["file"] for e in errors})
    bid = db.create_batch(user["org_id"], user["id"], name.strip()[:200] or None, len(loaded))
    ids = [db.create_scan(user["org_id"], user["id"], opts, fname, "pdf" if kind == "pdf" else "image",
                          data, ".pdf" if kind == "pdf" else f".{kind}", batch_id=bid, listing=lst,
                          api_key_id=user.get("api_key_id"))
           for fname, kind, data, opts, lst in loaded]
    audit(request, user, "batch_uploaded", "batch", bid, {"files": len(ids), "rejected": len(errors)})
    return {"id": bid, "scan_ids": ids, "rejected": errors, "manifest_rows_without_file": missing}


@router.get("")
def batches(page: Optional[int] = None, limit: int = 10, paginated: bool = False,
            user=Depends(auth.current_user)):
    if paginated or page is not None:
        p = max(1, page or 1)
        lim = max(1, min(limit, 100))
        offset = (p - 1) * lim
        items = db.list_batches(user["org_id"], limit=lim, offset=offset)
        total = db.count_batches(user["org_id"])
        return paginate_response(items, total, p, lim)
    return db.list_batches(user["org_id"])


@router.get("/{bid}")
def batch(bid: int, user=Depends(auth.current_user)):
    b = db.get_batch(bid, user["org_id"])
    if not b:
        raise HTTPException(404, "Batch not found.")
    b["scans"] = db.list_scans(user["org_id"], batch_id=bid, limit=MAX_BATCH_FILES)
    return b


def _summary_csv(scans):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["scan_id", "file", "product", "sku", "version", "status", "verdict", "fail", "check",
                "manual", "pass", "review_status", "failed_rules", "error"])
    for s in scans:
        full = db.get_scan(s["id"])
        failed = ""
        if full["result"]:
            failed = "; ".join(sorted({f["rule"] for f in full["result"]["findings"] if f["status"] == "fail"}))
        w.writerow([s["id"], s["filename"], s["product"], s["sku"] or "", s["version"], s["status"],
                    s["verdict"] or "", s["fails"], s["warns"], s["manuals"], s["passes"],
                    s["review_status"], failed, s["error"] or ""])
    return buf.getvalue()


@router.get("/{bid}/summary.csv")
def batch_csv(bid: int, user=Depends(auth.current_user)):
    if not db.get_batch(bid, user["org_id"]):
        raise HTTPException(404, "Batch not found.")
    return Response(_summary_csv(db.list_scans(user["org_id"], batch_id=bid, limit=MAX_BATCH_FILES)),
                    media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="batch-{bid}-summary.csv"'})


@router.get("/{bid}/reports.zip")
def batch_zip(bid: int, user=Depends(auth.current_user)):
    if not db.get_batch(bid, user["org_id"]):
        raise HTTPException(404, "Batch not found.")
    scans = db.list_scans(user["org_id"], batch_id=bid, limit=MAX_BATCH_FILES)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.csv", _summary_csv(scans))
        for s in scans:
            if s["status"] == "done":
                stem = Path(s["filename"]).stem
                z.writestr(f"reports/{s['id']}-{stem}.pdf", reports.pdf_report(report_scan(db.get_scan(s["id"]))))
    tmp.close()
    return FileResponse(tmp.name, media_type="application/zip", filename=f"batch-{bid}-reports.zip",
                        background=BackgroundTask(os.unlink, tmp.name))
