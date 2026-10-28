# Care Desk

Every comment, direct message and email that reaches Redefine's GHL lands in one
inbox. Claude sorts each one by issue type and urgency and drafts a public
reply. Concerns become tickets with response-time clocks, and questions about
services and prices become leads with an email follow-up journey.

It runs as its own Cloud Run service next to the Redefine App, because it keeps
data and must answer GHL's webhooks quickly while audits run for minutes.

| Piece | What it uses |
|---|---|
| Staff | The Redefine App's sign-in (Google or an emailed code), checked against the desk's own team list |
| Data | Supabase (Postgres), schema `care_desk`, when `SUPABASE_DB_URL` is set; otherwise the app's Firestore database, in collections named `desk_…` |
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
- **Emails are answered from the desk.** An email that came in to info@ has a
  "Reply by email" box in the Inbox, pre-filled with Claude's draft. It goes
  from `DESK_FROM_EMAIL` as a reply in the customer's thread (In-Reply-To set,
  their message quoted), and counts as the ticket's first reply. On Contacts,
  "Send email" sends one email per selected contact with `{first_name}` filled
  in, at most 50 at a time; sent to two or more it carries an unsubscribe link
  and skips anyone unsubscribed. SMS and WhatsApp are still previews only.
- **Lead journeys stop when the lead writes back.** A new message from the same
  email stops the journey, including a reply to a journey email, which reaches
  info@ and comes in through the n8n workflow (see "Emails to info@").
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

The desk keeps its own team, managed on its **Team** page, separate from the
Redefine App's: being on one does not give access to the other. Everyone signs
in the same way (Google, or an emailed code) with the address on the list.

- **Admins** work the desk and manage the Team page: add people, switch them
  between Admin and Agent, remove them. Only admins see the page; the server
  refuses its API to anyone else.
