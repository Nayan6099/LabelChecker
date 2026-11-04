"""Scans: upload, results, files, reports, compare, delete, and the review workflow."""
import math
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import auth, db, engine, inputs, mailer, pipeline, reports, webhooks
from .. import rules_data as R
from ..config import settings
from ..storage import store
from .common import audit, flag, num, paginate_response, scan_or_404

router = APIRouter(prefix="/api", tags=["scans"])
MAX_FILE = 25 * 1024 * 1024
MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "bmp": "image/bmp",
        "tif": "image/tiff", "pdf": "application/pdf"}


def make_opts(product_name, sku, category, pw, ph, embossed, imported, auto_straighten, pdf_page):
    if category not in R.CATEGORIES:
        raise HTTPException(400, f"Unknown category '{category}'.")
    return {"product_name": (product_name or "").strip()[:200], "sku": (sku or "").strip()[:100] or None,
            "category": category, "panel_width_mm": num(pw), "panel_height_mm": num(ph),
            "embossed": flag(embossed), "imported": flag(imported),
            "auto_straighten": flag(auto_straighten), "pdf_page": int(num(pdf_page) or 1)}


def make_listing(text, url=None):
    text = (text or "").strip()[:60000]
    if not text:
        return None
    return {"text": text}


def check_file(data, name):
    if not data:
        raise HTTPException(400, f"{name}: empty file.")
    if len(data) > MAX_FILE:
        raise HTTPException(413, f"{name}: larger than {MAX_FILE // 1024 // 1024} MB.")
    kind = inputs.sniff(data)
    if kind is None:
        raise HTTPException(400, f"{name}: unsupported file. Upload JPG, PNG, WEBP, BMP, TIFF or PDF.")
    return kind


def reserve(user, n):
    """Upload rate limit per company + monthly quota of the plan."""
    auth.rate_limit("upload", user["org_id"], settings.rate_upload_per_min, 60,
                    "Upload limit reached for your company. Wait a minute.")
    ok, used, quota = db.reserve_scans(user["org_id"], n)
    if not ok:
        raise HTTPException(429, f"Monthly scan limit reached ({used} of {quota} used). "
                                 "Ask your administrator to upgrade the plan.")


@router.post("/scans")
async def create_scan(
    request: Request,
    image: UploadFile = File(...),
    product_name: str = Form(""), sku: str = Form(""), category: str = Form("general"),
    panel_width_mm: Optional[str] = Form(None), panel_height_mm: Optional[str] = Form(None),
    embossed: str = Form("false"), imported: str = Form("false"),
    auto_straighten: str = Form("false"), pdf_page: str = Form("1"),
    listing_text: str = Form(""), listing_url: Optional[str] = Form(None),
    wait: str = Form("true"),
    user=Depends(auth.require("uploader")),
):
    """Upload one label. wait=true (default): checked now, result returned (3-6 s).
    wait=false: queued for the workers; poll GET /api/scans/{id} until status is done or error."""
    data = await image.read()
    kind = check_file(data, image.filename or "file")
    opts = make_opts(product_name, sku, category, panel_width_mm, panel_height_mm, embossed, imported,
                     auto_straighten, pdf_page)
    reserve(user, 1)
    now = flag(wait)
    # wait=true: created as 'processing' so background workers never pick it up (no race).
    sid = db.create_scan(user["org_id"], user["id"], opts, (image.filename or "upload")[:255],
                         "pdf" if kind == "pdf" else "image", data, ".pdf" if kind == "pdf" else f".{kind}",
                         listing=make_listing(listing_text, listing_url),
                         status="processing" if now else "queued", api_key_id=user.get("api_key_id"))
    audit(request, user, "scan_uploaded", "scan", sid, {"file": image.filename, "product": opts["product_name"]})
    if now:
        await run_in_threadpool(pipeline.process, sid)
    return scan_payload(db.get_scan(sid), user)


