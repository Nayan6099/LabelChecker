# LabelCheck 0.4

LabelCheck checks product labels (photos, scans or PDF artwork) and e-commerce listings against Indian
labelling law, runs a reviewer approval workflow with a full audit trail, and serves many customer
companies from one deployment (SaaS). Built for SIH26034.

- **Legal Metrology (Packaged Commodities) Rules, 2011**, as amended up to GSR 826(E) (21 Sep 2026)
- **FSSAI Labelling and Display Regulations, 2020** (food category; compendium Version VIII + 24 Mar 2026 amendment)

Design rule: **AI reads, code judges.** OCR (Tesseract) only reads the label. Every PASS / FAIL is plain
code in `app/engine.py` pointing to a rule in `app/rules_data.py`, so the same input always gets the
same verdict. Assistive tool, not legal advice (`/terms.html`, `/privacy.html`).

Other documents:

- `docs/integration.md`: API keys and webhooks, for customers' developers
- `docs/research/`: sources for every rule
- `deploy/prometheus/`: monitoring config and alerts

---

## Architecture

```
            internet
               |
        +------v------+   HTTPS (Let's Encrypt), HSTS, blocks /metrics
        |    Caddy    |
        +------+------+
               |
        +------v------+   FastAPI: UI, REST API, login, reviews. No OCR here.
        |     web     |   LABELCHECK_WORKERS=0. Scale out freely.
        +------+------+
               |  PostgreSQL = data + job queue + rate limits + webhook outbox
        +------v------+          +------------------+
        |  PostgreSQL |<---------|  worker  (xN)    |  OCR + rules, webhooks, e-mail,
        +-------------+          |  python -m       |  housekeeping (retention, cleanup)
                                 |  app.worker      |  metrics on :9101
                                 +--------+---------+
                                          |
                          files: /data volume or S3 (LABELCHECK_STORAGE=s3)
```

- **One image, two roles:** the default command is the web server; `python -m app.worker` is the worker.
- **The database is the queue:** no Redis or RabbitMQ needed. Workers claim jobs with conditional updates, so they can run on several servers.
- **Migrations run on start-up** behind a PostgreSQL advisory lock, so web and workers can start together. You can also run `python -m app.cli migrate` as a release step.
- **Graceful stop:** SIGTERM makes a worker finish the scans in hand. Queued scans stay queued. A crashed worker's scans are re-queued after 15 minutes (`LABELCHECK_STALE_MINUTES`).

## Features

| Area | What |
|---|---|
| Input | JPG, PNG, WEBP, BMP, TIFF, PDF artwork (panel size from the page). Photo auto-straighten. |
| Checks | Legal Metrology 6(1)(a)-(f), (aa), (da), 6(2), 6(3), 6(10), 6(11), 7(2) Table-I, 7(3), 8(1), 9(1), 9(4), 12(6). FSSAI 5(1)-(14), veg / non-veg symbol, licence, nutrition, allergens, 6(3). |
| Workflow | Roles uploader < reviewer < admin. OK / Issue per finding, approve or request changes, four-eyes rule, comments, audit log. Versions by SKU with compare. |
| Bulk | Up to 200 files per batch, CSV manifest, summary CSV, ZIP of PDF reports, e-mail when done |
| Accounts | Invites by e-mail, forgot / reset password, 2FA (TOTP, QR code), company-wide "require 2FA", login lockout |
| SaaS | Platform admin: customer companies, plans (Trial 100 scans / 3 users, Standard 2,000 / 10, Business 10,000 / 50, Enterprise unlimited), monthly usage, disable company |
| Data | Delete scan or user (personal data erased), retention period per company, export everything as ZIP (DPDP right of access) |
| Integration | API keys (read / read+write scopes, rate-limited), OpenAPI docs at `/api/docs`, **signed webhooks** with retries and a delivery log |
| Operations | `/api/health/live`, `/api/health/ready` (DB, storage, OCR), Prometheus `/metrics` (token), JSON logs with request id, S3 storage, backups, load-test tool |

