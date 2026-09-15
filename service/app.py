"""HTTP front end for the website audit, sized for Cloud Run.

One request runs one audit and returns the PDF bytes directly. That keeps the
service stateless: no job store, no shared disk, and no risk of a follow-up
request landing on a different instance than the one that made the report.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from website_audit.analysis import analyse
from website_audit.blocking import detect as detect_block
from website_audit.blocking import explain as explain_block
from website_audit.browser import browser_session
from website_audit.collector import collect
from website_audit.report import render_html, html_to_pdf, write_json

from .security import UnsafeURL, guard_route, validate

log = logging.getLogger("website-audit")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Website design audit", docs_url=None, redoc_url=None)

AUDIT_TIMEOUT_MS = int(os.environ.get("AUDIT_TIMEOUT_MS", "45000"))
RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "10"))

# Per-instance only — it resets on cold start and is not shared between
# instances. Real protection belongs at the edge (Cloud Armor); this just stops
# one impatient visitor from queueing twenty audits.
_hits: dict[str, deque[float]] = {}
_hits_lock = threading.Lock()

# Chromium is not thread-safe and each instance is sized for one audit at a
# time; Cloud Run's --concurrency 1 enforces this too, and the lock makes it
# true regardless of how the service is run.
_audit_lock = threading.Lock()

STATIC = Path(__file__).parent / "static"
INDEX = STATIC / "index.html"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class BlockedError(RuntimeError):
    """The page we reached was a bot wall, not the site."""


class AuditRequest(BaseModel):
    url: str = ""
    json_only: bool = False


def rate_limited(client_ip: str) -> bool:
    now = time.time()
    cutoff = now - 3600
    with _hits_lock:
        seen = _hits.setdefault(client_ip, deque())
        while seen and seen[0] < cutoff:
            seen.popleft()
        if len(seen) >= RATE_LIMIT_PER_HOUR:
            return True
        seen.append(now)
        if len(_hits) > 10_000:  # bound the dict on a long-lived instance
            for key in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
                _hits.pop(key, None)
    return False


def client_ip(request: Request) -> str:
    # Cloud Run appends the caller to X-Forwarded-For; the first entry is the
    # original client. Trusted because only the load balancer can set it here.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def run_audit(target: str, work_dir: Path):
    """Render, analyse and print one report. Returns (pdf_path, data, results)."""
    with browser_session() as browser:
        data = collect(
            target,
            work_dir,
            timeout_ms=AUDIT_TIMEOUT_MS,
            browser=browser,
            route_guard=guard_route,
        )

        # A bot wall renders like any other page and would score like one. Refuse
        # rather than hand back an authoritative report about someone's firewall.
        verdict = detect_block(data)
        if verdict:
            raise BlockedError(explain_block(verdict, urlparse(data.final_url).netloc or target))

        results = analyse(data)
        pdf_path = work_dir / "report.pdf"
        html_to_pdf(render_html(data, results), pdf_path, work_dir, browser=browser)
    return pdf_path, data, results


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX.read_text(encoding="utf-8"))


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.post("/api/audit")
def audit(payload: AuditRequest, request: Request) -> Response:
    requested = payload.url
    want_json = payload.json_only

    if rate_limited(client_ip(request)):
        raise HTTPException(429, f"Rate limit reached ({RATE_LIMIT_PER_HOUR}/hour). Try again later.")

    try:
        safe = validate(requested)
    except UnsafeURL as exc:
        raise HTTPException(400, str(exc)) from exc

    work_dir = Path(tempfile.mkdtemp(prefix="audit-"))
    started = time.time()
    try:
        if not _audit_lock.acquire(timeout=120):
            raise HTTPException(503, "The auditor is busy. Try again in a moment.")
        try:
            pdf_path, data, results = run_audit(safe.url, work_dir)
        finally:
            _audit_lock.release()

        scores = results["scores"]
        log.info(
            "audited %s in %.1fs — score %s, %s findings",
            safe.hostname, time.time() - started, scores["overall"], len(results["findings"]),
        )

        if want_json:
            json_path = write_json(data, results, work_dir / "report.json")
            return Response(
                content=json_path.read_bytes(),
                media_type="application/json",
            )

        host = urlparse(data.final_url).netloc or safe.hostname
        filename = f"{host.replace(':', '-')}-audit.pdf"
        return Response(
            content=pdf_path.read_bytes(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                # The page reads these to render the summary without a second call.
                "X-Audit-Score": str(scores["overall"]),
                "X-Audit-Grade": scores["grade"],
                "X-Audit-Findings": str(len(results["findings"])),
                "X-Audit-Host": host,
                "Access-Control-Expose-Headers":
                    "X-Audit-Score, X-Audit-Grade, X-Audit-Findings, X-Audit-Host",
            },
        )
    except HTTPException:
        raise
    except BlockedError as exc:
        log.info("blocked by %s", safe.hostname)
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - one bad page must not kill the instance
        log.exception("audit failed for %s", safe.hostname)
        raise HTTPException(502, f"Could not audit that page: {exc}") from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
