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
import re
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from service import auth as team_auth
from service import email_login
from service import profiles
from service import team as app_team
from service.profiles import NameError_, get_profiles
from service.firestore_db import StoreUnavailable

from . import auth, intake
from .desk import LOAD, Desk, DeskError, NotFound
from .store import FirestoreStore, MemoryStore, SupabaseStore
from .team import DeskTeam, TeamError, invite_message

log = logging.getLogger("care-desk")
logging.basicConfig(level=logging.INFO)

STATIC = Path(__file__).parent / "static"
# The React desk (care_desk/web), built by `npm run build` or by Dockerfile.desk.
WEB = Path(__file__).parent / "web" / "dist"
# The desk's views, each at its own address; care_desk/web/src/App.tsx has the same list.
DESK_VIEWS = ("/", "/inbox", "/tickets", "/contacts", "/contacts/{lead_id}", "/email", "/email/{journey}", "/team")


class GoogleCredential(BaseModel):
    credential: str = ""
    next: str = "/"


class EmailCode(BaseModel):
    email: str = ""
    code: str = ""
    next: str = "/"


class NewMessage(BaseModel):
    text: str = ""
    channel: str = "other"
    author: str = ""
    email: str = ""


class Import(BaseModel):
    rows: list[dict] = []
    dry_run: bool = True


class OtherContact(BaseModel):
    other: int = 0


class BulkContacts(BaseModel):
    action: str = ""            # tag, untag or delete
    ids: list[int] = []
    tags: list[str] = []


class JourneyStatus(BaseModel):
    status: str = ""


class Step(BaseModel):
    subject: str = ""
    body: str = ""


class Reply(BaseModel):
    text: str = ""


class ContactEmail(BaseModel):
    ids: list[int] = []
    subject: str = ""
    body: str = ""


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


NOT_ON_TEAM = "{email} isn't on the Care Desk team. Ask a desk admin to add it on the desk's Team page."


class NewName(BaseModel):
    name: str = ""


class TeamChange(BaseModel):
    email: str = ""
    role: str = "agent"


def dev_email() -> str:
    """Local runs only: with sign-in off, DESK_DEV_EMAIL acts as the signed-in person."""
    return os.environ.get("DESK_DEV_EMAIL", "").strip().lower() if team_auth.mode() == "off" else ""


def signed_in(request: Request, team: DeskTeam) -> str | None:
    """The signed-in email, if it is on the desk's own team (not the app's)."""
    if team_auth.mode() == "on":
        return team_auth.read_session(request.cookies.get(team_auth.COOKIE), team_auth.config(),
                                      allowed=team.is_allowed)
    return dev_email() or None


def not_set_up() -> str | None:
    """Why nobody can sign in, or None. The desk holds customer data, so it never runs open."""
    state = team_auth.mode()
    if state == "broken":
        return f"Sign-in is not fully set up: {', '.join(team_auth.missing_settings())} missing on the server."
    if state == "off" and not dev_email():
        return "Sign-in is not set up: set GOOGLE_CLIENT_ID, ALLOWED_EMAILS and SESSION_SECRET (see CARE_DESK.md)."
    return None


def _store():
    # DESK_STORE=memory is for trying it on a laptop: nothing is saved past a restart.
    if os.environ.get("DESK_STORE") == "memory":
        return MemoryStore()
    url = os.environ.get("SUPABASE_DB_URL", "").strip()
    return SupabaseStore(url) if url else FirestoreStore()