---

## Run on your PC (development)

Needs Python 3.11+ and Tesseract (`sudo apt install tesseract-ocr tesseract-ocr-hin fonts-dejavu-core fonts-freefont-ttf`).

```bash
pip install -r requirements-dev.txt
export LABELCHECK_COOKIE_SECURE=0              # only because http://localhost has no HTTPS
python -m app.cli create-admin --org "Sunrise Foods" --email admin@sunrise.in --name "Asha" --platform
uvicorn app.main:app --reload --port 8000      # web + 2 built-in workers; open http://localhost:8000
pytest -q                                      # 73 tests (set TEST_DATABASE_URL to run them on PostgreSQL)
```

Without SMTP settings, e-mails (invites, resets) are printed to the server log.

## Run in production (docker compose, one server)

1. Point the domain's DNS A record to the server. Open only ports 80 and 443.
2. `cp .env.example .env` and fill in:
   - the domain and `LABELCHECK_PUBLIC_URL`;
   - `POSTGRES_PASSWORD`;
   - `LABELCHECK_SECRET_KEY` (48 random characters, never change it later);
   - SMTP details;
   - the first admin.
   > wsl
3. `docker compose up -d --build`. Caddy gets the certificate, then open `https://<domain>`.
4. Log in as the first admin, change the password, turn on 2FA.
5. Scale OCR with `docker compose up -d --scale worker=3` (about 1 worker per CPU core).
6. Back up nightly with cron: `scripts/backup.sh /backups` (pg_dump + files, keeps 14 days). Copy the backups off the server.

`LABELCHECK_ENV=production` (set in compose) **refuses to start** on unsafe settings. It requires:

- HTTPS cookies;
- PostgreSQL;
- a secret key;
- an `https://` public URL;
- the test-only private-address switch to be off.

Check before deploying: `docker compose run --rm web python -m app.cli check-config`.

### On AWS instead of one server

- **Database:** RDS PostgreSQL 17 replaces the `db` service (`DATABASE_URL=postgresql+psycopg://...`).
- **Files:** S3 replaces the `/data` volume (`LABELCHECK_STORAGE=s3`, `LABELCHECK_S3_BUCKET`, `AWS_REGION`). Give the tasks an IAM role with `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` and `s3:ListBucket` on that bucket or prefix, and turn on bucket versioning.
- **Proxy:** an ALB with an ACM certificate replaces Caddy. Set `LABELCHECK_TRUST_PROXY=1` and block `/metrics` on the ALB.
- **Containers:** run web and worker as two ECS services from the same image (worker command `python -m app.worker`). Scale workers on CPU or on the `labelcheck_queue{status="queued"}` metric.
- **E-mail:** SES SMTP for `LABELCHECK_SMTP_*`.

### Sizing (measured)

On a 2-vCPU machine with 2 workers: **about 14 labels per minute** (one label about 8.5 s per core,
p95 upload response 467 ms, p95 list / dashboard 43 ms during OCR, no errors). Plan about 7 labels per
minute per CPU core for workers. The web container needs little CPU.

Measure your own server with `python -m tools.loadtest --url https://staging... --email ... --password ... --scans 60 --concurrency 8`.

