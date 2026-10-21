"""HTTP front end for the Care Desk. Runs as its own Cloud Run service.

It is kept apart from the auditors on purpose: they handle one request at a
time and an audit can take minutes, so GHL's webhooks and staff clicks would
queue behind them.

Sign-in is the Redefine App's: the same Google client, team list and session
secret (service/auth.py, service/team.py), so whoever can use the app can use
the desk, and removing someone on the app's team page removes them here too.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import html
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from service import auth as team_auth
from service import email_login
from service.firestore_db import StoreUnavailable

from . import auth, intake
from .desk import Desk, DeskError, NotFound
from .store import FirestoreStore, MemoryStore

log = logging.getLogger("care-desk")
logging.basicConfig(level=logging.INFO)

STATIC = Path(__file__).parent / "static"


class GoogleCredential(BaseModel):
    credential: str = ""
    next: str = "/desk"


class EmailCode(BaseModel):
    email: str = ""
    code: str = ""
    next: str = "/desk"


class NewMessage(BaseModel):
    text: str = ""
    channel: str = "other"
    author: str = ""
    email: str = ""


class Import(BaseModel):
    rows: list[dict] = []
    dry_run: bool = True


class JourneyStatus(BaseModel):
    status: str = ""


class Step(BaseModel):
    subject: str = ""
    body: str = ""


class Reply(BaseModel):
    text: str = ""


class Pick(BaseModel):
    comment_id: str = ""


class NoteFile(BaseModel):
    name: str = ""
    type: str = ""
    data: str = ""  # base64, so a note and its files arrive in one request


class NewNote(BaseModel):
    text: str = ""
    files: list[NoteFile] = []


# Opened in the browser; anything else downloads. Never HTML or SVG, which could run script on this site.
SHOW_INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}


def dev_email() -> str:
    """Local runs only: with sign-in off, DESK_DEV_EMAIL acts as the signed-in person."""
    return os.environ.get("DESK_DEV_EMAIL", "").strip().lower() if team_auth.mode() == "off" else ""


def signed_in(request: Request) -> str | None:
    if team_auth.mode() == "on":
        return team_auth.read_session(request.cookies.get(team_auth.COOKIE), team_auth.config())
    return dev_email() or None


def not_set_up() -> str | None:
    """Why nobody can sign in, or None. The desk holds customer data, so it never runs open."""
    state = team_auth.mode()
    if state == "broken":
        return f"Sign-in is not fully set up: {', '.join(team_auth.missing_settings())} missing on the server."
    if state == "off" and not dev_email():
        return "Sign-in is not set up: set GOOGLE_CLIENT_ID, ALLOWED_EMAILS and SESSION_SECRET (see CARE_DESK.md)."
    return None


def create_app(desk: Desk | None = None) -> FastAPI:
    app = FastAPI(title="Care Desk", docs_url=None, redoc_url=None)
    # DESK_STORE=memory is for trying it on a laptop: nothing is saved past a restart.
    app.state.desk = desk or Desk(MemoryStore() if os.environ.get("DESK_STORE") == "memory" else FirestoreStore())

    def the_desk() -> Desk:
        return app.state.desk

    @app.exception_handler(StoreUnavailable)
    async def no_firestore(_request: Request, exc: StoreUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    def staff(request: Request, mutating: bool = False) -> str:
        problem = not_set_up()
        if problem:
            raise HTTPException(503, problem)
        email = signed_in(request)
        if not email:
            raise HTTPException(401, "Your session has ended. Refresh the page and sign in again.")
        # A custom header cannot be sent cross-site without a CORS preflight,
        # which this app never grants: together with SameSite cookies it stops CSRF.
        if mutating and request.headers.get("x-requested-with") != "desk":
            raise HTTPException(403, "Missing request header.")
        return email

    def run(fn, *args):
        try:
            return fn(*args)
        except DeskError as exc:
            raise HTTPException(400, str(exc)) from exc
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    # ------------------------------------------------------------ pages

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/desk", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        problem = not_set_up()
        if problem:
            return HTMLResponse(_note("Sign-in is not set up", problem), 503)
        if signed_in(request):
            return RedirectResponse("/desk", status_code=303)
        page = (STATIC / "login.html").read_text(encoding="utf-8")
        client_id = html.escape(team_auth.config().client_id, quote=True)
        page = page.replace("__GOOGLE_CLIENT_ID__", client_id)
        page = page.replace("__EMAIL_LOGIN__", "on" if email_login.available() else "off")
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/desk", response_class=HTMLResponse)
    def desk_page(request: Request) -> Response:
        problem = not_set_up()
        if problem:
            return HTMLResponse(_note("Sign-in is not set up", problem), 503)
        if not signed_in(request):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse((STATIC / "desk.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    # The Redefine App's logo and icons, copied from service/static.
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/favicon.ico")
    def favicon() -> FileResponse:
        return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------ sign in (same flow as the app)

    @app.post("/auth/google")
    def google_sign_in(payload: GoogleCredential, request: Request) -> JSONResponse:
        if team_auth.mode() != "on":
            raise HTTPException(503, "Sign-in is not set up on this server.")
        try:
            email = team_auth.verify_google_credential(payload.credential, team_auth.config())
        except team_auth.NotAllowed as exc:
            raise HTTPException(403, str(exc)) from exc
        return signed_in_response(email, payload.next, request)

    def signed_in_response(email: str, next_path: str, request: Request) -> JSONResponse:
        log.info("signed in: %s", email)
        response = JSONResponse({"ok": True, "email": email, "next": team_auth.safe_next(next_path)})
        response.set_cookie(
            team_auth.COOKIE, team_auth.make_session(email, team_auth.config().secret),
            max_age=team_auth.SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/",
            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        )
        return response

    def email_login_on() -> None:
        if team_auth.mode() != "on":
            raise HTTPException(503, "Sign-in is not set up on this server.")
        if not email_login.available():
            raise HTTPException(503, "Email sign-in is not set up on this server. Use Google.")

    @app.post("/auth/email/start")
    def email_code_start(payload: EmailCode, request: Request) -> JSONResponse:
        email_login_on()
        try:
            email_login.request_code(payload.email, team_auth.config().secret, app="Care Desk",
                                     logo_url=email_login.logo_for(request))
        except email_login.LoginError as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.post("/auth/email/verify")
    def email_code_verify(payload: EmailCode, request: Request) -> JSONResponse:
        email_login_on()
        try:
            email = email_login.verify_code(payload.email, payload.code, team_auth.config().secret)
        except email_login.LoginError as exc:
            raise HTTPException(403, str(exc)) from exc
        return signed_in_response(email, payload.next, request)

    @app.api_route("/auth/logout", methods=["GET", "POST"])
    def sign_out() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(team_auth.COOKIE, path="/")
        return response

    # ------------------------------------------------------------ desk API

    @app.get("/api/desk/state")
    def state(request: Request, since: int = 0) -> JSONResponse:
        email = staff(request)
        return JSONResponse({**the_desk().state(since), "user": {"email": email}},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/desk/messages")
    def add_message(payload: NewMessage, request: Request) -> JSONResponse:
        staff(request, mutating=True)
        if not payload.text.strip():
            raise HTTPException(400, "Paste the message text first.")
        return JSONResponse(the_desk().ingest(intake.manual(payload.text, payload.channel, payload.author, payload.email)))

    # These two come before the action route below, which would otherwise take "send" and "pick" as actions.
    @app.post("/api/desk/messages/{message_id}/send")
    def send_reply(message_id: int, payload: Reply, request: Request) -> JSONResponse:
        run(the_desk().send_reply, message_id, payload.text, staff(request, mutating=True))
        return JSONResponse({"ok": True})

    @app.post("/api/desk/messages/{message_id}/pick")
    def pick_comment(message_id: int, payload: Pick, request: Request) -> JSONResponse:
        staff(request, mutating=True)
        run(the_desk().pick_comment, message_id, payload.comment_id)
        return JSONResponse({"ok": True})

    @app.post("/api/desk/messages/{message_id}/{action}")
    def message_action(message_id: int, action: str, request: Request) -> JSONResponse:
        run(the_desk().message_action, message_id, action, staff(request, mutating=True))
        return JSONResponse({"ok": True})

    # Before the action route below, which would otherwise take "notes" as an action.
    @app.post("/api/desk/tickets/{ticket_id}/notes")
    def add_note(ticket_id: int, payload: NewNote, request: Request) -> JSONResponse:
        email = staff(request, mutating=True)
        files = []
        for f in payload.files:
            try:
                files.append((f.name, f.type, base64.b64decode(f.data, validate=True)))
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(400, f"{f.name or 'A file'} could not be read. Attach it again.") from exc
        return JSONResponse({"id": run(the_desk().add_note, ticket_id, email, payload.text, files)})

    @app.get("/api/desk/files/{file_id}")
    def get_file(file_id: int, request: Request) -> Response:
        staff(request)
        meta, data = run(the_desk().file, file_id)
        inline = meta["type"] in SHOW_INLINE
        disposition = "inline" if inline else "attachment"
        return Response(data, media_type=meta["type"] if inline else "application/octet-stream", headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(meta['name'])}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, max-age=3600",
        })

    @app.post("/api/desk/tickets/{ticket_id}/{action}")
    def ticket_action(ticket_id: int, action: str, request: Request) -> JSONResponse:
        run(the_desk().ticket_action, ticket_id, action, staff(request, mutating=True))
        return JSONResponse({"ok": True})

    @app.post("/api/desk/leads/{lead_id}/{status}")
    def lead_action(lead_id: int, status: str, request: Request) -> JSONResponse:
        run(the_desk().lead_action, lead_id, status, staff(request, mutating=True))
        return JSONResponse({"ok": True})

    @app.post("/api/desk/import")
    def import_leads(payload: Import, request: Request) -> JSONResponse:
        email = staff(request, mutating=True)
        if payload.dry_run:
            return JSONResponse({"rows": the_desk().check_import(payload.rows)})
        return JSONResponse(the_desk().import_leads(payload.rows, email))

    @app.post("/api/desk/journeys/{key}/status")
    def journey_status(key: str, payload: JourneyStatus, request: Request) -> JSONResponse:
        email = staff(request, mutating=True)
        return JSONResponse({"released": run(the_desk().set_journey_status, key, payload.status, email)})

    @app.post("/api/desk/journeys/{key}/steps/{index}")
    def journey_step(key: str, index: int, payload: Step, request: Request) -> JSONResponse:
        staff(request, mutating=True)
        run(the_desk().save_journey_step, key, index, payload.subject, payload.body)
        return JSONResponse({"ok": True})

    @app.post("/api/desk/tick")
    def tick(request: Request) -> JSONResponse:
        # Cloud Scheduler calls this with the cron key; an open desk page calls it with a session.
        key = os.environ.get("DESK_CRON_KEY", "")
        given = request.headers.get("x-cron-key", "")
        if not (key and hmac.compare_digest(given, key)):
            staff(request, mutating=True)
        return JSONResponse(the_desk().tick())

    # ------------------------------------------------------------ GHL webhook

    @app.post("/hooks/ghl")
    async def ghl_hook(request: Request) -> JSONResponse:
        key = os.environ.get("DESK_WEBHOOK_KEY", "")
        if not key:
            raise HTTPException(503, "The webhook is not set up: DESK_WEBHOOK_KEY is missing on the server.")
        given = request.query_params.get("key") or request.headers.get("x-desk-key") or ""
        if not hmac.compare_digest(given, key):
            raise HTTPException(401, "Wrong webhook key.")
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "Send JSON.") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "Send a JSON object.")
        started = time.time()
        result = the_desk().ingest(intake.parse_ghl(payload))
        log.info("ghl webhook: message %s%s in %.1fs", result["id"], " (duplicate)" if result["duplicate"] else "",
                 time.time() - started)
        # Always 200 once stored: a sorting failure is retried here, not by GHL.
        return JSONResponse({"ok": True, **result})

    # ------------------------------------------------------------ unsubscribe

    @app.get("/unsubscribe", response_class=HTMLResponse)
    def unsubscribe(t: str = "") -> HTMLResponse:
        email = auth.unsign(t, "unsubscribe")
        if not email:
            return HTMLResponse(_note("This link is not valid", "Reply to any of our emails and we will take you off the list."), 400)
        the_desk().unsubscribe(email)
        return HTMLResponse(_note("You are unsubscribed", f"We will not send follow-up emails to {email} again."))

    return app


def _note(title: str, text: str) -> str:
    return (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><body style="margin:0;min-height:100vh;display:grid;place-items:center;'
            f'background:#F4F3F0;font:16px/1.5 Arial,sans-serif;color:#191616;padding:16px">'
            f'<div style="max-width:420px;background:#fff;border-radius:14px;padding:28px"><h1 style="margin:0 0 8px;'
            f'font-size:22px">{html.escape(title)}</h1><p style="margin:0">{html.escape(text)}</p></div></body>')


app = create_app() if os.environ.get("DESK_AUTOSTART", "1") == "1" else None