def scan_payload(s, user):
    out = {k: s[k] for k in ("id", "created_at", "product", "sku", "category", "filename", "source_type",
                             "status", "error", "verdict", "version", "product_id", "batch_id",
                             "review_status", "reviewed_at", "options", "listing", "prep", "duration_ms")}
    if s["status"] == "done":
        out.update(result=s["result"], extracted=s["extracted"],
                   image={"width": s["ocr"]["width"], "height": s["ocr"]["height"]},
                   ocr_text=s["ocr"]["text"])
    out["decisions"] = db.finding_decisions(s["id"])
    out["comments"] = db.list_comments(s["id"])
    out["versions"] = db.versions(user["org_id"], s["product_id"]) if s["product_id"] else []
    out["previous_id"] = db.previous_version(s)
    out["reviewed_by"] = (db.user_by_id(s["reviewed_by"]) or {}).get("name") if s["reviewed_by"] else None
    out["uploaded_by"] = (db.user_by_id(s["user_id"]) or {}).get("name")
    out["can"] = permissions(s, user)
    return out


def permissions(s, user):
    r = 3 if user.get("is_platform_admin") else auth.ROLES.get(user.get("role"), 1)
    own = s["user_id"] == user["id"]
    done = s["status"] == "done"
    human = user.get("via") != "api_key"
    return {
        "submit": human and done and s["review_status"] in ("not_submitted", "changes_requested") and (r >= 2 or own),
        "decide": human and done and r >= 2 and s["review_status"] != "approved",
        "approve": human and done and r >= 2 and s["review_status"] == "in_review"
        and not (settings.four_eyes and own),
        "comment": human,
        "delete": human and s["status"] in ("done", "error")
        and (r >= 3 or (own and s["review_status"] != "approved")),
    }


@router.get("/scans")
def list_scans(q: Optional[str] = None, review_status: Optional[str] = None, batch_id: Optional[int] = None,
               status: Optional[str] = None, verdict: Optional[str] = None,
               before_id: Optional[int] = None, limit: int = 100, page: Optional[int] = None,
               paginated: bool = False, user=Depends(auth.current_user)):
    """Newest first. If paginated=true or page is provided, returns {items, total, page, limit, total_pages}."""
    if paginated or page is not None:
        p = max(1, page or 1)
        lim = 10 if (paginated and limit == 100 and page is None) else max(1, min(limit, 500))
        offset = (p - 1) * lim
        items = db.list_scans(user["org_id"], q, review_status, batch_id, lim, before_id, status, verdict, offset=offset)
        total = db.count_scans(user["org_id"], q, review_status, batch_id, status, verdict)
        return paginate_response(items, total, p, lim)
    return db.list_scans(user["org_id"], q, review_status, batch_id, max(1, min(limit, 500)), before_id,
                         status, verdict)


@router.get("/scans/{sid}/findings")
def scan_findings(sid: int, page: int = 1, limit: int = 5, status: Optional[str] = None,
                  user=Depends(auth.current_user)):
    """Paginated list of statutory checks/findings on a scan (5 checks per page)."""
    s = scan_or_404(sid, user)
    findings = []
    if s["status"] == "done" and s.get("result"):
        findings = list((s["result"] or {}).get("findings", []))
    if status and status != "all":
        findings = [f for f in findings if f.get("status") == status]
    total = len(findings)
    p = max(1, page)
    lim = max(1, min(limit, 100))
    start = (p - 1) * lim
    end = start + lim
    items = findings[start:end]
    resp = paginate_response(items, total, p, lim)
    resp["decisions"] = db.finding_decisions(s["id"])
    return resp


@router.get("/scans/{sid}")
def get_scan(sid: int, user=Depends(auth.current_user)):
    return scan_payload(scan_or_404(sid, user), user)