---

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `LABELCHECK_ENV` | development | `production` turns on the safety checks and JSON logs |
| `DATABASE_URL` | SQLite in data dir | `postgresql+psycopg://user:pass@host:5432/db` (required in production) |
| `LABELCHECK_PUBLIC_URL` | http://localhost:8000 | Address users open; used in e-mail links and webhooks |
| `LABELCHECK_SECRET_KEY` | - | 32+ chars; encrypts 2FA and webhook secrets. **Changing it breaks both** |
| `LABELCHECK_DATA` | ./data | Local files (uploads, checked images, SQLite) |
| `LABELCHECK_STORAGE` | local | `local` or `s3` (+ `LABELCHECK_S3_BUCKET`, `LABELCHECK_S3_PREFIX`, `LABELCHECK_S3_ENDPOINT`, `AWS_REGION`) |
| `LABELCHECK_WORKERS` | 2 | Scan threads in this process (0 in the web container) |
| `LABELCHECK_WORKER_METRICS_PORT` | 0 | Worker's Prometheus port (9101 in compose) |
| `LABELCHECK_STALE_MINUTES` | 15 | Re-queue scans whose worker stopped responding |
| `LABELCHECK_MAINTENANCE_MINUTES` | 60 | Housekeeping interval (retention, expired sessions) |
| `LABELCHECK_WEBHOOK_POLL_SECONDS` | 3 | How often workers send due webhooks |
| `LABELCHECK_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_TLS` | - / 587 / - / - / starttls | Mail server (`starttls`, `ssl` or `none`) |
| `LABELCHECK_MAIL_FROM` | LabelCheck <no-reply@localhost> | Sender address |
| `LABELCHECK_COOKIE_SECURE` | 1 | Session cookie over HTTPS only. Set 0 only for http://localhost |
| `LABELCHECK_TRUST_PROXY` | 0 | 1 behind Caddy or an ALB (real client IP from X-Forwarded-For) |
| `LABELCHECK_SESSION_HOURS` | 12 | Login session length |
| `LABELCHECK_FOUR_EYES` | 1 | The uploader of a label cannot approve it |
| `LABELCHECK_ENABLE_DOCS` | 1 | Serve `/api/docs` |
| `LABELCHECK_METRICS_TOKEN` | - | Bearer token for `/metrics` on the web server |
| `LABELCHECK_LOG_FORMAT` / `_LEVEL` | json in prod / INFO | `json` or `text` |
| `LABELCHECK_RATE_UPLOADS_PER_MIN` | 120 | Uploads per company per minute |
| `LABELCHECK_RATE_API_PER_MIN` | 600 | Requests per API key per minute |
| `LABELCHECK_ADMIN_EMAIL` / `_PASSWORD`, `LABELCHECK_ORG_NAME`, `LABELCHECK_ADMIN_IS_PLATFORM` | - | First admin, created once on an empty database |
| `LABELCHECK_ALLOW_PRIVATE_FETCH` | 0 | **Tests only**; refused in production |

Fixed limits:

- 25 MB per file, 40 megapixels, 50 PDF pages;
- 200 files or 400 MB per batch;
- login: 5 failures per email and IP lock that pair for 15 minutes; 60 attempts per IP per 15 minutes;
- exports: 5 per company per hour;
- webhooks: 10 per company.

## Admin commands

```
python -m app.cli migrate                  apply database migrations (also runs automatically on start-up)
python -m app.cli check-config             list unsafe or missing settings (exit 1 on errors)
python -m app.cli create-admin --org .. --email .. --name .. [--plan enterprise] [--platform]
python -m app.cli maintenance              run housekeeping once
python -m app.cli backup --out DIR         SQLite + local files only (PostgreSQL: scripts/backup.sh)
python -m tools.accuracy template|run DIR  accuracy on real labels (see below)
python -m tools.loadtest --url ..          throughput and latency
```

## Monitoring

- **Health:** `/api/health/live` checks the process. `/api/health/ready` checks the database, storage and OCR, and is meant for load balancers.
- **Metrics:** HTTP requests and latency per route, scans by result, scan duration, queue depth, logins, e-mails and webhook deliveries.
- **Prometheus:** `deploy/prometheus/prometheus.yml` and `alerts.yml` hold 9 rules: down, 5xx rate, slow API, queue backlog, workers stuck, scan errors, slow OCR, login attacks, failing webhooks.
- **Logs:** one JSON line per request, with `request_id` (also returned as `X-Request-ID`), user, company, status and duration.

## Security

