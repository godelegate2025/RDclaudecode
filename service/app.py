"""HTTP front end for the website audit, sized for Cloud Run.

One request runs one audit and returns the PDF bytes directly. That keeps the
service stateless: no job store, no shared disk, and no risk of a follow-up
request landing on a different instance than the one that made the report.
"""

from __future__ import annotations

import base64
import html
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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from post_audit.analysis import AnalysisError
from post_audit.apify import ScrapeError
from post_audit.media import MediaError
from post_audit.pipeline import NotEnoughToAudit, audit_post
from post_audit.platforms import UnsupportedURL
from website_audit.analysis import analyse
from website_audit.blocking import detect as detect_block
from website_audit.blocking import explain as explain_block
from website_audit.browser import browser_session
from website_audit.brief import build_brief
from website_audit.collector import collect
from website_audit.report import html_to_pdf, render_html, render_site_html, write_json
from website_audit.site import audit_site

from . import auth
from .team import StoreUnavailable, TeamError, get_team
from .brief_view import render_brief_html
from .post_pdf import attach_pdf
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

# A post audit spends money on Apify and Claude, so it costs more of the hourly
# budget than a single page. Keys come from the environment, never the repo.
POST_RATE_COST = int(os.environ.get("POST_RATE_COST", "3"))
_post_lock = threading.Lock()

# Chromium is not thread-safe and each instance is sized for one audit at a
# time; Cloud Run's --concurrency 1 enforces this too, and the lock makes it
# true regardless of how the service is run.
_audit_lock = threading.Lock()

STATIC = Path(__file__).parent / "static"
HOME = STATIC / "home.html"
INDEX = STATIC / "index.html"
POST_PAGE = STATIC / "post.html"
SERVICE_WORKER = STATIC / "sw.js"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
LOGIN_PAGE = STATIC / "login.html"
TEAM_PAGE = STATIC / "team.html"


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    """Every page and API call needs a signed-in team member, once sign-in is set up."""
    state = auth.mode()
    path = request.url.path
    if state == "off" or auth.is_public(path):
        return await call_next(request)
    is_api = path.startswith("/api/")
    if state == "broken":
        # Half-configured must not mean open: refuse until all settings exist.
        detail = f"Sign-in is not fully set up: {', '.join(auth.missing_settings())} missing on the server."
        log.error(detail)
        return JSONResponse({"detail": detail}, status_code=503) if is_api else PlainTextResponse(detail, status_code=503)
    email = auth.read_session(request.cookies.get(auth.COOKIE), auth.config())
    if not email:
        if is_api:
            return JSONResponse({"detail": "Your session has ended. Refresh the page and sign in again."}, status_code=401)
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(auth.login_url(target), status_code=303)
    request.state.user = email
    return await call_next(request)


class BlockedError(RuntimeError):
    """The page we reached was a bot wall, not the site."""


class PostAuditRequest(BaseModel):
    url: str = ""


class AuditRequest(BaseModel):
    url: str = ""
    json_only: bool = False
    mode: str = "site"          # "site" audits every page found; "page" just this one
    limit: int | None = None
    brief: bool = False         # site mode only: also return the build brief; the response becomes JSON


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


class GoogleCredential(BaseModel):
    credential: str = ""
    next: str = "/"


def _secure_cookie(request: Request) -> bool:
    # Cloud Run terminates TLS in front of the app and says so in this header.
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> Response:
    if auth.mode() == "off":
        return RedirectResponse("/", status_code=303)
    if auth.mode() == "on" and auth.read_session(request.cookies.get(auth.COOKIE), auth.config()):
        return RedirectResponse(auth.safe_next(next), status_code=303)
    page = LOGIN_PAGE.read_text(encoding="utf-8")
    client_id = html.escape(auth.config().client_id, quote=True)
    return HTMLResponse(page.replace("__GOOGLE_CLIENT_ID__", client_id), headers={"Cache-Control": "no-store"})


@app.post("/auth/google")
def google_sign_in(payload: GoogleCredential, request: Request) -> JSONResponse:
    if auth.mode() != "on":
        raise HTTPException(503, "Sign-in is not set up on this server.")
    try:
        email = auth.verify_google_credential(payload.credential, auth.config())
    except auth.NotAllowed as exc:
        raise HTTPException(403, str(exc)) from exc
    log.info("signed in: %s", email)
    response = JSONResponse({"ok": True, "email": email, "next": auth.safe_next(payload.next)})
    response.set_cookie(
        auth.COOKIE,
        auth.make_session(email, auth.config().secret),
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,                    # page scripts can't read it
        secure=_secure_cookie(request),
        samesite="lax",                   # not sent on other sites' requests
        path="/",
    )
    return response


