# Integrating with LabelCheck (API keys and webhooks)

For developers connecting an ERP, PLM, DAM or e-commerce back office to LabelCheck.
The full, interactive API reference is at `https://<your LabelCheck address>/api/docs`.

## 1. Get an API key

A company admin creates the key under **Company → API keys**. The key is shown once, so store it in
your secret manager.

- **Read + write:** can upload labels and read results.
- **Read only:** can only read results.
- A key acts as an *uploader*. It can never review or approve.
- Each key is limited to 600 requests per minute (`LABELCHECK_RATE_API_PER_MIN`).

Send the key in a header:

```
X-API-Key: lc_3623cf4c_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 2. Upload a label

```bash
curl -sS https://labelcheck.example.in/api/scans \
  -H "X-API-Key: $LABELCHECK_KEY" \
  -F image=@artwork.pdf \
  -F product_name="Roasted Peanuts" -F sku="PNT-500" -F category=food \
  -F wait=false
```

`wait=false` queues the scan and returns at once with `{"id": 123, "status": "queued", ...}`.
`wait=true` (the default) returns the finished result, but holds the connection open for 3 to 10 seconds.

Useful fields:

- `panel_width_mm` and `panel_height_mm` (not needed for PDF artwork)
- `imported=true`
- `embossed=true`
- `auto_straighten=true` (for photos)
- `listing_text` (paste listing text to verify mandatory declarations match the label)

The same `sku` again creates the next version of that product.

Bulk: `POST /api/batches`, with several `files` plus an optional `manifest` CSV. Download the
template from `/api/batches/template.csv`.

## 3. Get the result

- Poll `GET /api/scans/{id}` until `status` is `done` or `error`, **or**
- set up a webhook (below) and let LabelCheck tell you.

Reports: `GET /api/scans/{id}/report.pdf` and `/report.docx`. The original file is at `/upload`.

## 4. Webhooks

A company admin adds the endpoint under **Company → Webhooks** and chooses the events:

| Event | When |
|---|---|
| `scan.completed` | A label scan finished (any verdict) |
| `scan.failed` | The file could not be processed (e.g. damaged PDF) |
| `review.submitted` | A label was submitted for review |
| `review.approved` | A reviewer approved a label |
| `review.changes_requested` | A reviewer sent it back (the note is in `data.note`) |
| `batch.completed` | Every file of a bulk upload is finished |
| `ping` | Sent by the **Send test** button |

LabelCheck sends an HTTP `POST` with a JSON body:

```json
{"id": "dlv_8f3c...", "event": "review.approved", "created_at": "2026-11-03T09:30:12+00:00", "org_id": 7,
 "data": {"scan_id": 123, "file": "artwork.pdf", "product": "Roasted Peanuts", "sku": "PNT-500", "version": 2,
          "status": "done", "verdict": "needs_review", "counts": {"fail": 0, "check": 1, "manual": 5, "pass": 14},
          "failed_rules": [], "review_status": "approved", "by": "Priya Sharma", "note": null,
          "url": "https://labelcheck.example.in/#/scan/123", "api_url": "https://labelcheck.example.in/api/scans/123",
          "report_pdf": "https://labelcheck.example.in/api/scans/123/report.pdf"}}
```

Headers:

- `X-LabelCheck-Event`: the event name
- `X-LabelCheck-Delivery`: a unique delivery id
- `X-LabelCheck-Signature: t=<unix seconds>,v1=<hex>`

### Your endpoint must

1. **Verify the signature.** `v1` = HMAC-SHA256 of `"<t>." + raw request body`, with the signing
   secret shown when the webhook was created. Compare in constant time, using the raw body bytes, not
   re-serialised JSON.
2. **Reject old timestamps:** more than 5 minutes off means a possible replay.
3. **Answer 2xx within 10 seconds.** Do slow work afterwards, for example by putting the event on your own queue.
4. **Ignore duplicates.** The same event can arrive more than once, so de-duplicate on `X-LabelCheck-Delivery`.

### Retries

If your endpoint is down or answers non-2xx, LabelCheck retries after 1 minute, 5 minutes, 30 minutes,
2 hours, 6 hours and 12 hours. That is 7 attempts over about 21 hours. After 20 failed attempts in a
row, the webhook is switched off; turn it on again under Company → Webhooks.

Every attempt is listed under **Deliveries**. **Send again** re-sends a delivered or failed event.
**New secret** rotates the signing secret, and the old one stops working immediately.

Webhook URLs must be public `https://` addresses on port 443. Private and internal addresses are refused.

### Example receiver: Python (Flask)

```python
import hashlib, hmac, os, time
from flask import Flask, abort, request

app = Flask(__name__)
SECRET = os.environ["LABELCHECK_WEBHOOK_SECRET"].encode()
seen = set()          # use your database in production

def valid(header, body, tolerance=300):
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    expected = hmac.new(SECRET, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))

@app.post("/labelcheck/events")
def events():
    body = request.get_data()                      # raw bytes
    if not valid(request.headers.get("X-LabelCheck-Signature", ""), body):
        abort(401)
    delivery = request.headers["X-LabelCheck-Delivery"]
    if delivery in seen:
        return "", 200
    seen.add(delivery)
    event = request.get_json()
    if event["event"] == "review.approved":
        print("approved:", event["data"]["sku"], event["data"]["version"])
    return "", 200
```

### Example receiver: Node.js (Express)

```js
const crypto = require("crypto");
const express = require("express");
const app = express();
const SECRET = process.env.LABELCHECK_WEBHOOK_SECRET;

app.post("/labelcheck/events", express.raw({ type: "application/json" }), (req, res) => {
  const parts = Object.fromEntries((req.get("X-LabelCheck-Signature") || "").split(",").map(p => p.split("=")));
  const ts = Number(parts.t);
  if (!ts || Math.abs(Date.now() / 1000 - ts) > 300) return res.sendStatus(401);
  const expected = crypto.createHmac("sha256", SECRET).update(`${ts}.`).update(req.body).digest("hex");
  const given = Buffer.from(parts.v1 || "", "hex");
  if (given.length !== 32 || !crypto.timingSafeEqual(Buffer.from(expected, "hex"), given)) return res.sendStatus(401);
  const event = JSON.parse(req.body);
  console.log(event.event, event.data.scan_id);
  res.sendStatus(200);
});
app.listen(3000);
```