- **Passwords and sessions:** passwords use Argon2id. Session cookies are HttpOnly, Secure and SameSite=Strict. Only hashes of session tokens, reset/invite tokens and API keys are stored.
- **CSRF:** cookie requests that change data need the `X-LabelCheck: 1` header.
- **2FA:** TOTP with replay protection. The secret is encrypted with `LABELCHECK_SECRET_KEY`.
- **Rate limits and lockout:** stored in the database, so they hold across servers.
- **Company separation:** every query is filtered by company. Someone else's id returns 404.
- **Uploads:** file type checked by content (magic bytes), size and pixel limits, PDF page limit.
- **Outgoing requests** (listing links, webhooks): only public addresses on ports 80/443, checked on every request. Webhooks must use https in production.
- **Headers:** strict CSP with no inline scripts, `X-Frame-Options: DENY`, nosniff, HSTS (set by Caddy).
- **Containers:** non-root (uid 10001). Only Caddy is exposed.
- **CI:** ruff with security rules, pip-audit, Trivy image scan, Dependabot.

## Measuring accuracy on real labels

```bash
python -m tools.accuracy template my-labels/   # writes ground_truth.csv; fill in what is really on each label
python -m tools.accuracy run my-labels/        # report.html: field accuracy, FAIL precision / recall
```

The 25-label synthetic pack (`python tests/make_test_pack.py out/`) scores 100% on every field with
100% precision and recall, and CI keeps it there. Synthetic labels are not real labels: measure 50 to 200
real photos and artwork files before quoting any accuracy number to a customer.

## Rule set and how sure we are

| Source | Status |
|---|---|
| 2011 base text, amendments GSR 784(E) 2011 to 826(E) 2026 | Read from official notifications; Table-I checked on the PDF |
| GSR 226(E) 2022: unit-sale-price format | Secondary source only; that check is advisory (CHECK, never FAIL) |
| FSSAI Labelling & Display Regs 2020 | Compendium Version VIII + 24 Mar 2026 amendment (read via excerpts) |
| FSSAI 14-digit licence format | From the licensing regulations |
| Draft rules (FSSAI bold %RDA, front-of-pack warnings) | Not applied |

## Known limits

- **OCR:** can miss faint, tiny, glossy, curved or decorative text. A missed declaration shows as FAIL, so always check FAILs against the image.
- **Heights:** measured from the image, so they are approximate.
- **Manual checks:** stickers, letter width, "is this the main panel", FSSAI additives, ingredient order.
- **Not covered:** Drugs & Cosmetics, Medical Devices, BIS marks, garment tags.
- **Billing:** plans and quotas exist, but there is no payment gateway, so plans are changed by the platform admin.
- **No SSO** (SAML / Google) yet.

## Project layout

```
app/
  main.py          app, middleware (security headers, request id, metrics), routers
  api/             auth_routes, users (company admin, API keys, export), platform, scans, batches,
                   webhook_routes, ops (health, metrics, meta)
  auth.py          passwords, sessions, 2FA, API keys, CSRF, lockout, rate limits
  db.py            tables (SQLAlchemy), queries, queue, outbox; migrations in ../migrations
  pipeline.py      scan processing, worker threads, housekeeping, webhook sender
  worker.py        standalone worker process (python -m app.worker)
  webhooks.py      events, signing, delivery with retries
  mailer.py        SMTP e-mail (or log in development)
  storage.py       local disk or S3
  inputs.py ocr.py extract.py engine.py symbols.py listing.py reports.py rules_data.py
  config.py        all settings; production safety checks
  static/          web UI (index.html, app.js, app.css, terms.html, privacy.html)
migrations/        Alembic revisions 0001 (0.2 baseline), 0002 (0.3 platform), 0003 (0.4 webhooks)
tests/             pytest (73), e2e/ui-smoke.js (Playwright), sample + test-pack generators
tools/             accuracy.py, loadtest.py
deploy/            Caddyfile, prometheus/
docs/              integration.md, research/
.github/           CI (lint, audit, tests on SQLite + PostgreSQL, 3 browsers, Docker build + Trivy), Dependabot
```



       URL: http://localhost:8000
Email: admin@example.com
Password: SunriseAdmin2026!

