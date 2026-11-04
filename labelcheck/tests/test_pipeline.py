"""End-to-end tests. Run: pytest -q

Synthetic labels prove the pipeline works. They do NOT prove real-world
accuracy; that needs photographed labels (see tools/accuracy.py).
Set TEST_DATABASE_URL=postgresql+psycopg://... to run the same tests on PostgreSQL.
"""
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"

from app import auth, db  # noqa: E402

PW = "Testpass123"


@pytest.fixture(scope="session", autouse=True)
def samples():
    subprocess.run([sys.executable, str(ROOT / "tests" / "make_samples.py")], check=True)


@pytest.fixture(scope="session")
def org(client):
    """One company with three users, plus a second company to test separation."""
    o = db.create_org("Sunrise Foods", "enterprise")
    ids = {r: db.create_user(o, f"{r}@sunrise.test", r.title(), auth.hash_password(PW), r)
           for r in ("admin", "reviewer", "uploader")}
    ids["reviewer2"] = db.create_user(o, "reviewer2@sunrise.test", "Reviewer Two", auth.hash_password(PW), "reviewer")
    o2 = db.create_org("Other Co", "enterprise")
    ids["other"] = db.create_user(o2, "admin@other.test", "Other", auth.hash_password(PW), "admin")
    return ids


