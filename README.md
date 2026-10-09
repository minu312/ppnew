# Learn-X PDF Bot (Heroku + MongoDB)

A Telegram PDF search and delivery bot with a built-in **Telegram Mini
App**. Runs on a single Heroku web dyno (bot + mini app in one process)
with MongoDB for all persistent data.

## What it does

- Admins upload PDFs by sending documents privately to the bot.
  Telegram stores the original file; MongoDB stores the searchable reference.
- Telegram Mini App (`/app` or the button in `/start`):
  - search papers, browse each tutor's collection, and download — the same
    gates apply as in the bot (channel + group + private group, and the
    tutor group when the paper belongs to one), and every download is
    watermarked and logged just like a bot download;
  - every app open is reported to the log group:
    `👀 Mini App Visited` with the user's name, username, ID, IP address,
    Telegram platform, and browser user-agent;
  - admin mode in the app: admins see a badge and can rename or delete
    files directly from search results.
- Users search by filename, code, or partial keyword, or browse folders.
- Access requires membership in:
  - the updates channel and the discussion group (public links are shown when missing);
  - the private group — its invite link is **never** shared; non-members are simply told
    they do not have access;
  - optionally, the tutor's own group for papers in that tutor's collection.
    Each tutor can have a dedicated group; non-members get that group's invite link.
- Every delivered PDF is personalized:
  - a visible black three-digit code at the bottom-left of **page 2**
    (last page is used for single-page PDFs) — the only visible mark;
  - an invisible micro-dot marker on **every page**: the recipient's
    Telegram ID and the trace ID are encoded in the positions of
    sub-pixel vector dots at a pseudo-random location. They are not
    text, so select-all, copy-paste, Ctrl+F, and text extraction show
    nothing; rendered pages are pixel-identical to the original except
    for the page-2 code;
  - innocuous-looking PDF metadata (generic creator/producer, with the
    trace ID and recipient ID disguised as comma-separated hex tokens)
    as a second recovery path;
  - `/trace` also still recognizes the plain-text marker from PDFs
    delivered by earlier versions of the bot.
- `/trace` (reply to a PDF) identifies a leaked copy: it recovers the trace ID,
  the embedded recipient ID, the visible code, the delivery timestamp, checks
  whether the visible code matches the embedded recipient, and then shows the
  stored delivery record.
- Deliveries, failures, uploads, no-result searches, blocked tutor-group
  attempts, and profile changes are logged to the log group. Delivery logs
  include the timestamp, file size, tutor group status, and the user's total
  download count.

## Tutor groups

Each tutor can have a dedicated Telegram group. A user must be a member of
that group to receive that tutor's papers. Files are linked to a tutor by,
in priority order:

1. `/setutor` for explicit assignments;
2. moving them into a folder named after the tutor key (e.g. `AP/paper.pdf`
   with tutor key `ap`);
3. the tutor key appearing as a word in the filename, e.g. `sd paper 03.pdf`
   is guarded by the `sd` tutor group, `rk fp 03.pdf` by the `rk` group
   (word-boundary match: `asdpaper.pdf` would not match `sd`).

Tutor groups are managed with admin commands and stored in MongoDB:

```text
/addtutor KEY | Group ID or @name | Invite URL | Display Name
/rmtutor KEY
/tutors
/setutor exact filename.pdf | KEY
```

They can also be seeded from the `TUTOR_GROUPS` environment variable (JSON list).

## Important limitations

- PDF fingerprinting discourages casual unauthorized sharing but cannot be made
  impossible to remove. A person can destroy all embedded information by rebuilding
  or rasterizing the document.
- Password-protected, corrupted, or unsupported PDFs are rejected. An original PDF
  is never sent when mandatory watermarking fails.
- Inline-mode file delivery is intentionally omitted because cached inline documents
  would bypass per-user watermarking.

## Heroku deployment

### 1. Rotate exposed credentials

Regenerate any bot token or API hash that has appeared in chat, source code,
screenshots, or public logs. Never commit real credentials.

### 2. Deploy

Upload this project to a private GitHub repository and connect it to a new
Heroku app (deployment method: GitHub), or push with the Heroku CLI.
Heroku builds it with `requirements.txt` / `runtime.txt` and starts
`web: python app.py` from the `Procfile`.

The Pyrogram session runs in memory (`SESSION_NAME=:memory:`), so no
persistent disk is needed. All persistent data (files, users, admins, bans,
deliveries, tutor groups) lives in MongoDB.

### 3. Add config variables

Copy the names from `.env.example` into Heroku Config Vars. Enter real values
only in Heroku.

