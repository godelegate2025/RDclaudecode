"""HTTP front end for the website audit, sized for Cloud Run.

One request runs one audit and returns the PDF bytes directly. That keeps the
service stateless: no job store, no shared disk, and no risk of a follow-up
request landing on a different instance than the one that made the report.
"""

from __future__ import annotations

import base64
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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from website_audit.analysis import analyse
from website_audit.blocking import detect as detect_block
from website_audit.blocking import explain as explain_block
from website_audit.browser import browser_session
from website_audit.collector import collect
from website_audit.draft import build_draft, draft_from_site
from website_audit.report import html_to_pdf, render_html, render_site_html, write_json
from website_audit.site import audit_site

from .security import UnsafeURL, guard_route, validate

log = logging.getLogger("website-audit")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Website design audit", docs_url=None, redoc_url=None)

AUDIT_TIMEOUT_MS = int(os.environ.get("AUDIT_TIMEOUT_MS", "45000"))
RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "30"))

# A whole-site audit takes about 6.6s per page, so 25 pages lands near three
# minutes — inside the request timeout, which is why this needs no job queue.
SITE_PAGE_LIMIT = int(os.environ.get("SITE_PAGE_LIMIT", "12"))
SITE_PAGE_MAX = int(os.environ.get("SITE_PAGE_MAX", "25"))
# One site audit is a dozen page audits' worth of work; charge it accordingly.
SITE_RATE_COST = 5

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
SERVICE_WORKER = STATIC / "sw.js"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class BlockedError(RuntimeError):
    """The page we reached was a bot wall, not the site."""


class AuditRequest(BaseModel):
    url: str = ""
    json_only: bool = False
    mode: str = "site"          # "site" audits every page found; "page" just this one
    limit: int | None = None
    draft: bool = False         # also build a corrected home page draft; the response becomes JSON


RATE_WINDOW_SECONDS = 3600


def _seconds_until_room(seen: deque[float], cost: int, now: float) -> int:
    """How long until `cost` more units fit in the window for this visitor.

    The window is rolling, so room opens when the oldest hits age out: with
    `n` hits recorded, `cost` units fit once the (n - limit + cost)th oldest
    hit is more than an hour old.
    """
    needed = len(seen) + cost - RATE_LIMIT_PER_HOUR
    if needed <= 0:
        return 0
    if cost > RATE_LIMIT_PER_HOUR:
        return RATE_WINDOW_SECONDS  # never fits; still give the caller a number
    frees_at = seen[needed - 1] + RATE_WINDOW_SECONDS
    return max(1, int(frees_at - now + 0.999))


def rate_limited(client_ip: str, cost: int = 1) -> bool:
    """Charge `cost` units to this visitor, or refuse if they would exceed the hour's budget."""
    return seconds_until_allowed(client_ip, cost) > 0


def seconds_until_allowed(client_ip: str, cost: int = 1) -> int:
    """Charge `cost` units and return 0, or return how many seconds until they would fit."""
    now = time.time()
    cutoff = now - RATE_WINDOW_SECONDS
    with _hits_lock:
        seen = _hits.setdefault(client_ip, deque())
        while seen and seen[0] < cutoff:
            seen.popleft()
        wait = _seconds_until_room(seen, cost, now)
        if wait:
            return wait
        for _ in range(cost):
            seen.append(now)
        if len(_hits) > 10_000:  # bound the dict on a long-lived instance
            for key in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
                _hits.pop(key, None)
    return 0


def retry_wait(client_ip: str, cost: int = 1) -> int:
    """Seconds until `cost` units would fit, without charging anything."""
    now = time.time()
    with _hits_lock:
        seen = _hits.get(client_ip) or deque()
        live = deque(t for t in seen if t >= now - RATE_WINDOW_SECONDS)
        return _seconds_until_room(live, cost, now)


def refund(client_ip: str, cost: int) -> None:
    """Give back units charged for an audit that never produced a report."""
    with _hits_lock:
        seen = _hits.get(client_ip)
        if not seen:
            return
        for _ in range(min(cost, len(seen))):
            seen.pop()
        if not seen:
            _hits.pop(client_ip, None)