@app.api_route("/auth/logout", methods=["GET", "POST"])
def sign_out() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.get("/auth/me")
def who_am_i(request: Request) -> JSONResponse:
    email = auth.read_session(request.cookies.get(auth.COOKIE), auth.config()) if auth.mode() == "on" else None
    return JSONResponse({"signed_in": bool(email), "email": email, "sign_in": auth.mode(),
                         "can_manage_team": bool(email) and get_team().can_manage(email)},
                        headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ team admin

class TeamChange(BaseModel):
    email: str = ""
    role: str = "member"


def _team_admin(request: Request) -> str:
    """The signed-in owner or admin making this request, or a 403."""
    if auth.mode() != "on":
        raise HTTPException(409, "Turn on team sign-in first (see DEPLOY.md, Team sign-in).")
    email = getattr(request.state, "user", None)
    if not email or not get_team().can_manage(email):
        raise HTTPException(403, "Only owners and admins can manage the team.")
    return email


@app.get("/admin/team", response_class=HTMLResponse)
def team_page(request: Request) -> Response:
    if auth.mode() != "on":
        return RedirectResponse("/", status_code=303)
    if not get_team().can_manage(getattr(request.state, "user", "")):
        return PlainTextResponse("Only owners and admins can manage the team.", status_code=403)
    return HTMLResponse(TEAM_PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/api/team")
def team_list(request: Request) -> JSONResponse:
    me = _team_admin(request)
    team = get_team()
    try:
        people = [m.to_dict() for m in team.listing()]
        ready, problem = True, ""
    except StoreUnavailable as exc:
        people = [{"email": e, "role": "owner", "owner": True, "added_by": "Server setting", "added_at": ""}
                  for e in team.owners()]
        ready, problem = False, str(exc)
    return JSONResponse({"me": me, "me_role": team.role_of(me), "members": people,
                         "store_ready": ready, "problem": problem},
                        headers={"Cache-Control": "no-store"})


@app.post("/api/team")
def team_add(change: TeamChange, request: Request) -> JSONResponse:
    me = _team_admin(request)
    # Admins can bring people in; making someone an admin is the owners' call.
    if change.role == "admin" and get_team().role_of(me) != "owner":
        raise HTTPException(403, "Only owners can add admins. Add them as a member and ask an owner to promote them.")
    try:
        member = get_team().add(change.email, change.role, by=me)
    except StoreUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except TeamError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("team: %s added %s as %s", me, member.email, member.role)
    return JSONResponse(member.to_dict())


class RoleChange(BaseModel):
    role: str = ""


@app.patch("/api/team/{email}")
def team_set_role(email: str, change: RoleChange, request: Request) -> JSONResponse:
    me = _team_admin(request)
    # Admins run the team day to day; deciding who else becomes an admin is
    # the owners' call.
    if get_team().role_of(me) != "owner":
        raise HTTPException(403, "Only owners can change someone's role.")
    try:
        member = get_team().set_role(email, change.role, by=me)
    except StoreUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except TeamError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("team: %s made %s %s", me, member.email, member.role)
    return JSONResponse(member.to_dict())


@app.delete("/api/team/{email}")
def team_remove(email: str, request: Request) -> JSONResponse:
    me = _team_admin(request)
    # Admins can remove members; removing an admin is the owners' call.
    if get_team().role_of(email) == "admin" and get_team().role_of(me) != "owner":
        raise HTTPException(403, "Only owners can remove admins.")
    try:
        get_team().remove(email, by=me)
    except StoreUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except TeamError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("team: %s removed %s", me, email.lower())
    return JSONResponse({"ok": True})


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse(HOME.read_text(encoding="utf-8"))


@app.get("/website-audit", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX.read_text(encoding="utf-8"))


@app.get("/post-audit", response_class=HTMLResponse)
def post_page() -> HTMLResponse:
    return HTMLResponse(POST_PAGE.read_text(encoding="utf-8"))


@app.get("/favicon.ico")
def favicon() -> Response:
    # Browsers ask for this at the root whatever the page's <link> says.
    return Response(
        content=(STATIC / "favicon.ico").read_bytes(),
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


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

        if payload.brief and whole_site:
            # One crawl, two deliverables: the PDF rides along base64-encoded next
            # to the Markdown brief, so nothing is kept on the server.
            brief = build_brief(audit)
            return JSONResponse({
                "host": host,
                "score": scores["overall"],
                "grade": scores["grade"],
                "findings": len(findings),
                "pages": pages,
                "report": {"filename": filename, "pdf_base64": base64.b64encode(pdf_path.read_bytes()).decode()},
                "brief": {
                    "filename": brief.filename,
                    "markdown": brief.markdown,
                    # The preview shown in the page; the Markdown is the deliverable.
                    "html": render_brief_html(brief.markdown, f"Build brief — {host}"),
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


def _missing_post_config() -> list[str]:
    return [name for name in ("APIFY_TOKEN", "ANTHROPIC_API_KEY") if not os.environ.get(name)]


@app.post("/api/post-audit")
def post_audit(payload: PostAuditRequest, request: Request) -> JSONResponse:
    missing = _missing_post_config()
    if missing:
        raise HTTPException(503, f"The Post Auditor is not set up yet: {', '.join(missing)} missing on the server.")

    ip = client_ip(request)
    wait = seconds_until_allowed(ip, POST_RATE_COST)
    if wait:
        raise HTTPException(429, f"Rate limit reached. You can audit another post in {_minutes(wait)}.",
                            headers={"Retry-After": str(wait)})

    work_dir = Path(tempfile.mkdtemp(prefix="post-audit-"))
    delivered = False  # anything short of a report refunds the units charged above
    try:
        if not _post_lock.acquire(timeout=300):
            raise HTTPException(503, "The auditor is busy with another post. Try again shortly.")
        try:
            report = audit_post(
                payload.url,
                work_dir,
                apify_token=os.environ["APIFY_TOKEN"],
                check=validate,
            )
            try:
                attach_pdf(report)
            except Exception:  # noqa: BLE001 - the report on screen matters more than the file
                log.exception("post audit PDF failed; the page falls back to printing")
        finally:
            _post_lock.release()
        delivered = True
        return JSONResponse(report)
    except HTTPException:
        raise
    except UnsupportedURL as exc:
        raise HTTPException(400, str(exc)) from exc
    except NotEnoughToAudit as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ScrapeError, MediaError, AnalysisError) as exc:
        log.info("post audit failed: %s", exc)
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - one bad post must not kill the instance
        log.exception("post audit failed")
        raise HTTPException(502, "Could not audit that post. Try again, or try another post.") from exc
    finally:
        if not delivered:
            refund(ip, POST_RATE_COST)
        shutil.rmtree(work_dir, ignore_errors=True)
