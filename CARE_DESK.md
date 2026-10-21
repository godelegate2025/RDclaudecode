# Care Desk

Every comment, direct message and email that reaches Redefine's GHL lands in one
inbox. Claude sorts each one by issue type and urgency and drafts a public
reply. Concerns become tickets with response-time clocks, and questions about
services and prices become leads with an email follow-up journey.

It runs as its own Cloud Run service next to the Redefine App, because it keeps
data and must answer GHL's webhooks quickly while audits run for minutes.

| Piece | What it uses |
|---|---|
| Staff | The Redefine App's team sign-in: the same Google accounts, team list and admin page |
| Data | The app's Firestore database, in collections named `desk_…` |
| Messages in | A GHL workflow posts each new comment or message to `/hooks/ghl` |
| Sorting | Claude (`ANTHROPIC_API_KEY`, the Post Auditor's key), or keyword rules without one |
| Email out | Resend (`RESEND_API_KEY`, `DESK_FROM_EMAIL`); without them nothing is sent |
| Clock | Cloud Scheduler calls `/api/desk/tick` every 5 minutes to escalate late tickets and send due journey emails |

## What it can and cannot do

- **Facebook comments are replied to from the desk; everything else in GHL.**
  GHL's API has no endpoint for Social Planner comments, so the desk talks to
  Meta directly for Facebook (see "Facebook replies" below). For Instagram, DMs,
  and any Facebook comment the desk could not find, it drafts the reply, copies
  it and links to GHL, and records when someone marks it posted. Either way that
  counts as the ticket's first reply.
- **Only new comments arrive.** The GHL workflow fires on new comments, so
  anything posted before it was switched on stays in GHL.
- **Customer emails need an email on the GHL contact.** Most commenters have
  none, so their tickets are updated through GHL instead and their leads are
  marked "follow up by DM".
- **Lead journeys stop on a reply only if the lead writes in again.** A new
  message from the same email stops the journey automatically. A reply to a
  journey email goes to `DESK_REPLY_TO`; mark the lead "Replied" by hand.
- **A placeholder can never be emailed.** Templates with `[SQUARE BRACKETS]`
  cannot go live. The new lead journey ships with `[PORTFOLIO LINK]` and
  `[BOOKING LINK]` to fill in on the Email screen.
- **It never runs open.** Unlike the auditors, the desk refuses every page
  until team sign-in is set up, because it holds customers' messages.
- **Notes and files go on open tickets.** Open a ticket to add a note for the
  team, with up to 5 files (10 MB in all). Customers never see them. A resolved
  ticket keeps its notes but takes no new ones until it is reopened. Files are
  stored in Firestore in parts of up to 700 KB, so they need no storage bucket.

## Who can use it

Whoever can sign in to the Redefine App: the owners in `ALLOWED_EMAILS` and the
people on the app's team page (`/admin/team`). Removing someone there signs them
out of the desk too. The desk needs no user list of its own.

## Run it on a laptop

```powershell
pip install -r requirements-desk.txt
$env:DESK_STORE = "memory"            # keep data in memory; nothing reaches Firestore
$env:DESK_DEV_EMAIL = "you@redefine.ph"  # act as this person; only works with sign-in off
uvicorn care_desk.app:app --port 8010 --reload
```

Open http://127.0.0.1:8010. Data is gone when the server stops. Without
`ANTHROPIC_API_KEY` messages are sorted by keyword rules, and the header says
so. `DESK_DEV_EMAIL` is ignored as soon as the sign-in settings are present, so
it can never open a deployed desk.

## Deploy

Team sign-in and the Firestore database must already be set up for the Redefine
App (DEPLOY.md, "Team sign-in"). Then:

```powershell
.\deploy-desk.ps1 -FromEmail "Redefine <hello@redefine.ph>" -ReplyTo "hello@redefine.ph"
```

The script copies `GOOGLE_CLIENT_ID`, `ALLOWED_EMAILS` and `SESSION_SECRET` from
the running `redefine-apps` service (pass `-AppService` for another name), reuses
the Post Auditor's Claude key (the `anthropic-api-key` secret if there is one,
otherwise the key set on the app), creates the webhook and cron keys, builds
`Dockerfile.desk`, deploys `care-desk` to `us-central1` and schedules the
5-minute clock. Re-run it after code changes, and after changing the app's
sign-in settings.

Then, once:

1. **Google sign-in.** Console → Google Auth Platform → Clients → the app's
   client → *Authorised JavaScript origins*: add the desk's URL (the script
   prints it). Without this, Google's button refuses to sign in on the desk.
2. **Email (optional).** Add and verify Redefine's domain in Resend (it gives
   you DNS records to add), then store the key and re-run the script:

   ```powershell
   "re_..." | gcloud secrets create resend-api-key --data-file=-
   ```

3. **Link it from the Redefine App:**

   ```powershell
   gcloud run services update redefine-apps --region us-central1 --update-env-vars CARE_DESK_URL=https://<care-desk-url>/desk
   ```

   The Care Desk card on the app's home page then opens it.

The automatic deploy on push (cloudbuild.yaml) runs the desk's tests but does
not deploy the desk; `deploy-desk.ps1` does.

### Firestore costs

Each open desk page loads everything once, then asks only for what changed
every 20 seconds: an idle page costs about seven reads a refresh, around 30,000
a day if left open around the clock. Firestore's free tier is 50,000 reads and
20,000 writes a day. Each new message costs about ten writes. A note costs four
writes, plus two per attached file and one per 700 KB of it; opening a file
reads one document per 700 KB.