def create_app(desk: Desk | None = None) -> FastAPI:
    app = FastAPI(title="Care Desk", docs_url=None, redoc_url=None)
    app.state.desk = desk or Desk(_store())
    web = WEB
    if os.environ.get("DESK_STORE") == "memory":
        app_team.use_store(app_team.MemoryStore())  # nor the app's team list, which the desk team is filled from
        profiles.use_store(profiles.MemoryStore())

    app.state.team = DeskTeam(app.state.desk.db)

    def the_desk() -> Desk:
        return app.state.desk

    def team() -> DeskTeam:
        return app.state.team

    def role_of(email: str) -> str | None:
        return "owner" if dev_email() else team().role_of(email)

    @app.exception_handler(StoreUnavailable)
    async def no_firestore(_request: Request, exc: StoreUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    def staff(request: Request, mutating: bool = False, admin: bool = False) -> str:
        problem = not_set_up()
        if problem:
            raise HTTPException(503, problem)
        email = signed_in(request, team())
        if not email:
            raise HTTPException(401, "Your session has ended. Refresh the page and sign in again.")
        if admin and role_of(email) not in ("owner", "admin"):
            raise HTTPException(403, "Only Care Desk admins can manage the team.")
        if not dev_email():
            team().mark_joined(email)  # turns an invite from pending to approved
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

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        problem = not_set_up()
        if problem:
            return HTMLResponse(_note("Sign-in is not set up", problem), 503)
        if signed_in(request, team()):
            return RedirectResponse(team_auth.safe_next(request.query_params.get("next")), status_code=303)
        page = (STATIC / "login.html").read_text(encoding="utf-8")
        client_id = html.escape(team_auth.config().client_id, quote=True)
        page = page.replace("__GOOGLE_CLIENT_ID__", client_id)
        page = page.replace("__EMAIL_LOGIN__", "on" if email_login.available() else "off")
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    # The desk is a React app (care_desk/web), built into web/dist. Each view has its own
    # address and they all get the same page; the page shows the view the address names.
    def desk_page(request: Request) -> Response:
        problem = not_set_up()
        if problem:
            return HTMLResponse(_note("Sign-in is not set up", problem), 503)
        here = request.url.path
        email = signed_in(request, team())
        if not email:  # back to this view once signed in
            return RedirectResponse(team_auth.login_url(here), status_code=303)
        if get_profiles().needs_name(email):  # first visit: ask for a name once
            return RedirectResponse("/welcome?next=" + quote(team_auth.safe_next(here), safe="/"), status_code=303)
        page = web / "index.html"
        if not page.is_file():
            return HTMLResponse(_note("The desk isn't built", "Run npm run build in care_desk/web, "
                                      "or deploy with deploy-desk.ps1, which builds it."), 404)
        return HTMLResponse(page.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    for path in DESK_VIEWS:
        app.add_api_route(path, desk_page, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)

    # The desk's address before its views had their own, so bookmarks of it still open the desk.
    @app.get("/desk")
    def old_desk_address() -> RedirectResponse:
        return RedirectResponse("/", status_code=301)

    # Its scripts and styles, named by content hash so a new build never mixes with an old one.
    # No desk data is in them, so they need no sign-in, like /static.
    app.mount("/assets", StaticFiles(directory=web / "assets", check_dir=False), name="desk-assets")

    @app.get("/welcome", response_class=HTMLResponse)
    def welcome_page(request: Request) -> Response:
        problem = not_set_up()
        if problem:
            return HTMLResponse(_note("Sign-in is not set up", problem), 503)
        email = signed_in(request, team())
        if not email:
            return RedirectResponse("/login", status_code=303)
        name = get_profiles().name_of(email) or ""
        page = (STATIC / "welcome.html").read_text(encoding="utf-8")
        page = (page.replace("__NAME__", html.escape(name, quote=True)).replace("__EMAIL__", html.escape(email))
                .replace("__EDIT__", "on" if name else "off"))
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.post("/auth/name")
    def save_name(payload: NewName, request: Request) -> JSONResponse:
        email = staff(request, mutating=True)
        try:
            name = get_profiles().set_name(email, payload.name)
        except NameError_ as exc:
            raise HTTPException(400, str(exc)) from exc
        except StoreUnavailable as exc:
            raise HTTPException(503, "Your name could not be saved right now. Try again in a minute.") from exc
        log.info("%s set their name", email)
        return JSONResponse({"ok": True, "name": name})

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
            email = team_auth.verify_google_credential(payload.credential, team_auth.config(),
                                                       allowed=team().is_allowed, refused=NOT_ON_TEAM)
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
                                     logo_url=email_login.logo_for(request), allowed=team().is_allowed)
        except email_login.LoginError as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.post("/auth/email/verify")
    def email_code_verify(payload: EmailCode, request: Request) -> JSONResponse:
        email_login_on()
        try:
            email = email_login.verify_code(payload.email, payload.code, team_auth.config().secret,
                                            allowed=team().is_allowed, refused=NOT_ON_TEAM)
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
        names = get_profiles().names()
        people = {e: names[e] for e in team().emails() if e in names}  # only the desk's own people
        return JSONResponse({**the_desk().state(since), "people": people,
                             "user": {"email": email, "role": role_of(email), "name": names.get(email)}},
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

    @app.post("/api/desk/messages/{message_id}/email")
    def email_reply(message_id: int, payload: Reply, request: Request) -> JSONResponse:
        run(the_desk().email_reply, message_id, payload.text, staff(request, mutating=True))
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

    @app.post("/api/desk/leads/email")
    def email_contacts(payload: ContactEmail, request: Request) -> JSONResponse:
        email = staff(request, mutating=True)
        result = run(the_desk().email_contacts, payload.ids[:LOAD["leads"]], payload.subject, payload.body, email)
        return JSONResponse({"ok": True, **result})

    @app.post("/api/desk/leads/bulk")
    def bulk_contacts(payload: BulkContacts, request: Request) -> JSONResponse:
        email = staff(request, mutating=True)
        ids = payload.ids[:LOAD["leads"]]  # never more than the page can show
        if not ids:
            raise HTTPException(400, "Select at least one contact.")
        if payload.action == "delete":
            if role_of(email) not in ("owner", "admin"):
                raise HTTPException(403, "Only Care Desk admins can delete contacts.")
            changed = run(the_desk().delete_leads, ids, email)
        elif payload.action in ("tag", "untag"):
            changed = run(the_desk().tag_leads, ids, payload.tags, email, payload.action == "untag")
        else:
            raise HTTPException(400, f"Unknown action: {payload.action}")
        return JSONResponse({"ok": True, "changed": changed})

    # A contact's page: their whole conversation and activity, beyond what the state loads.
    @app.get("/api/desk/leads/{lead_id}")
    def contact_thread(lead_id: int, request: Request) -> JSONResponse:
        staff(request)
        return JSONResponse(run(the_desk().contact_thread, lead_id), headers={"Cache-Control": "no-store"})

    # One person saved twice: merge the other contact into this one, or mark them as two people.
    @app.post("/api/desk/leads/{lead_id}/merge")
    def merge_contacts(lead_id: int, payload: OtherContact, request: Request) -> JSONResponse:
        run(the_desk().merge_leads, lead_id, payload.other, staff(request, mutating=True))
        return JSONResponse({"ok": True})

    @app.post("/api/desk/leads/{lead_id}/separate")
    def keep_contacts_separate(lead_id: int, payload: OtherContact, request: Request) -> JSONResponse:
        run(the_desk().keep_leads_separate, lead_id, payload.other, staff(request, mutating=True))
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

    # ------------------------------------------------------------ the desk's own team (admins only)

    def team_change(fn, *args) -> JSONResponse:
        try:
            fn(*args)
        except TeamError as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse({"people": team().listing()})

    @app.get("/api/desk/team")
    def team_list(request: Request) -> JSONResponse:
        staff(request, admin=True)
        return JSONResponse({"people": team().listing()}, headers={"Cache-Control": "no-store"})

    @app.post("/api/desk/team")
    def team_add(change: TeamChange, request: Request) -> JSONResponse:
        me = staff(request, mutating=True, admin=True)
        try:
            team().add(change.email, change.role, me)
        except TeamError as exc:
            raise HTTPException(400, str(exc)) from exc
        email = change.email.strip().lower()
        log.info("%s added %s to the desk team as %s", me, email, change.role)
        invited, note = send_invite(email, change.role, me, request)
        if note:
            note = "Added, but " + note[0].lower() + note[1:]
        return JSONResponse({"people": team().listing(), "invited": invited, "note": note})

    def send_invite(email: str, role: str, me: str, request: Request) -> tuple[bool, str | None]:
        """Email the invite and record how it went. Being on the list is what grants access;
        the invite only tells them, so a failed send is reported, never undone."""
        if not email_login.available():
            return False, "Invite emails are off on this server, so send them the desk link yourself."
        site = email_login.site_for(request)
        try:
            email_login.deliver(email, *invite_message(email, role, me, f"{site}/login", f"{site}/static/logo.png"))
        except email_login.LoginError:
            team().record_invite(email, "Could not be sent")
            return False, "The invite email could not be sent. Try again, or send them the desk link yourself."
        team().record_invite(email, None)
        log.info("%s invited %s to the desk", me, email)
        return True, None

    @app.post("/api/desk/team/{email}/invite")
    def team_resend_invite(email: str, request: Request) -> JSONResponse:
        me = staff(request, mutating=True, admin=True)
        try:
            person = team().check_invite(email)
        except TeamError as exc:
            raise HTTPException(400, str(exc)) from exc
        invited, note = send_invite(person["email"], person.get("role", "agent"), me, request)
        if not invited:
            raise HTTPException(502 if email_login.available() else 503, note)
        return JSONResponse({"people": team().listing(), "invited": True})

    @app.patch("/api/desk/team/{email}")
    def team_set_role(email: str, change: TeamChange, request: Request) -> JSONResponse:
        me = staff(request, mutating=True, admin=True)
        log.info("%s made %s a desk %s", me, email, change.role)
        return team_change(team().set_role, email, change.role, me)

    @app.delete("/api/desk/team/{email}")
    def team_remove(email: str, request: Request) -> JSONResponse:
        me = staff(request, mutating=True, admin=True)
        log.info("%s removed %s from the desk team", me, email)
        return team_change(team().remove, email, me)

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

    @app.post("/hooks/email")
    async def email_hook(request: Request) -> JSONResponse:
        """Emails to info@, forwarded by the n8n workflow that watches that inbox.

        Same key as the GHL webhook. Every email is sorted like any other
        message (sales questions become contacts, concerns tickets), so n8n
        does no sorting of its own. Repeats of the same Message-ID are ignored.
        """
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
        incoming = intake.parse_email(payload, own_domains())
        if incoming is None:
            log.info("email webhook: ignored mail from %s", payload.get("from_email"))
            return JSONResponse({"ok": True, "ignored": True})
        if not incoming.text:
            raise HTTPException(400, "The email has no text.")
        started = time.time()
        result = the_desk().ingest(incoming)
        log.info("email webhook: message %s%s in %.1fs", result["id"], " (duplicate)" if result["duplicate"] else "",
                 time.time() - started)
        return JSONResponse({"ok": True, "ignored": False, **result})

    def own_domains() -> tuple[str, ...]:
        """Our sending domains (from the sender settings), so the desk never files its own mail."""
        found = set()
        for name in ("DESK_FROM_EMAIL", "LOGIN_FROM_EMAIL", "DESK_REPLY_TO"):
            match = re.search(r"@([\w.-]+)", os.environ.get(name, ""))
            if match:
                found.add(match.group(1).lower())
        return tuple(found)

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