def token(client, who):
    email = "admin@other.test" if who == "other" else f"{who}@sunrise.test"
    r = client.post("/api/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture(scope="session")
def H(client, org):
    return {w: token(client, w) for w in ("admin", "reviewer", "reviewer2", "uploader", "other")}


def scan(client, h, name, folder=SAMPLES, **form):
    form.setdefault("panel_width_mm", "100")   # samples: 1200 x 900 px at 12 px/mm
    form.setdefault("panel_height_mm", "75")
    path = Path(folder) / name
    if not path.suffix:
        path = path.with_suffix(".png")
    with open(path, "rb") as f:
        r = client.post("/api/scans", files={"image": (path.name, f)}, data=form, headers=h)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "done", j
    return j


def statuses(j, rule):
    return [f["status"] for f in j["result"]["findings"] if f["rule"] == rule]


# ---------------- rules / OCR ----------------

def test_good_label_has_no_failures(client, H):
    r = scan(client, H["uploader"], "good_label", product_name="Roasted Peanuts")
    assert r["result"]["summary"]["counts"]["fail"] == 0, [f for f in r["result"]["findings"] if f["status"] == "fail"]
    assert r["extracted"]["mrp"]["amount"] == 120
    assert r["extracted"]["net_quantity"]["base_value"] == 500
    assert "manufacturer" in r["extracted"]["parties"]


def test_bad_label_catches_each_planted_problem(client, H):
    r = scan(client, H["uploader"], "bad_label", product_name="Roasted Peanuts")
    assert "fail" in statuses(r, "6(1)(e)")  # MRP missing
    assert "fail" in statuses(r, "6(2)")     # no phone
    assert "fail" in statuses(r, "7(2)")     # numerals too small
    assert "fail" in statuses(r, "8(1)")     # text in clear zone
    assert "warn" in statuses(r, "6(1)(a)")  # marketer only
    assert r["result"]["summary"]["verdict"] == "issues_found"


def test_low_contrast_quantity_fails_9_1(client, H):
    r = scan(client, H["uploader"], "low_contrast_label", product_name="Roasted Peanuts")
    assert "fail" in statuses(r, "9(1)")


def test_bilingual_label_reads_hindi_and_english(client, H):
    r = scan(client, H["uploader"], "bilingual_label", product_name="Mustard Oil")
    assert r["extracted"]["scripts"]["devanagari"] > 0
    assert r["extracted"]["net_quantity"]["base_value"] == 1000
    assert r["result"]["summary"]["counts"]["fail"] == 0


def test_no_panel_size_means_manual_height_check(client, H):
    r = scan(client, H["uploader"], "good_label", product_name="Roasted Peanuts",
             panel_width_mm="", panel_height_mm="")
    assert statuses(r, "7(2)") == ["manual"]


def test_one_panel_dimension_is_enough(client, H):
    r = scan(client, H["uploader"], "good_label", product_name="Roasted Peanuts", panel_height_mm="")
    assert r["result"]["options_used"]["panel_height_mm"] == 75.0
    assert any("worked out from the width" in n for n in r["prep"])


def test_food_category_exemptions(client, H):
    r = scan(client, H["uploader"], "good_label", product_name="Roasted Peanuts", category="food")
    assert statuses(r, "6(1)(a)") == ["na"]
    assert statuses(r, "FSS 5(6)") == ["pass"]


def test_listing_mismatch_detected(client, H):
    r = scan(client, H["uploader"], "good_label", product_name="Roasted Peanuts",
             listing_text=(SAMPLES / "listing_mismatch.txt").read_text())
    details = " ".join(f["detail"] for f in r["result"]["findings"] if f["rule"] == "6(10)")
    assert "MRP differs" in details and "Net quantity differs" in details


def test_listing_match_passes(client, H):
    r = scan(client, H["uploader"], "good_label", product_name="Roasted Peanuts",
             listing_text=(SAMPLES / "listing_match.txt").read_text())
    assert "fail" not in statuses(r, "6(10)")


def test_landline_with_spaces_is_a_phone():
    from app.extract import extract_from_text
    e = extract_from_text("Consumer Care: Alpina Trading, Mumbai 400069\nPh: 022 4000 1234")
    assert e["consumer_care"]["phone"]


def test_unreadable_unit_is_check_not_fail():
    from app.extract import extract_from_text
    e = extract_from_text("Net Wt. 500 9")  # OCR misread of '500 g'
    assert e["net_quantity"]["value"] is None and e["net_quantity"]["number"] == 500


# ---------------- inputs ----------------

def test_pdf_artwork_uses_page_size(client, H):
    r = scan(client, H["uploader"], "good_label.pdf", product_name="Roasted Peanuts",
             panel_width_mm="", panel_height_mm="")
    used = r["result"]["options_used"]
    assert abs(used["panel_width_mm"] - 100) < 0.5 and abs(used["panel_height_mm"] - 75) < 0.5
    assert r["source_type"] == "pdf"
    assert "manual" not in statuses(r, "7(2)")
    assert r["result"]["summary"]["counts"]["fail"] == 0


def test_tilted_photo_is_straightened(client, H):
    r = scan(client, H["uploader"], "tilted_label.jpg", product_name="Roasted Peanuts", auto_straighten="true")
    assert any("rotated" in n for n in r["prep"])
    assert r["extracted"]["mrp"]["amount"] == 120


def test_rejects_non_image(client, H):
    r = client.post("/api/scans", files={"image": ("x.txt", b"hello")}, headers=H["uploader"])
    assert r.status_code == 400


def test_rejects_fake_extension(client, H):
    r = client.post("/api/scans", files={"image": ("evil.png", b"<?php echo 1; ?>")}, headers=H["uploader"])
    assert r.status_code == 400


# ---------------- auth / security ----------------

def test_requires_login(client):
    client.cookies.clear()
    assert client.get("/api/scans").status_code == 401
    assert client.get("/api/dashboard").status_code == 401


def test_wrong_password_and_lockout(client, org):
    for _ in range(5):
        r = client.post("/api/auth/login", json={"email": "lock@x.test", "password": "nope"})
        assert r.status_code == 401
    assert client.post("/api/auth/login", json={"email": "lock@x.test", "password": "nope"}).status_code == 429


def test_cookie_session_needs_csrf_header(client, org):
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": "uploader@sunrise.test", "password": PW})
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 200         # cookie works for reads
    r = client.post("/api/scans/1/comments", json={"text": "hi"})  # no X-LabelCheck header
    assert r.status_code == 403
    r = client.post("/api/auth/logout", headers={"X-LabelCheck": "1"})
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.clear()


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_roles_enforced(client, H):
    assert client.get("/api/users", headers=H["uploader"]).status_code == 403
    assert client.get("/api/users", headers=H["admin"]).status_code == 200
    assert client.get("/api/audit", headers=H["reviewer"]).status_code == 403


def test_weak_password_rejected(client, H):
    r = client.post("/api/users", json={"email": "new@sunrise.test", "name": "New", "role": "uploader",
                                        "password": "short"}, headers=H["admin"])
    assert r.status_code == 400


def test_company_data_is_separated(client, H):
    sid = scan(client, H["uploader"], "good_label", product_name="Secret Product")["id"]
    assert client.get(f"/api/scans/{sid}", headers=H["other"]).status_code == 404
    assert client.get(f"/api/scans/{sid}/image", headers=H["other"]).status_code == 404
    assert all(s["id"] != sid for s in client.get("/api/scans", headers=H["other"]).json())


def test_password_hash_not_plain(org):
    u = db.user_by_email("admin@sunrise.test")
    assert u["password_hash"].startswith("$argon2id$") and PW not in u["password_hash"]


# ---------------- review workflow ----------------

def test_full_review_workflow(client, H):
    j = scan(client, H["uploader"], "bad_label", product_name="Review Peanuts", sku="RV-1")
    sid = j["id"]
    # uploader cannot decide findings
    key = j["result"]["findings"][0]["key"]
    assert client.put(f"/api/scans/{sid}/findings/{key}", json={"decision": "ok"},
                      headers=H["uploader"]).status_code == 403
    r = client.post(f"/api/scans/{sid}/review", json={"action": "submit"}, headers=H["uploader"])
    assert r.json()["review_status"] == "in_review"
    # cannot approve with undecided findings
    r = client.post(f"/api/scans/{sid}/review", json={"action": "approve"}, headers=H["reviewer"])
    assert r.status_code == 409 and "need a decision" in r.json()["detail"]
    # confirm one issue -> approve blocked, request changes works
    fail_key = next(f["key"] for f in j["result"]["findings"] if f["status"] == "fail")
    assert client.put(f"/api/scans/{sid}/findings/{fail_key}", json={"decision": "issue", "comment": "MRP missing"},
                      headers=H["reviewer"]).status_code == 200
    r = client.post(f"/api/scans/{sid}/review", json={"action": "request_changes", "note": "Add MRP"},
                    headers=H["reviewer"])
    assert r.json()["review_status"] == "changes_requested"
    assert any(c["text"] == "Add MRP" for c in r.json()["comments"])
    # PDF report includes the review
    pdf = client.get(f"/api/scans/{sid}/report.pdf", headers=H["uploader"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    docx = client.get(f"/api/scans/{sid}/report.docx", headers=H["uploader"])
    assert docx.status_code == 200 and docx.content[:2] == b"PK"
    # audit trail recorded
    actions = [a["action"] for a in client.get(f"/api/scans/{sid}/audit", headers=H["uploader"]).json()]
    for a in ("scan_uploaded", "review_submit", "finding_decided", "review_request_changes"):
        assert a in actions


def test_approve_after_all_findings_ok(client, H):
    j = scan(client, H["uploader"], "good_label", product_name="Approve Peanuts", sku="AP-1")
    sid = j["id"]
    client.post(f"/api/scans/{sid}/review", json={"action": "submit"}, headers=H["uploader"])
    for f in j["result"]["findings"]:
        if f["status"] not in ("pass", "na"):
            client.put(f"/api/scans/{sid}/findings/{f['key']}", json={"decision": "ok"}, headers=H["reviewer"])
    r = client.post(f"/api/scans/{sid}/review", json={"action": "approve"}, headers=H["reviewer"])
    assert r.status_code == 200 and r.json()["review_status"] == "approved"
    assert r.json()["reviewed_by"] == "Reviewer"


def test_four_eyes_reviewer_cannot_approve_own_upload(client, H):
    j = scan(client, H["reviewer"], "good_label", product_name="Own Peanuts", sku="OWN-1")
    sid = j["id"]
    client.post(f"/api/scans/{sid}/review", json={"action": "submit"}, headers=H["reviewer"])
    for f in j["result"]["findings"]:
        if f["status"] not in ("pass", "na"):
            client.put(f"/api/scans/{sid}/findings/{f['key']}", json={"decision": "ok"}, headers=H["reviewer"])
    r = client.post(f"/api/scans/{sid}/review", json={"action": "approve"}, headers=H["reviewer"])
    assert r.status_code == 409 and "four-eyes" in r.json()["detail"]
    r = client.post(f"/api/scans/{sid}/review", json={"action": "approve"}, headers=H["reviewer2"])
    assert r.status_code == 200


# ---------------- versions ----------------

def test_versions_and_compare(client, H):
    v1 = scan(client, H["uploader"], "bad_label", product_name="Peanuts", sku="CMP-1")
    v2 = scan(client, H["uploader"], "good_label", product_name="Peanuts", sku="CMP-1")
    assert (v1["version"], v2["version"]) == (1, 2)
    assert v2["previous_id"] == v1["id"]
    c = client.get(f"/api/scans/{v2['id']}/compare", headers=H["uploader"]).json()
    fixed = {r["rule"] for r in c["rules"] if r["change"] == "fixed"}
    assert {"6(1)(e)", "6(2)", "7(2)"} <= fixed
    mrp = next(f for f in c["fields"] if f["field"] == "MRP")
    assert mrp["old"] is None and mrp["new"] and mrp["changed"]


# ---------------- batches ----------------

def test_batch_upload_with_manifest(client, H):
    manifest = ("file,product_name,sku,category,panel_width_mm,panel_height_mm\n"
                "good_label.png,Peanuts A,B-1,general,100,75\n"
                "bad_label.png,Peanuts B,B-2,general,100,75\n"
                "missing.png,Ghost,B-3,general,100,75\n")
    files = [("files", ("good_label.png", (SAMPLES / "good_label.png").read_bytes())),
             ("files", ("bad_label.png", (SAMPLES / "bad_label.png").read_bytes())),
             ("files", ("good_label.pdf", (SAMPLES / "good_label.pdf").read_bytes())),
             ("files", ("notes.txt", b"not a label")),
             ("manifest", ("m.csv", manifest.encode()))]
    r = client.post("/api/batches", files=files, data={"name": "Test batch", "panel_width_mm": "100",
                                                       "panel_height_mm": "75"}, headers=H["uploader"])
    assert r.status_code == 200, r.text
    b = r.json()
    assert len(b["scan_ids"]) == 3
    assert b["rejected"][0]["file"] == "notes.txt"
    assert b["manifest_rows_without_file"] == ["missing.png"]
    from app import pipeline
    assert pipeline.wait_for(b["scan_ids"], timeout=300)
    d = client.get(f"/api/batches/{b['id']}", headers=H["uploader"]).json()
    assert d["status_counts"] == {"done": 3}
    by_file = {s["filename"]: s for s in d["scans"]}
    assert by_file["good_label.png"]["sku"] == "B-1" and by_file["bad_label.png"]["fails"] > 0
    # file not in the manifest: no product name invented, so no false 6(1)(b) failure
    assert not by_file["good_label.pdf"]["product"] and by_file["good_label.pdf"]["fails"] == 0
    z = client.get(f"/api/batches/{b['id']}/reports.zip", headers=H["uploader"])
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert "summary.csv" in names and sum(n.endswith(".pdf") for n in names) == 3
    csv_text = client.get(f"/api/batches/{b['id']}/summary.csv", headers=H["uploader"]).text
    assert "6(1)(e)" in csv_text


# ---------------- URL safety and listing text ----------------

def test_url_safety_blocks_private_addresses():
    from app.listing import FetchError, check_url
    for url in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd",
                "http://localhost:8000/api/health"):
        with pytest.raises(FetchError):
            check_url(url)


def test_listing_html_to_text_reads_json_ld():
    from app.listing import html_to_text
    html = """<html><head><script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Product","name":"SUNRISE Roasted Peanuts 500 g",
       "offers":{"@type":"Offer","price":"99","priceCurrency":"INR"}}</script></head>
      <body><nav>menu</nav><p>Net Wt. 500 g</p><script>var x=1</script></body></html>"""
    t = html_to_text(html)
    assert "MRP Rs. 99" in t and "Net Wt. 500 g" in t and "var x" not in t and "menu" not in t


def test_dashboard(client, H):
    d = client.get("/api/dashboard", headers=H["uploader"]).json()
    assert d["total"] >= 1 and "by_review" in d


# ---------------- amended Legal Metrology rules + FSSAI ----------------

def test_full_fssai_food_label_passes(client, H):
    r = scan(client, H["uploader"], "food_label", product_name="Roasted Peanuts", category="food",
             panel_width_mm="200", panel_height_mm="150")
    bad = [(f["rule"], f["detail"]) for f in r["result"]["findings"] if f["status"] in ("fail", "warn")]
    assert not bad, bad
    for rule in ("FSS 5(2)", "FSS 5(3)", "FSS 5(4)", "FSS 5(7)", "FSS 5(9)", "FSS 5(10)", "FSS 5(14)"):
        assert statuses(r, rule) == ["pass"], rule


def test_food_label_missing_fssai_items(client, H):
    # good_label has no ingredients, nutrition, licence, batch, veg symbol or use-by date
    r = scan(client, H["uploader"], "good_label", product_name="Roasted Peanuts", category="food",
             panel_width_mm="200", panel_height_mm="150")
    for rule in ("FSS 5(2)", "FSS 5(3)", "FSS 5(7)", "FSS 5(9)", "FSS 5(10)"):
        assert statuses(r, rule) == ["fail"], rule
    assert statuses(r, "FSS 5(4)") == ["warn"]


def test_small_food_pack_missing_items_are_manual_not_na(client, H):
    r = scan(client, H["uploader"], "good_label", product_name="Roasted Peanuts", category="food")  # 75 cm2
    for rule in ("FSS 5(2)", "FSS 5(3)", "FSS 5(7)", "FSS 5(9)"):
        assert statuses(r, rule) == ["manual"], rule


def test_table_i_embossed_column(client, H):
    # 75 cm2 panel: printed needs 1.5 mm (passes), blown/moulded needs 3.0 mm (small text fails)
    r = scan(client, H["uploader"], "good_label", product_name="Roasted Peanuts", embossed="true")
    assert "fail" in statuses(r, "7(2)")
    assert any("3.0 mm" in f["detail"] for f in r["result"]["findings"] if f["rule"] == "7(2)")


def test_amended_extractors():
    from app.extract import extract_from_text
    e = extract_from_text("Net Wt. 1 kg Jumbo Pack\nMRP Rs 250\nUSP Rs 250.00 per kg\nPkd. Date 08/2026")
    assert e["quantity_qualifier"]["word"] == "Jumbo"
    assert e["tax_inclusive"] is None                     # no 'inclusive of all taxes'
    assert e["usp"]["per"] == "kg" and e["usp"]["amount"] == 250
    assert e["date"]["date_kind"] == "packing"            # 6(1)(d) needs manufacture since 2022
    e = extract_from_text("Mfg. Date: 08/2026   Use By: 05/2027")
    assert e["expiry"]["value"] == "05/2027" and e["expiry"]["kind"] == "use by"


def test_bad_label_unit_sale_price_and_tax_wording(client, H):
    r = scan(client, H["uploader"], "good_label", product_name="Roasted Peanuts")
    assert statuses(r, "6(11)") == ["pass"] and statuses(r, "6(1)(e)") == ["pass"]


def test_usp_unit_follows_pack_size():
    """GSR 226(E): per g below 1 kg, per kg above; exactly 1 kg either."""
    from app import engine
    from app.extract import extract_from_text as E
    def usp_findings(text):
        e = E(text)
        return [(f["rule"], f["status"]) for f in engine._usp_findings(e, e["net_quantity"], e["mrp"])]
    assert ("6(11)-format", "fail") in usp_findings("Net Wt. 500 g\nMRP Rs 120\nUSP Rs 240.00 per kg")
    assert usp_findings("Net Wt. 500 g\nMRP Rs 120\nUSP Rs 0.24 per g") == [("6(11)", "pass")]
    assert usp_findings("Net Wt. 1 kg\nMRP Rs 240\nUSP Rs 0.24 per g") == [("6(11)", "pass")]
    assert ("6(11)-format", "fail") in usp_findings("Net Wt. 2 kg\nMRP Rs 400\nUSP Rs 0.20 per g")


def test_fssai_licence_any_first_digit():
    from app.extract import extract_from_text as E
    assert E("FSSAI Lic. No. 30012345678901")["fssai"]["licence"] == "30012345678901"