def _minutes(seconds: int) -> str:
    minutes = max(1, -(-seconds // 60))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def rate_limit_response(ip: str, whole_site: bool, wait: int) -> HTTPException:
    """A 429 that says when the next audit can run, and whether a cheaper one fits now."""
    if whole_site:
        page_wait = retry_wait(ip, 1)
        detail = f"Rate limit reached. You can run a whole-site audit again in {_minutes(wait)}"
        detail += ", or a single-page audit now." if page_wait == 0 else \
                  f", or a single-page audit in {_minutes(page_wait)}."
    else:
        detail = f"Rate limit reached. You can run another audit in {_minutes(wait)}."
    return HTTPException(429, detail, headers={"Retry-After": str(wait)})


def client_ip(request: Request) -> str:
    # Cloud Run appends the caller to X-Forwarded-For; the first entry is the
    # original client. Trusted because only the load balancer can set it here.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def run_site_audit(target: str, work_dir: Path, limit: int):
    """Crawl and compare. Returns (pdf_path, audit)."""
    audit = audit_site(
        target,
        work_dir,
        limit=limit,
        timeout_ms=AUDIT_TIMEOUT_MS,
        progress=lambda message: log.info("  %s", message),
    )
    pdf_path = work_dir / "report.pdf"
    html_to_pdf(render_site_html(audit), pdf_path, work_dir)
    return pdf_path, audit


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


@app.get("/sw.js")
def service_worker() -> Response:
    # Served from the root so its scope covers /reports/. A worker's scope can
    # never be wider than the path it was loaded from, so /static/sw.js would not do.
    return Response(
        content=SERVICE_WORKER.read_bytes(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/reports/{name}")
def report(name: str) -> PlainTextResponse:
    # Reports live only in the browser that ran the audit (see sw.js); the
    # server keeps nothing. This answers a link opened elsewhere honestly.
    return PlainTextResponse(
        "Reports are not stored on the server. Run the audit again to get a fresh copy.",
        status_code=404,
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.post("/api/audit")
def audit(payload: AuditRequest, request: Request) -> Response:
    requested = payload.url
    want_json = payload.json_only
    whole_site = payload.mode != "page"
    requested_limit = SITE_PAGE_LIMIT if payload.limit is None else payload.limit
    limit = max(1, min(requested_limit, SITE_PAGE_MAX))

    try:
        safe = validate(requested)
    except UnsafeURL as exc:
        raise HTTPException(400, str(exc)) from exc

    ip = client_ip(request)
    cost = SITE_RATE_COST if whole_site else 1
    wait = seconds_until_allowed(ip, cost)
    if wait:
        raise rate_limit_response(ip, whole_site, wait)

    work_dir = Path(tempfile.mkdtemp(prefix="audit-"))
    started = time.time()
    delivered = False  # anything short of a report refunds the units charged above
    try:
        # Long enough to queue behind one whole-site audit rather than 503.
        if not _audit_lock.acquire(timeout=360):
            raise HTTPException(503, "The auditor is busy with another site. Try again shortly.")
        try:
            if whole_site:
                pdf_path, audit = run_site_audit(safe.url, work_dir, limit)
                scores = audit.scores
                findings = audit.findings
                host = audit.host
                pages = len(audit.audited)
                data = results = None
            else:
                pdf_path, data, results = run_audit(safe.url, work_dir)
                scores = results["scores"]
                findings = results["findings"]
                host = urlparse(data.final_url).netloc or safe.hostname
                pages = 1
        finally:
            _audit_lock.release()

        log.info(
            "audited %s (%s, %s page(s)) in %.1fs — score %s, %s findings",
            safe.hostname, payload.mode, pages, time.time() - started,
            scores["overall"], len(findings),
        )

        if want_json:
            if data is None:
                raise HTTPException(400, "JSON output is only available for a single page.")
            json_path = write_json(data, results, work_dir / "report.json")
            delivered = True
            return Response(content=json_path.read_bytes(), media_type="application/json")

        suffix = "site-audit" if whole_site else "audit"
        filename = f"{host.replace(':', '-')}-{suffix}.pdf"

        if payload.draft:
            # One request, two deliverables: the PDF rides along base64-encoded so
            # the draft never needs a second crawl or anything kept on the server.
            draft = draft_from_site(audit) if data is None else build_draft(data.final_url, data.probe, results)
            delivered = True
            return JSONResponse({
                "host": host,
                "score": scores["overall"],
                "grade": scores["grade"],
                "findings": len(findings),
                "pages": pages,
                "report": {"filename": filename, "pdf_base64": base64.b64encode(pdf_path.read_bytes()).decode()},
                "draft": None if draft is None else {
                    "filename": draft.filename,
                    "html": draft.html,
                    "notes": draft.notes,
                    "unresolved": draft.unresolved,
                    "palette": draft.palette,
                    "fonts": draft.fonts,
                },
            })

        delivered = True
        return Response(
            content=pdf_path.read_bytes(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                # The page reads these to render the summary without a second call.
                "X-Audit-Score": str(scores["overall"]),
                "X-Audit-Grade": scores["grade"],
                "X-Audit-Findings": str(len(findings)),
                "X-Audit-Host": host,
                "X-Audit-Pages": str(pages),
                "Access-Control-Expose-Headers":
                    "X-Audit-Score, X-Audit-Grade, X-Audit-Findings, X-Audit-Host, X-Audit-Pages",
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
        if not delivered:
            refund(ip, cost)
        shutil.rmtree(work_dir, ignore_errors=True)