- **Agents** work tickets, leads and email, but cannot see or change the team.
- **Owners** (the app's `ALLOWED_EMAILS`) are always desk admins and cannot be
  changed or removed here, so nobody can lock everyone out. Nobody can change
  their own role or remove themselves.

When an admin adds someone, they get an invite email from the sign-in sender
(`LOGIN_FROM_EMAIL`) with a button to the desk. The Team page shows each person
as **Pending** until they first use the desk, then **Approved**, with when they
were invited or first signed in. Pending people have **Resend invite** (at most
once a minute per person). A failed or switched-off invite never undoes the
add: being on the list is what grants access.

**Names.** Google sign-in takes the name from the Google account; someone who
signs in with an emailed code is asked for their name on their first visit.
The desk shows names instead of emails in the top bar, on the Team page, as
ticket owners, on notes and in Activity (records still store the email).
Clicking your name in the top bar changes it. Names are shared with the
Redefine App, in the Firestore collection `user_profiles`, and a name someone
typed is never replaced by their Google name.

Removing someone signs them out within a minute. The list is the Firestore
collection `desk_team`. The first time it was read it was filled from the
app's team (app admins as desk Admins, everyone else as Agents); after that the
two lists are independent.

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

## The new desk (React)

The desk's pages are being rebuilt in React + Vite + TypeScript in
`care_desk/web/`, one view at a time, at `/desk-next` beside the current desk
(`/desk`, `static/desk.html`). Contacts is done; the other views link back to
the current desk. Both use the same API and the same sign-in, so they can run
side by side on real data. Once every view is across, `/desk` serves the new
one and `desk.html` goes.

```powershell
cd care_desk/web
npm install
npm run dev        # http://localhost:5173/desk-next/, with the desk above running on port 8010
npm run build      # type-checks, then writes dist/, which the desk serves at /desk-next
```

`npm run dev` sends `/api` and the sign-in pages on to the desk on port 8010, so
the data is real and edits show up as you save. Nothing extra is needed to
deploy: `Dockerfile.desk` builds the pages in a Node stage and copies only
`dist/` into the image. `dist/` and `node_modules/` are not committed.

The styles are the current desk's, copied into `src/desk.css`, so the two look
the same while views move across. Records load from `/api/desk/state` through
TanStack Query (`src/desk.ts`), which keeps what was loaded and asks only for
changes every 20 seconds, as the current desk does. Tables use TanStack Table.

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

## Move to Supabase

The desk can keep its records in Supabase (Postgres) instead of Firestore. Each
`desk_<name>` collection becomes the table `care_desk.<name>`, with the record's
id in `key` and the record itself in `data` (jsonb), so SQL reads any field:

```sql
select data->>'category' as issue, count(*) from care_desk.messages group by 1 order by 2 desc;
```

The `care_desk` schema is not exposed through Supabase's Data API, so the
project's API keys can't reach it, and row level security is on with no
policies as a second lock; only the database login in the connection string
reads it. Only the desk moves:
sign-in, the app's team list and profiles stay in Firestore, and the desk keeps
reading them there.

1. **Make the project.** supabase.com → New project. The desk runs in Google's
   `us-central1` (Iowa) and calls the database several times per page and per
   message, so the project must be close to it: choose **Americas**, then under
   *Need a specific region?* pick **East US (Ohio)**, or **East US (North
   Virginia)** if Ohio isn't offered. A project in Asia would add about 0.2 s
   to every call. The region can't be changed later; a project made in the
   wrong one has to be deleted and made again. Keep the database password;
   one of letters and numbers only saves percent-encoding it in step 2. Untick
   *Automatically expose new tables*; the desk doesn't need the Data API.
2. **Store the connection string.** Project → **Connect** → *Transaction
   pooler* (port 6543; Cloud Run can't reach the direct address). Copy the URI,
   put the password in place of `[YOUR-PASSWORD]`, then:

   ```powershell
   Get-Clipboard | gcloud secrets create supabase-db-url --data-file=-
   ```

3. **Copy the records**, from the repo folder, in a quiet moment:

   ```powershell
   pip install -r requirements-desk.txt
   $env:SUPABASE_DB_URL = (gcloud secrets versions access latest --secret supabase-db-url)
   $env:FIRESTORE_DATABASE = "redefine-team"   # the app's Firestore database
   python -m care_desk.move_to_supabase
   ```

   It creates the tables and copies every record with its id. New ids on
   Supabase start 100 past Firestore's, so ticket numbers jump ahead once (say
   RD-1050 to RD-1151). That gap keeps anything the desk records on Firestore
   before the switch (step 4) from clashing with records made on Supabase.
4. **Switch the desk over:** re-run `deploy-desk.ps1`. It sees the
   `supabase-db-url` secret and hands it to the desk. The desk keeps its
   address, so GHL, Google sign-in and unsubscribe links need no changes.
5. **Catch stragglers:** run `python -m care_desk.move_to_supabase` once more,
   then `python -m care_desk.move_to_supabase --check`; the two columns should
   match. Re-runs only bring over records that are newer in Firestore, so
   nothing done on Supabase is undone.

The `desk_` collections stay in Firestore untouched, as a backup. To go back,
delete the `supabase-db-url` secret and re-run `deploy-desk.ps1`; anything
recorded on Supabase in the meantime stays there and is not copied back.

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

## Emails to info@

An n8n workflow watches the info@redefineph.com inbox and forwards each email
to `/hooks/email`. The desk sorts it like any other message: sales questions
become Contacts with the sender's email, concerns become Tickets, the rest is
logged. A reply from someone in a contact journey stops their journey, the
same as a message through GHL.

The workflow is "Care Desk: info@ inbox to desk" in n8n (redefine.app.n8n.cloud,
workflow `VldAWUdg4OL5b2Rd`); `care_desk/n8n/info-inbox-to-desk.json` is a copy to
re-import if it is ever lost (Workflows → Create workflow → ⋯ → Import from File).
It needs two credentials before it is published:

1. **Open it** in n8n.
2. **Mailbox credential** on *New email to info@*: IMAP, user
   `info@redefineph.com`, its Hostinger password, host `imap.hostinger.com`,
   port 993, SSL on.
3. **Desk key credential** on *Send to Care Desk*: Header Auth, name
   `X-Desk-Key`, value from
   `gcloud secrets versions access latest --secret desk-webhook-key`.
4. **Publish** the workflow and send a test email to info@ from another address.

The trigger reconnects to the mailbox every minute (Force Reconnect = 1): Hostinger's
IMAP doesn't push new-mail notifications to n8n, so without it nothing ever fires.
Emails reach the desk within about a minute. n8n leaves emails unread, skips our own domain, bounces, no-reply senders and
newsletters, and retries a failed send three times. The desk also ignores mail
from our sending domains and automatic senders, files only the new part of a
reply (quoted history cut off), and ignores repeats of the same Message-ID.

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
| `SUPABASE_DB_URL` | Keeps the desk's data in Supabase (secret `supabase-db-url`); without it, Firestore |
| `DESK_STORE`, `DESK_DEV_EMAIL` | Laptop runs only, see above |