Required:

```text
API_ID
API_HASH
BOT_TOKEN
MONGODB_URI
ADMIN_IDS
MAIN_CHANNEL_ID
MAIN_CHANNEL_URL
PUBLIC_GROUP_ID
PUBLIC_GROUP_URL
PRIVATE_GROUP_ID
LOG_GROUP_ID
```

Optional: `TUTOR_GROUPS`, `SESSION_NAME`, `DATA_DIR`, `PROTECT_CONTENT`, `LOG_LEVEL`.

### 4. Give the bot permissions

Add the bot as an administrator in the channel, the discussion group, the
private group, the log group, and every tutor group. It needs permission to
inspect membership and to post in the log group.

### 5. Web admin panel (optional)

Set `PANEL_PASSWORD=your-secret` to enable a browser dashboard at
`https://YOUR-APP-NAME.herokuapp.com/panel?key=your-secret`:
total users, stored PDFs, deliveries, bans, tutor groups, recent mini app
visits (user, IP, platform), recent deliveries, and a
User Messages page (`/messages?key=...`) with recent searches/messages.
Leave `PANEL_PASSWORD` empty to disable the panel entirely.

### 6. Discussions (optional)

Set `DISCUSSIONS` (JSON, tutor key -> refs). A bare number is a message ID
forwarded from the main channel, a t.me URL is sent as a link:

```
heroku config:set DISCUSSIONS={"ap":"1234,1235","sd":"https://t.me/Learn_X_Edu/789"}
```

Users request them with `/discussion` or the Discussions buttons in the
mini app; tutors with no configured refs are not shown.

### 7. Start the web dyno

Scale the web dyno (`heroku ps:scale web=1`). One process serves both the
bot (Telegram long polling) and the mini app over HTTPS on the port Heroku
assigns. Run exactly one dyno per bot token.

Then set the app's public URL so the Mini App button appears:

```
heroku config:set APP_URL=https://YOUR-APP-NAME.herokuapp.com
```

The mini app is served at `https://YOUR-APP-NAME.herokuapp.com/miniapp`.
Telegram requires an HTTPS URL — the default `herokuapp.com` domain works.
Without `APP_URL` the bot still runs fine, only the Mini App button
(`/app`, and the button in `/start`) is hidden.

## First test

1. Check the Heroku logs: all four chats must resolve successfully.
   If a private `-100...` chat cannot be resolved, send `/chatid@YOUR_BOT_USERNAME`
   inside that group and restart the dyno.
2. Send `/start`, then `/help` — user commands show with explanations.
3. As an admin, add a tutor group with `/addtutor` and send a test PDF.
4. Move it into the tutor's folder with `/movefile` and check `/tutors`.
5. Search for the file from an authorized user account that is **not** in the
   tutor group — the bot should offer the tutor group's join link.
6. Join the tutor group, tap **Check again**, and confirm:
   - the PDF arrives with the caption;
   - page 2 contains the three-digit code;
   - the log group receives the full delivery record (timestamp, size, tutor
     status, download count).
7. Reply to the delivered PDF with `/trace` from an admin chat.

## Commands

### User

- `/start` — Start the bot
- `/help` — Show commands with explanations
- `/myid` — Show your Telegram ID
- `/browse` — Browse PDF folders
- `/app` — Open the Learn-X Mini App (button also appears in `/start`)
- `/discussion` — Get a tutor's discussion materials
- `/contact your message` — Send a message to the admins
- Send plain text to search

### Admin

- Send a PDF privately to upload it
- `/addadmin USER_ID` — Grant admin rights
- `/rmadmin USER_ID` — Remove an admin
- `/renamefile old filename.pdf | new filename.pdf`
- `/rmfile exact filename.pdf`
- `/movefile exact filename.pdf | folder/path`
- `/setutor exact filename.pdf | KEY` — Assign/clear a tutor
- `/tutors` — List tutor groups
- `/addtutor KEY | Group ID or @name | Invite URL | Display Name`
- `/rmtutor KEY`
- `/ban USER_ID optional reason`
- `/unban USER_ID`
- `/broadcast message` (returns a Broadcast ID for deletion)
- `/deletebroadcast ID`
- `/cleardb` then `/confirmclear` (owners only)
- `/stats`
- `/checkaccess USER_ID`
- `/trace` (reply to a PDF)
- `/chatid`

## Local development

Create a `.env` from `.env.example`, point `MONGODB_URI` at a test database,
and run:

```bash
pip install -r requirements.txt
python app.py
```

Run the watermarking tests with:

```bash
python -m pytest tests/
```
