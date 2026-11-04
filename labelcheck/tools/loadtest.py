"""Load test: how many labels per minute a deployment can check, and how fast the API answers.

  python -m tools.loadtest --url https://labelcheck.example.in --email admin@co.in --password ... \\
         --scans 60 --concurrency 8 [--file samples/good_label.png] [--api-key lc_...]

What it does
  1. logs in (or uses an API key),
  2. uploads --scans labels with --concurrency parallel uploads (queued, wait=false),
  3. while they are processed, keeps calling GET /api/scans and /api/dashboard (read load),
  4. waits until every scan is done or failed, then prints:
     upload latency p50/p95, read latency p50/p95, end-to-end time per scan, throughput
     (labels per minute), and errors by HTTP status.

Use a separate test company: every upload counts against that company's monthly quota.
Run it against staging, not production. Throughput depends on the worker count and CPU
cores (OCR uses about one core per worker).
"""
import argparse
import asyncio
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import httpx


def pct(values, p):
    if not values:
        return float("nan")
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(p / 100 * (len(values) - 1))))
    return values[k]


async def login(client, a):
    if a.api_key:
        return {"Authorization": f"Bearer {a.api_key}"}
    r = await client.post("/api/auth/login", json={"email": a.email, "password": a.password, "totp": a.totp or ""})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def run(a):
    data = Path(a.file).read_bytes()
    errors, up_ms, read_ms = Counter(), [], []
    created = {}
    async with httpx.AsyncClient(base_url=a.url.rstrip("/"), timeout=120, verify=not a.insecure) as client:
        h = await login(client, a)
        sem = asyncio.Semaphore(a.concurrency)
        t0 = time.perf_counter()

        async def upload(i):
            async with sem:
                t = time.perf_counter()
                r = await client.post("/api/scans", headers=h, data={
                    "product_name": a.product, "panel_width_mm": a.width, "panel_height_mm": a.height,
                    "wait": "false"}, files={"image": (f"load-{i}{Path(a.file).suffix}", data)})
                up_ms.append((time.perf_counter() - t) * 1000)
                if r.status_code != 200:
                    errors[f"upload {r.status_code}"] += 1
                    return
                created[r.json()["id"]] = time.perf_counter()

        stop = asyncio.Event()

        async def reader():
            while not stop.is_set():
                for path in ("/api/scans", "/api/dashboard"):
                    t = time.perf_counter()
                    r = await client.get(path, headers=h)
                    read_ms.append((time.perf_counter() - t) * 1000)
                    if r.status_code != 200:
                        errors[f"read {r.status_code}"] += 1
                await asyncio.sleep(0.2)

        readers = [asyncio.create_task(reader()) for _ in range(a.readers)]
        await asyncio.gather(*(upload(i) for i in range(a.scans)))
        uploaded_at = time.perf_counter()
        finished, failed = {}, 0
        deadline = time.perf_counter() + a.timeout
        while len(finished) < len(created) and time.perf_counter() < deadline:
            await asyncio.sleep(1.0)
            for sid in [s for s in created if s not in finished]:
                r = await client.get(f"/api/scans/{sid}", headers=h)
                if r.status_code == 200 and r.json()["status"] in ("done", "error"):
                    finished[sid] = time.perf_counter()
                    failed += r.json()["status"] == "error"
        stop.set()
        await asyncio.gather(*readers)
        total = time.perf_counter() - t0

    e2e = [finished[s] - created[s] for s in finished]
    print(f"Target {a.url}  file {a.file}")
    print(f"Uploaded {len(created)}/{a.scans} (concurrency {a.concurrency}) in {uploaded_at - t0:.1f} s")
    print(f"Finished {len(finished)}  failed {failed}  still queued {len(created) - len(finished)}")
    print(f"Throughput     {60 * len(finished) / total:.1f} labels / minute (whole run {total:.1f} s)")
    print(f"Upload latency p50 {pct(up_ms, 50):.0f} ms  p95 {pct(up_ms, 95):.0f} ms")
    print(f"Read latency   p50 {pct(read_ms, 50):.0f} ms  p95 {pct(read_ms, 95):.0f} ms  ({len(read_ms)} requests)")
    if e2e:
        print(f"Upload->done   p50 {pct(e2e, 50):.1f} s  p95 {pct(e2e, 95):.1f} s  mean {statistics.mean(e2e):.1f} s")
    print("Errors         " + (", ".join(f"{k}: {v}" for k, v in errors.items()) or "none"))
    return 1 if errors or failed or len(finished) < a.scans else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m tools.loadtest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True)
    p.add_argument("--email")
    p.add_argument("--password")
    p.add_argument("--totp")
    p.add_argument("--api-key")
    p.add_argument("--file", default=str(Path(__file__).resolve().parent.parent / "samples" / "good_label.png"))
    p.add_argument("--product", default="Roasted Peanuts")
    p.add_argument("--width", default="100")
    p.add_argument("--height", default="75")
    p.add_argument("--scans", type=int, default=30)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--readers", type=int, default=2, help="parallel users browsing lists while scans run")
    p.add_argument("--timeout", type=int, default=900, help="seconds to wait for the queue to drain")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed staging)")
    a = p.parse_args(argv)
    if not a.api_key and not (a.email and a.password):
        p.error("give --api-key, or --email and --password")
    sys.exit(asyncio.run(run(a)))


if __name__ == "__main__":
    main()