@router.get("/scans/{sid}/image")
def scan_image(sid: int, user=Depends(auth.current_user)):
    s = scan_or_404(sid, user)
    if not s["image_path"]:
        raise HTTPException(404, "Image not ready.")
    try:
        data = store().get(s["image_path"])
    except Exception:
        raise HTTPException(404, "Image not found.") from None
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/scans/{sid}/upload")
def scan_upload(sid: int, user=Depends(auth.current_user)):
    s = scan_or_404(sid, user)
    try:
        data = store().get(s["upload_path"])
    except Exception:
        raise HTTPException(404, "File not found.") from None
    kind = inputs.sniff(data) or "bin"
    safe = "".join(c for c in (s["filename"] or "upload") if c.isalnum() or c in "._- ")[:100] or "upload"
    return Response(data, media_type=MIME.get(kind, "application/octet-stream"),
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


def report_scan(s):
    s = dict(s)
    s["decisions"] = db.finding_decisions(s["id"])
    s["comments"] = db.list_comments(s["id"])
    s["reviewed_by_name"] = (db.user_by_id(s["reviewed_by"]) or {}).get("name") if s["reviewed_by"] else None
    return s


@router.get("/scans/{sid}/report.pdf")
def report_pdf(sid: int, user=Depends(auth.current_user)):
    s = scan_or_404(sid, user)
    if s["status"] != "done":
        raise HTTPException(409, "Scan is not finished.")
    return Response(reports.pdf_report(report_scan(s)), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="labelcheck-{sid}.pdf"'})


@router.get("/scans/{sid}/report.docx")
def report_docx(sid: int, user=Depends(auth.current_user)):
    s = scan_or_404(sid, user)
    if s["status"] != "done":
        raise HTTPException(409, "Scan is not finished.")
    return Response(reports.docx_report(report_scan(s)),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="labelcheck-{sid}.docx"'})


@router.get("/scans/{sid}/compare")
def compare(sid: int, with_id: Optional[int] = None, user=Depends(auth.current_user)):
    new = scan_or_404(sid, user)
    other = with_id or db.previous_version(new)
    if not other:
        raise HTTPException(404, "No earlier version of this product to compare with.")
    old = scan_or_404(other, user)
    if old["status"] != "done" or new["status"] != "done":
        raise HTTPException(409, "Both scans must be finished.")
    if old["version"] > new["version"]:
        old, new = new, old
    return engine.compare(old, new)


@router.get("/scans/{sid}/audit")
def scan_audit(sid: int, page: Optional[int] = None, limit: int = 10, paginated: bool = False,
               user=Depends(auth.current_user)):
    scan_or_404(sid, user)
    if paginated or page is not None:
        p = max(1, page or 1)
        lim = max(1, min(limit, 500))
        offset = (p - 1) * lim
        items = db.audit_list(user["org_id"], "scan", sid, limit=lim, offset=offset)
        total = db.count_audit(user["org_id"], "scan", sid)
        return paginate_response(items, total, p, lim)
    return db.audit_list(user["org_id"], "scan", sid)


@router.delete("/scans/{sid}")
def delete_scan(sid: int, request: Request, user=Depends(auth.current_user)):
    """Admins can delete any finished scan; uploaders their own, unless it is approved."""
    s = scan_or_404(sid, user)
    if not permissions(s, user)["delete"]:
        raise HTTPException(403, "You cannot delete this scan (approved labels: admins only; "
                                 "scans still running cannot be deleted).")
    db.delete_scan(sid)
    audit(request, user, "scan_deleted", "scan", sid,
          {"file": s["filename"], "product": s["product"], "version": s["version"]})
    return {"ok": True}


# ---------------- review workflow ----------------

class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(ok|issue)$")
    comment: str = Field("", max_length=2000)


@router.put("/scans/{sid}/findings/{key}")
def decide(sid: int, key: str, body: DecisionIn, request: Request, user=Depends(auth.require("reviewer"))):
    s = scan_or_404(sid, user)
    if not permissions(s, user)["decide"]:
        raise HTTPException(409, "Findings cannot be changed on this scan now.")
    if key not in {f["key"] for f in s["result"]["findings"]}:
        raise HTTPException(404, "Finding not found.")
    db.add_finding_review(sid, key, body.decision, body.comment.strip(), user["id"])
    audit(request, user, "finding_decided", "scan", sid,
          {"finding": key, "decision": body.decision, "comment": body.comment.strip()})
    return {"ok": True, "decisions": db.finding_decisions(sid)}


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/scans/{sid}/comments")
def comment(sid: int, body: CommentIn, request: Request, user=Depends(auth.session_only)):
    scan_or_404(sid, user)
    cid = db.add_comment(sid, user["id"], body.text.strip())
    audit(request, user, "comment_added", "scan", sid, {"comment_id": cid})
    return {"ok": True, "comments": db.list_comments(sid)}


class ReviewIn(BaseModel):
    action: str = Field(pattern="^(submit|approve|request_changes)$")
    note: str = Field("", max_length=4000)


def open_items(s):
    """Findings that still block approval: anything not pass/na without an 'ok' decision."""
    dec = db.finding_decisions(s["id"])
    pending, issues = [], []
    for f in s["result"]["findings"]:
        if f["status"] in ("pass", "na"):
            continue
        d = dec.get(f["key"])
        if d is None:
            pending.append(f["key"])
        elif d["decision"] == "issue":
            issues.append(f["key"])
    return pending, issues


def _scan_url(sid):
    return f"{settings.public_url}/#/scan/{sid}"


@router.post("/scans/{sid}/review")
def review(sid: int, body: ReviewIn, request: Request, user=Depends(auth.session_only)):
    s = scan_or_404(sid, user)
    can = permissions(s, user)
    note = body.note.strip()
    if body.action == "submit":
        if not can["submit"]:
            raise HTTPException(409, "This scan cannot be submitted for review now.")
        new = "in_review"
    elif body.action == "approve":
        if auth.ROLES[user["role"]] < 2:
            raise HTTPException(403, "This needs the reviewer role.")
        if not can["approve"]:
            msg = ("You uploaded this label; another reviewer must approve it (four-eyes rule)."
                   if settings.four_eyes and s["user_id"] == user["id"] and s["review_status"] == "in_review"
                   else "Only labels in review can be approved.")
            raise HTTPException(409, msg)
        pending, issues = open_items(s)
        if issues:
            raise HTTPException(409, f"Cannot approve: {len(issues)} finding(s) confirmed as issues. "
                                     "Request changes instead.")
        if pending:
            raise HTTPException(409, f"Cannot approve: {len(pending)} finding(s) still need a decision.")
        new = "approved"
    else:
        if auth.ROLES[user["role"]] < 2:
            raise HTTPException(403, "This needs the reviewer role.")
        if s["review_status"] != "in_review":
            raise HTTPException(409, "Only labels in review can be sent back.")
        if not note:
            raise HTTPException(400, "Say what must change.")
        new = "changes_requested"
    db.set_review_status(sid, new, user["id"])
    if note:
        db.add_comment(sid, user["id"], note)
    audit(request, user, f"review_{body.action}", "scan", sid, {"note": note} if note else None)
    _notify(s, new, user, note)
    after = db.get_scan(sid)
    event = {"in_review": "review.submitted", "approved": "review.approved",
             "changes_requested": "review.changes_requested"}[new]
    webhooks.emit(after["org_id"], event, {**webhooks.scan_data(after), "by": user["name"], "note": note or None})
    return scan_payload(after, user)


def _notify(s, new, user, note):
    if new == "in_review":
        for r in db.org_reviewers(s["org_id"], exclude_user=user["id"]):
            mailer.review_requested(r["email"], r["name"], s, user["name"], _scan_url(s["id"]))
    else:
        up = db.user_by_id(s["user_id"])
        if up and up["active"] and up["notify"] and up["id"] != user["id"]:
            mailer.review_result(up["email"], up["name"], s, new, user["name"], note, _scan_url(s["id"]))


@router.get("/dashboard")
def dashboard(user=Depends(auth.current_user)):
    return db.dashboard(user["org_id"])