## Connect GHL

Get the webhook address:

```powershell
gcloud secrets versions access latest --secret desk-webhook-key
```

The URL is `https://<care-desk-url>/hooks/ghl?key=<that key>`.

In Redefine's GHL sub-account, go to **Automation → Workflows → Create Workflow →
Start from scratch**, then:

1. **Trigger:** *Facebook/Instagram – Comment(s) On A Post*. Choose Redefine's
   page and, if GHL offers it, all posts.
2. **Action:** *Webhook*. Method `POST`, URL as above.
3. **Custom Data**, one item per row. Pick each value from the merge-field
   picker (the `{{ }}` menu, under the trigger's fields):

   | Key | Value |
   |---|---|
   | `comment` | the comment text |
   | `channel` | type `facebook` (or `instagram` in a second workflow) |
   | `comment_id` | the comment's id, if the picker has one; stops duplicates |
   | `post_url` | the post's link, if the picker has one; powers "Open the post" |

4. Save and publish, then comment on a test post.

The comment should appear in the inbox within seconds. If it shows as **Could
not read**, the desk received the webhook but found no `comment`. Open it: the
desk shows exactly what GHL sent, so you can pick the right merge field.

**Direct messages and emails** use the same webhook. Make a second workflow with
the *Customer Replied* trigger, filtered to the channels you want, with
`comment` = the message body (`{{message.body}}`) and `channel` = `messenger`,
`instagram` or `email`.

GHL retries a webhook that fails. The desk ignores repeats with the same
`comment_id`, or the same person and text within a day when there is no id.

## Facebook replies

GHL tells the desk a comment's text and who wrote it, but not its Facebook id.
So when a Facebook comment arrives, the desk reads the newest comments on
Redefine Ph's 15 newest posts through Meta's Graph API and finds the one with the
same text, posted within 15 minutes of the webhook (case and spacing ignored).

- **One match:** the message gets the comment's id and link, and the detail
  panel shows an editable reply with **Send reply**. It posts as Redefine Ph
  under that comment, after a confirmation, and can't post twice.
- **Several matches** (two "❤️" in the same minute): the desk lists them and
  staff pick one. A comment already linked to another message is never reused.
- **No match yet:** the clock looks again every minute for six minutes, then
  offers **Search again** and the copy-into-GHL fallback.

Meta's app stays in development mode, which means Meta hides who wrote a comment
unless that person has a role on the app. That's why names still come from GHL,
and matching leans on text and time; when Meta does show a name, it must match.

**The token.** A long-lived Page token for Redefine Ph, from the "Redefine Care
Desk" Meta app with `pages_show_list`, `pages_read_engagement`,
`pages_read_user_content` and `pages_manage_engagement`, stored as the
`meta-page-token` secret. The deploy script hands it to the desk. The token
itself never expires, but Meta's data access on it lapses after 90 days without
use of the app by its admin (shown as "data access until" in Meta's Access Token
Debugger), and it stops working if the admin who made it loses the Page or
changes their password. When Send reply says the token stopped working:

1. Graph API Explorer → app **Redefine Care Desk** → User Token with the four
   permissions → **Generate Access Token**, Redefine Ph only.
2. Access Token Debugger → **Extend Access Token**, then in the Explorer with
   that long-lived user token run `me/accounts?fields=name,access_token` (GET)
   and copy Redefine Ph's `access_token` (starts with `EAA`).
3. `Get-Clipboard | gcloud secrets versions add meta-page-token --data-file=-`
4. Re-run `deploy-desk.ps1`.

## Settings

Everything Redefine-specific (issue types, teams, response targets, interests
and the default email templates) is in `care_desk/config.py`. Emails can also be
edited on the Email screen; those edits are stored in Firestore.

| Variable | Purpose |
|---|---|
| `GOOGLE_CLIENT_ID`, `ALLOWED_EMAILS`, `SESSION_SECRET` | The Redefine App's sign-in, copied by the deploy script |
| `LOGIN_FROM_EMAIL` | Sender of email sign-in codes, copied from the app; with `RESEND_API_KEY` it turns email sign-in on (DEPLOY.md, "Email sign-in") |
| `DESK_WEBHOOK_KEY` | Must match `?key=` on the GHL webhook URL |
| `DESK_CRON_KEY` | Lets Cloud Scheduler call the clock |
| `DESK_PUBLIC_URL` | Base of unsubscribe links (set by the deploy script) |
| `ANTHROPIC_API_KEY` | Claude sorting |
| `DESK_MODEL`, `DESK_EFFORT` | Default `claude-opus-5-5` at `low` effort |
| `RESEND_API_KEY`, `DESK_FROM_EMAIL`, `DESK_REPLY_TO` | Email sending |
| `DESK_BRAND`, `DESK_TICKET_PREFIX`, `DESK_SIGN_OFF` | Default `Redefine`, `RD`, `The Redefine team` |
| `META_PAGE_TOKEN` | Redefine Ph's Page token, for Facebook replies (secret `meta-page-token`) |
| `META_PAGE_ID` | The Page to read comments from; defaults to Redefine Ph (`1316682498188449`) |
| `DESK_STORE`, `DESK_DEV_EMAIL` | Laptop runs only, see above |
