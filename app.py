import asyncio
import base64
import html
import json
import logging
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from pymongo.errors import DuplicateKeyError
from dotenv import load_dotenv

from config import PDF_CAPTION, load_settings
from database import Database
from pdf_tools import extract_trace_details, is_pdf, watermark_pdf


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("learnx")

load_dotenv()
settings = load_settings()
db = Database(settings.mongodb_uri, settings.admin_ids)

# Seed tutor groups from configuration; runtime /addtutor changes persist in MongoDB.
for tutor in settings.tutor_seed:
    db.add_tutor(
        tutor["key"],
        tutor["display_name"],
        tutor["group_ref"],
        tutor["invite_url"],
    )

app = Client(
    settings.session_name,
    api_id=settings.api_id,
    api_hash=settings.api_hash,
    bot_token=settings.bot_token,
    workdir=settings.data_dir,
    parse_mode=ParseMode.HTML,
)

PAGE_SIZE = 8
SEARCH_CACHE = {}
DELIVERY_LOCKS = {}
LOG_CHAT_ID = None  # numeric ID of the log group, resolved at startup


def normalize_query(value: str) -> str:
    value = " ".join((value or "").strip().lower().split())
    return re.sub(r"\b([1-9])\b", r"0\1", value)


def display_name(user) -> str:
    name = " ".join(
        part for part in [user.first_name or "", user.last_name or ""] if part
    ).strip()
    return name or user.username or str(user.id)


def mention(user) -> str:
    return f'<a href="tg://user?id={user.id}">{html.escape(display_name(user))}</a>'


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", Path(name).name).strip()
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned or "document.pdf"


def _bot_api_request(method: str, payload: dict):
    """Call the HTTPS Bot API, which accepts private numeric IDs directly."""
    url = f"https://api.telegram.org/bot{settings.bot_token}/{method}"
    body = urlencode(
        {
            key: json.dumps(value) if isinstance(value, (dict, list)) else value
            for key, value in payload.items()
            if value is not None
        }
    ).encode()
    request = Request(url, data=body, method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode())
    except HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"Telegram HTTP {exc.code}: {raw[:300]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Telegram connection error: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Unknown Telegram API error"))
    return result["result"]


async def bot_api_request(method: str, payload: dict):
    return await asyncio.to_thread(_bot_api_request, method, payload)


async def send_log(text: str):
    try:
        await bot_api_request(
            "sendMessage",
            {
                "chat_id": settings.log_group,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
    except Exception:
        log.exception("Could not send a message to the log group")


async def sync_user(user):
    old = db.upsert_user(
        user.id,
        user.username or "",
        user.first_name or "",
        user.last_name or "",
    )
    if not old:
        return
    changed = []
    comparisons = (
        ("Username", old["username"], user.username or ""),
        ("First name", old["first_name"], user.first_name or ""),
        ("Last name", old["last_name"], user.last_name or ""),
    )
    for label, before, after in comparisons:
        if before != after:
            changed.append(
                f"<b>{label}:</b> <code>{html.escape(before or '—')}</code> → "
                f"<code>{html.escape(after or '—')}</code>"
            )
    if changed:
        await send_log(
            "👤 <b>User profile changed</b>\n"
            f"<b>User:</b> {mention(user)}\n"
            f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
            + "\n".join(changed)
        )


async def membership_details(chat_ref, user_id: int):
    try:
        member = await bot_api_request(
            "getChatMember", {"chat_id": chat_ref, "user_id": user_id}
        )
        status = member.get("status", "unknown")
        allowed = status in {"creator", "administrator", "member"} or (
            status == "restricted" and bool(member.get("is_member"))
        )
        return {"allowed": allowed, "status": status, "error": ""}
    except Exception as exc:
        error = str(exc)
        log.warning("Membership check failed for %s/%s: %s", chat_ref, user_id, error)
        return {"allowed": False, "status": "error", "error": error}


async def membership(chat_ref, user_id: int) -> bool:
    return (await membership_details(chat_ref, user_id))["allowed"]


async def access_status(user_id: int):
    main, public, private = await asyncio.gather(
        membership(settings.main_channel, user_id),
        membership(settings.public_group, user_id),
        membership(settings.private_group, user_id),
    )
    return {"main": main, "public": public, "private": private}


async def require_access(message_or_query) -> bool:
    user = message_or_query.from_user
    status = await access_status(user.id)
    if all(status.values()):
        return True

    rows = []
    if not status["main"]:
        rows.append(
            [InlineKeyboardButton("📢 Join updates channel", url=settings.main_channel_url)]
        )
    if not status["public"]:
        rows.append(
            [InlineKeyboardButton("💬 Join discussion group", url=settings.public_group_url)]
        )
    if rows:
        rows.append([InlineKeyboardButton("✅ Check again", callback_data="verify")])
        text = (
            "🔒 <b>Membership required</b>\n\n"
            "Join the channel/group below, then tap <b>Check again</b>."
        )
        markup = InlineKeyboardMarkup(rows)
    else:
        text = (
            "🔒 <b>Private access required</b>\n\n"
            "You do not have permission to access these PDFs."
        )
        markup = None

    if hasattr(message_or_query, "message"):
        await message_or_query.message.reply_text(text, reply_markup=markup)
    else:
        await message_or_query.reply_text(text, reply_markup=markup)
    return False


async def tutor_gate(query, file_record: dict) -> bool:
    """Files guarded by a tutor group require membership in that group.

    Non-members get the tutor group's invite link (unlike the private
    group, whose link is never shared). Unassigned files pass freely.
    """
    tutor = db.tutor_for_file(file_record)
    if not tutor:
        return True
    if await membership(tutor["group_ref"], query.from_user.id):
        return True
    rows = [
        [InlineKeyboardButton(
            f"Join {tutor['display_name']} group", url=tutor["invite_url"]
        )],
        [InlineKeyboardButton(
            "✅ Check again", callback_data=f"tv:{file_record['id']}"
        )],
    ]
    await query.message.reply_text(
        f"🔒 <b>{html.escape(tutor['display_name'])} group required</b>\n\n"
        "This paper belongs to that tutor's collection.\n"
        "Join the group below, then tap <b>Check again</b>.",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    await send_log(
        "🔐 <b>Tutor group membership required</b>\n"
        f"<b>User:</b> {mention(query.from_user)}\n"
        f"<b>Telegram ID:</b> <code>{query.from_user.id}</code>\n"
        f"<b>Tutor group:</b> <code>{html.escape(tutor['display_name'])}</code>\n"
        f"<b>File:</b> <code>{html.escape(file_record['display_name'])}</code>\n"
        "❌ <b>Tutor group:</b> not a member"
    )
    return False


def cache_query(user_id: int, query: str) -> str:
    now = time.time()
    for key, value in list(SEARCH_CACHE.items()):
        if now - value["created"] > 3600:
            SEARCH_CACHE.pop(key, None)
    token = secrets.token_hex(4)
    SEARCH_CACHE[token] = {"user_id": user_id, "query": query, "created": now}
    return token


def search_markup(user_id: int, query: str, page: int, token: str = None):
    token = token or cache_query(user_id, query)
    total, files = db.search_files(query, PAGE_SIZE, page * PAGE_SIZE)
    rows = [
        [InlineKeyboardButton(item["display_name"], callback_data=f"f:{item['id']}:{token}")]
        for item in files
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"sp:{token}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"sp:{token}:{page+1}"))
    if nav:
        rows.append(nav)
    return total, InlineKeyboardMarkup(rows) if rows else None


def browse_path_encode(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


def browse_path_decode(value: str) -> str:
    value += "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value.encode()).decode()


def browse_markup(path: str):
    folders, files = db.folder_items(path)
    rows = []
    for folder in folders:
        child = f"{path}/{folder}" if path else folder
        rows.append(
            [
                InlineKeyboardButton(
                    f"📁 {folder}", callback_data=f"b:{browse_path_encode(child)}"
                )
            ]
        )
    for item in files:
        rows.append(
            [InlineKeyboardButton(f"📄 {item['display_name']}", callback_data=f"f:{item['id']}:browse")]
        )
    if path:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        rows.append(
            [
                InlineKeyboardButton(
                    "⬅️ Back", callback_data=f"b:{browse_path_encode(parent)}"
                )
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else None


@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    await sync_user(message.from_user)
    if db.is_banned(message.from_user.id):
        return
    markup = miniapp_button()
    await message.reply_text(
        "👋 <b>Welcome to Learn-X PDF Bot</b>\n\n"
        "Send a PDF name, paper code, or keyword to search.\n"
        "Use /browse to browse folders and /help for instructions."
        + ("\n\nOr use the app to browse and download all papers 👈" if markup else ""),
        reply_markup=markup,
    )


@app.on_message(filters.command("app") & filters.private)
async def app_handler(client, message):
    await sync_user(message.from_user)
    markup = miniapp_button()
    if not markup:
        await message.reply_text("⚠️ Mini App URL is not configured.")
        return
    await message.reply_text(
        "🎓 <b>Learn-X Mini App</b>\n\n"
        "Search and download past papers directly from the app!",
        reply_markup=markup,
    )


@app.on_message(filters.command("contact") & filters.private)
async def contact_handler(client, message):
    """Forward a user's message to the log group; admins reply to answer."""
    await sync_user(message.from_user)
    if db.is_banned(message.from_user.id):
        return
    text = command_argument(message)
    if not text:
        await message.reply_text("Usage: <code>/contact your message</code>")
        return
    if not await require_access(message):
        return
    await send_log(
        "📩 <b>New Contact Message</b>\n"
        f"<b>User:</b> {mention(message.from_user)}\n"
        f"<b>Username:</b> <code>@{html.escape(message.from_user.username or 'none')}</code>\n"
        f"<b>Telegram ID:</b> <code>{message.from_user.id}</code>\n"
        f"<b>Message:</b>\n{html.escape(text)}\n\n"
        "Reply to this message to answer the user."
    )
    await message.reply_text("✅ Your message has been sent to the admins.")


async def _forward_user_submission(message, note: str = ""):
    """Forward a non-admin's file/media to the log group for admin review."""
    try:
        await app.forward_messages(settings.log_group, message.chat.id, message.id)
        await send_log(
            "📩 <b>User submission</b>\n"
            f"<b>User:</b> {mention(message.from_user)}\n"
            f"<b>Telegram ID:</b> <code>{message.from_user.id}</code>\n"
            f"<b>Type:</b> <code>{html.escape(note or 'media')}</code>\n\n"
            "Reply to this message to answer the user."
        )
    except Exception:
        log.exception("Could not forward user submission")


@app.on_message(
    filters.private
    & (filters.photo | filters.video | filters.audio | filters.voice | filters.video_note)
)
async def media_forward_handler(client, message):
    if db.is_admin(message.from_user.id):
        return
    await sync_user(message.from_user)
    if db.is_banned(message.from_user.id):
        return
    kind = (
        "photo" if message.photo
        else "video" if message.video
        else "audio" if message.audio
        else "voice note" if message.voice
        else "video note"
    )
    await _forward_user_submission(message, kind)


@app.on_message(filters.command("help") & filters.private)
async def help_handler(client, message):
    await sync_user(message.from_user)
    admin_text = ""
    if db.is_admin(message.from_user.id):
        admin_text = (
            "\n\n<b>Admin commands</b>\n"
            "Send a PDF privately — Upload a paper to the bot\n"
            "/addadmin ID — Grant admin rights to a user\n"
            "/rmadmin ID — Remove an admin\n"
            "/renamefile old name | new name — Rename a stored PDF\n"
            "/rmfile exact filename.pdf — Delete a stored PDF\n"
            "/movefile exact filename.pdf | folder/path — Move a PDF into a folder\n"
            "/setutor exact filename.pdf | KEY — Assign a PDF to a tutor group (none to clear)\n"
            "/tutors — List configured tutor groups\n"
            "/addtutor KEY | Group ID/@name | Invite URL | Display Name — Add a tutor group\n"
            "/rmtutor KEY — Remove a tutor group\n"
            "TIP: tutor groups guard papers automatically when the key is in "
            "the filename (sd paper.pdf → sd) or the folder name; /setutor "
            "overrides this per file.\n"
            "/ban ID [reason] — Ban a user from the bot\n"
            "/unban ID — Lift a ban\n"
            "/broadcast message — Message every known user\n"
            "/deletebroadcast ID — Delete a sent broadcast from every chat\n"
            "/cleardb then /confirmclear — Deactivate all stored file records (owners only)\n"
            "/stats — Bot statistics\n"
            "/checkaccess ID — Check a user's memberships\n"
            "/trace (reply to PDF) — Identify who a leaked PDF belongs to\n"
            "/chatid — Show the current chat's ID\n"
        )
    await message.reply_text(
        "<b>User commands</b>\n"
        "/start — Start the bot and get a welcome\n"
        "/help — Show this command list\n"
        "/myid — Show your Telegram ID\n"
        "/browse — Browse PDF folders and pick files\n"
        "/app — Open the Learn-X Mini App (also in /start)\n\n"
        "<b>Contact admins</b>\n"
        "/contact your message — Send a message to the admins\n"
        "Files or media you send are forwarded to the admins; they can "
        "reply to you directly.\n\n"
        "<b>Discussions</b>\n"
        "/discussion — Get a tutor's discussion materials\n\n"
        "<b>How to get a paper</b>\n"
        "Send any filename, paper code, or keyword — the bot lists matches and "
        "you tap the one you want. Every PDF is personalized with a hidden "
        "trace, so keep your copies to yourself.\n"
        "<b>Access</b>\n"
        "You must be a member of our channel, the discussion group, and the "
        "private group. Some papers also require joining the tutor's own group "
        "— the bot will show a join link when that applies."
        + admin_text
    )


@app.on_message(filters.command("myid") & filters.private)
async def myid_handler(client, message):
    await sync_user(message.from_user)
    await message.reply_text(f"Your Telegram ID: <code>{message.from_user.id}</code>")


@app.on_message(filters.group & filters.reply, group=0)
async def admin_reply_handler(client, message):
    """Admins reply to a forwarded/contact message in the log group to
    answer the user directly."""
    if not LOG_CHAT_ID or message.chat.id != LOG_CHAT_ID:
        return
    if message.from_user is None or message.from_user.is_bot:
        return
    if not db.is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    replied = message.reply_to_message
    if not replied:
        return

    user_id = None
    if replied.forward_from:
        user_id = replied.forward_from.id
    else:
        reply_body = replied.text or replied.caption or ""
        match = re.search(r"ID:\s*(\d+)", reply_body)
        if match:
            user_id = int(match.group(1))
    if not user_id:
        await message.reply_text(
            "⚠️ Could not find the user ID. Reply to the info message "
            "that contains the user's ID (or the forwarded message)."
        )
        return
    try:
        await app.send_message(
            user_id, f"👨‍💻 <b>Admin Reply:</b>\n{html.escape(text)}"
        )
        await message.reply_text("✅ Reply sent to the user.")
    except Exception:
        log.exception("Admin reply failed")
        await message.reply_text(
            "❌ Failed to send the reply. The user may have blocked the bot."
        )


async def send_discussion_messages(user_id: int, key: str):
    """Forward a tutor's discussion posts (configured via DISCUSSIONS)."""
    key = (key or "").strip().lower()
    refs = settings.discussions.get(key, "")
    tokens = [t for t in re.split(r"[\s,]+", refs.strip()) if t]
    if not tokens:
        return False, "⚠️ Discussion messages are not configured for this tutor yet."
    sent_any = False
    for token in tokens:
        if token.lstrip("-").isdigit():
            try:
                await app.forward_messages(user_id, settings.main_channel, int(token))
                sent_any = True
            except Exception:
                log.exception("Discussion forward failed for %s", token)
        elif token.startswith("http://") or token.startswith("https://"):
            try:
                await app.send_message(user_id, token)
                sent_any = True
            except Exception:
                log.exception("Discussion link failed for %s", token)
    if sent_any:
        return True, ""
    return False, "⚠️ Failed to send discussion materials. Please contact an admin."


@app.on_message(filters.command("discussion") & filters.private)
async def discussion_handler(client, message):
    await sync_user(message.from_user)
    if db.is_banned(message.from_user.id):
        return
    if not settings.discussions:
        await message.reply_text("⚠️ Discussion materials are not configured yet.")
        return
    if not await require_access(message):
        return
    rows, row = [], []
    for key in sorted(settings.discussions):
        row.append(InlineKeyboardButton(key.upper(), callback_data=f"dt:{key}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await message.reply_text(
        "💬 <b>Discussion materials</b>\n\nPlease choose a tutor:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


@app.on_callback_query(filters.regex(r"^dt:"))
async def discussion_tutor_callback(client, query):
    await sync_user(query.from_user)
    if db.is_banned(query.from_user.id):
        await query.answer("Access denied.", show_alert=True)
        return
    if not await require_access(query):
        await query.answer()
        return
    key = query.data.split(":", 1)[1]
    await query.answer("Sending discussions…")
    ok, err = await send_discussion_messages(query.from_user.id, key)
    if ok:
        await query.message.reply_text("✅ Discussion materials sent to your chat.")
        await send_log(
            "💬 <b>Discussions sent</b>\n"
            f"<b>User:</b> {mention(query.from_user)}\n"
            f"<b>Telegram ID:</b> <code>{query.from_user.id}</code>\n"
            f"<b>Tutor:</b> <code>{html.escape(key)}</code>"
        )
    else:
        await query.message.reply_text(err)


@app.on_message(filters.command("chatid"))
async def chatid_handler(client, message):
    if not message.from_user or not db.is_admin(message.from_user.id):
        return
    await message.reply_text(
        f"Chat ID: <code>{message.chat.id}</code>\n"
        f"Chat type: <code>{message.chat.type}</code>"
    )


@app.on_message(filters.private & filters.document)
async def document_handler(client, message):
    user = message.from_user
    await sync_user(user)

    # Admins upload PDFs; non-admin documents are forwarded for admin review.
    if not db.is_admin(user.id):
        if db.is_banned(user.id):
            return
        filename = document.file_name or "document"
        if not filename.lower().endswith(".pdf"):
            await message.reply_text(
                "⚠️ Only PDF documents are supported. "
                "Your file was forwarded to the admins."
            )
            await _forward_user_submission(message, f"document: {filename}")
            return
        await _forward_user_submission(message, f"PDF: {filename}")
        await message.reply_text(
            "✅ Your PDF was forwarded to the admins for review. "
            "Use /contact if you want to add a message."
        )
        return

    document = message.document
    filename = document.file_name or "document.pdf"
    if not filename.lower().endswith(".pdf") and document.mime_type != "application/pdf":
        await message.reply_text("⚠️ Only PDF documents are supported.")
        return

    normalized = normalize_query(filename)
    try:
        file_db_id = db.add_file(
            display_name=filename,
            search_name=normalized,
            telegram_file_id=document.file_id,
            telegram_file_unique_id=document.file_unique_id,
            file_size=document.file_size or 0,
            uploaded_by=user.id,
        )
    except DuplicateKeyError:
        await message.reply_text(
            f"⚠️ A file named <code>{html.escape(filename)}</code> already exists."
        )
        return

    size_mb = (document.file_size or 0) / (1024 * 1024)
    await message.reply_text(
        f"✅ Saved <b>{html.escape(filename)}</b>\n"
        f"File record: <code>{file_db_id}</code>"
    )
    await send_log(
        "📥 <b>PDF uploaded</b>\n"
        f"<b>Admin:</b> {mention(user)}\n"
        f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
        f"<b>File:</b> <code>{html.escape(filename)}</code>\n"
        f"<b>Size:</b> {size_mb:.2f} MB\n"
        f"<b>Record:</b> <code>{file_db_id}</code>"
    )


@app.on_message(filters.command("browse") & filters.private)
async def browse_handler(client, message):
    await sync_user(message.from_user)
    if db.is_banned(message.from_user.id):
        return
    if not await require_access(message):
        return
    markup = browse_markup("")
    if not markup:
        await message.reply_text("No PDFs have been added yet.")
        return
    await message.reply_text("📂 <b>Browse PDFs</b>", reply_markup=markup)


@app.on_callback_query(filters.regex(r"^verify$"))
async def verify_callback(client, query):
    await sync_user(query.from_user)
    if await require_access(query):
        await query.answer("Membership verified!", show_alert=True)
        await query.message.reply_text("✅ Verified. Send a PDF name or code to search.")
    else:
        await query.answer()


@app.on_callback_query(filters.regex(r"^sp:"))
async def search_page_callback(client, query):
    try:
        _, token, page_text = query.data.split(":", 2)
        cached = SEARCH_CACHE.get(token)
        if not cached or cached["user_id"] != query.from_user.id:
            await query.answer("Search expired. Please search again.", show_alert=True)
            return
        page = max(0, int(page_text))
        total, markup = search_markup(
            query.from_user.id, cached["query"], page, token=token
        )
        await query.message.edit_text(
            f"🔎 Found <b>{total}</b> result(s). Select a PDF:",
            reply_markup=markup,
        )
        await query.answer()
    except Exception:
        log.exception("Search pagination failed")
        await query.answer("Could not load that page.", show_alert=True)


@app.on_callback_query(filters.regex(r"^b:"))
async def browse_callback(client, query):
    try:
        if not await require_access(query):
            await query.answer()
            return
        path = browse_path_decode(query.data[2:])
        markup = browse_markup(path)
        text = f"📂 <b>Browse:</b> <code>{html.escape(path or 'Root')}</code>"
        if not markup:
            text += "\n\nThis folder is empty."
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
    except Exception:
        log.exception("Browse callback failed")
        await query.answer("Could not open that folder.", show_alert=True)


def miniapp_button():
    """Web App button shown in /app (and /start) when APP_URL is configured."""
    if not settings.app_url:
        return None
    url = settings.app_url.rstrip("/") + "/miniapp"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📚 Open Learn-X App", web_app=WebAppInfo(url=url))]]
    )


async def deliver_pdf_to_user(
    user, file_record: dict, requested_query: str, source: str = "Bot"
):
    visible_code = str(user.id)[-3:].zfill(3)
    trace_id = f"LX-{secrets.token_hex(4).upper()}"
    db.create_delivery(
        trace_id,
        file_record["id"],
        user,
        visible_code,
        requested_query,
    )

    lock = DELIVERY_LOCKS.setdefault(user.id, asyncio.Lock())
    async with lock:
        with tempfile.TemporaryDirectory(dir=settings.data_dir) as temp_dir:
            original = os.path.join(temp_dir, "original.pdf")
            output = os.path.join(
                temp_dir, safe_filename(file_record["display_name"])
            )
            try:
                downloaded = await app.download_media(
                    file_record["telegram_file_id"], file_name=original
                )
                if not downloaded or not is_pdf(downloaded):
                    raise ValueError("Telegram media is not a valid PDF")

                await asyncio.to_thread(
                    watermark_pdf,
                    downloaded,
                    output,
                    user.id,
                    visible_code,
                    trace_id,
                    file_record["display_name"],
                )

                sent = await app.send_document(
                    user.id,
                    output,
                    caption=PDF_CAPTION,
                    protect_content=settings.protect_content,
                )
                db.finish_delivery(trace_id, "sent", sent.id)

                tutor = db.tutor_for_file(file_record)
                if tutor:
                    tutor_member = await membership(tutor["group_ref"], user.id)
                    tutor_line = (
                        f"{'✅' if tutor_member else '❌'} "
                        f"<b>Tutor group:</b> {html.escape(tutor['display_name'])}"
                    )
                else:
                    tutor_line = "— <b>Tutor group:</b> not required"

                delivered_at = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
                size_mb = (file_record.get("file_size") or 0) / (1024 * 1024)
                downloads = db.count_user_deliveries(user.id)

                await send_log(
                    "📤 <b>Watermarked PDF delivered</b>\n"
                    f"<b>Source:</b> <code>{html.escape(source)}</code>\n"
                    f"<b>User:</b> {mention(user)}\n"
                    f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
                    f"<b>Username:</b> <code>@{html.escape(user.username or 'none')}</code>\n"
                    f"<b>Visible code:</b> <code>{visible_code}</code>\n"
                    f"<b>Hidden trace:</b> <code>{trace_id}</code>\n"
                    f"<b>File:</b> <code>{html.escape(file_record['display_name'])}</code>\n"
                    f"<b>Search:</b> <code>{html.escape(requested_query or 'browse')}</code>\n"
                    f"<b>Delivered at:</b> <code>{delivered_at}</code>\n"
                    f"<b>Size:</b> <code>{size_mb:.2f} MB</code>\n"
                    f"<b>User downloads:</b> <code>{downloads}</code>\n"
                    f"{tutor_line}\n"
                    "✅ <b>Main channel:</b> member\n"
                    "✅ <b>Discussion group:</b> member\n"
                    "✅ <b>Private group:</b> member"
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                db.finish_delivery(trace_id, "failed", error=error)
                log.exception("PDF delivery failed")
                await app.send_message(
                    user.id,
                    "❌ The PDF could not be processed. The original was not sent. "
                    "Administrators have been notified.",
                )
                await send_log(
                    "❌ <b>PDF delivery failed</b>\n"
                    f"<b>Source:</b> <code>{html.escape(source)}</code>\n"
                    f"<b>User:</b> {mention(user)}\n"
                    f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
                    f"<b>Visible code:</b> <code>{visible_code}</code>\n"
                    f"<b>Trace:</b> <code>{trace_id}</code>\n"
                    f"<b>File:</b> <code>{html.escape(file_record['display_name'])}</code>\n"
                    f"<b>Error:</b> <code>{html.escape(error[:800])}</code>"
                )


async def deliver_pdf(query, file_record: dict, requested_query: str):
    """Deliver a watermarked PDF from a bot callback query."""
    await query.answer("Preparing your personalized PDF…")
    await deliver_pdf_to_user(
        query.from_user, file_record, requested_query, source="Bot"
    )


@app.on_callback_query(filters.regex(r"^f:"))
async def file_callback(client, query):
    await sync_user(query.from_user)
    if db.is_banned(query.from_user.id):
        await query.answer("Access denied.", show_alert=True)
        return
    if not await require_access(query):
        await query.answer()
        return
    try:
        _, file_id_text, token = query.data.split(":", 2)
        file_record = db.get_file(int(file_id_text))
        if not file_record:
            await query.answer("File not found.", show_alert=True)
            return
        if not await tutor_gate(query, file_record):
            await query.answer()
            return
        requested_query = "browse"
        cached = SEARCH_CACHE.get(token)
        if cached and cached["user_id"] == query.from_user.id:
            requested_query = cached["query"]
        await deliver_pdf(query, file_record, requested_query)
    except Exception:
        log.exception("File callback failed")
        await query.answer("An error occurred.", show_alert=True)


@app.on_callback_query(filters.regex(r"^tv:"))
async def tutor_verify_callback(client, query):
    """Re-check tutor-group membership after the user tapped Check again."""
    await sync_user(query.from_user)
    if db.is_banned(query.from_user.id):
        await query.answer("Access denied.", show_alert=True)
        return
    try:
        file_record = db.get_file(int(query.data.split(":", 1)[1]))
        if not file_record:
            await query.answer("File not found.", show_alert=True)
            return
        tutor = db.tutor_for_file(file_record)
        if not tutor or await membership(tutor["group_ref"], query.from_user.id):
            await query.answer("Membership verified! Preparing your PDF…")
            await deliver_pdf(query, file_record, "browse")
        else:
            await query.answer(
                "You still need to join the tutor group first.", show_alert=True
            )
    except Exception:
        log.exception("Tutor verify callback failed")
        await query.answer("An error occurred.", show_alert=True)


@app.on_message(filters.private & filters.text, group=10)
async def text_search_handler(client, message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    user = message.from_user
    await sync_user(user)
    if db.is_banned(user.id):
        return
    db.log_message(user, text)
    if not await require_access(message):
        return
    query = normalize_query(text)
    total, markup = search_markup(user.id, query, 0)
    if total == 0:
        await message.reply_text("Sorry, no PDFs were found matching that name.")
        await send_log(
            "🔍 <b>No-result search</b>\n"
            f"<b>User:</b> {mention(user)}\n"
            f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
            f"<b>Query:</b> <code>{html.escape(text)}</code>"
        )
        return
    await message.reply_text(
        f"🔎 Found <b>{total}</b> result(s). Select a PDF:",
        reply_markup=markup,
    )


def command_argument(message) -> str:
    parts = (message.text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


@app.on_message(filters.command("addadmin") & filters.private)
async def addadmin_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    try:
        user_id = int(command_argument(message))
        db.add_admin(user_id)
        await message.reply_text(f"✅ Added admin <code>{user_id}</code>.")
    except ValueError:
        await message.reply_text("Usage: <code>/addadmin USER_ID</code>")


@app.on_message(filters.command("rmadmin") & filters.private)
async def rmadmin_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    try:
        user_id = int(command_argument(message))
        if user_id in settings.admin_ids:
            await message.reply_text("Configured owner/admin IDs cannot be removed.")
            return
        db.remove_admin(user_id)
        await message.reply_text(f"✅ Removed admin <code>{user_id}</code>.")
    except ValueError:
        await message.reply_text("Usage: <code>/rmadmin USER_ID</code>")


@app.on_message(filters.command("rmfile") & filters.private)
async def rmfile_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    name = normalize_query(command_argument(message))
    if not name:
        await message.reply_text("Usage: <code>/rmfile exact filename.pdf</code>")
        return
    count = db.delete_file(name)
    await message.reply_text(
        "✅ File record deleted." if count else "No exact filename was found."
    )


@app.on_message(filters.command("renamefile") & filters.private)
async def renamefile_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    value = command_argument(message)
    if "|" not in value:
        await message.reply_text(
            "Usage: <code>/renamefile old filename.pdf | new filename.pdf</code>"
        )
        return
    old, new = (part.strip() for part in value.split("|", 1))
    try:
        count = db.rename_file(
            normalize_query(old), new, normalize_query(new)
        )
        await message.reply_text(
            "✅ File renamed." if count else "No exact filename was found."
        )
    except DuplicateKeyError:
        await message.reply_text("A file with the new name already exists.")


@app.on_message(filters.command("movefile") & filters.private)
async def movefile_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    value = command_argument(message)
    if "|" not in value:
        await message.reply_text(
            "Usage: <code>/movefile exact filename.pdf | folder/path</code>"
        )
        return
    name, folder = (part.strip() for part in value.split("|", 1))
    folder = folder.strip("/")
    count = db.move_file(normalize_query(name), folder)
    await message.reply_text(
        f"✅ Moved to <code>{html.escape(folder or 'root')}</code>."
        if count
        else "No exact filename was found."
    )


@app.on_message(filters.command("setutor") & filters.private)
async def setutor_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    value = command_argument(message)
    if "|" not in value:
        await message.reply_text(
            "Usage: <code>/setutor exact filename.pdf | KEY</code>\n"
            "Use <code>none</code> instead of a key to clear the assignment.\n"
            "TIP: files are guarded automatically when the tutor key appears in "
            "the filename (e.g. <code>sd</code>, <code>rk</code>) or as the "
            "top-level folder name."
        )
        return
    name, key = (part.strip() for part in value.split("|", 1))
    key_clean = key.strip().lower()
    if key_clean not in {"none", "-", ""}:
        if not db.get_tutor(key_clean):
            await message.reply_text(
                f"⚠️ Tutor <code>{html.escape(key_clean)}</code> is not configured. "
                "Use /addtutor first or run /tutors to see the list."
            )
            return
    else:
        key_clean = None
    count = db.set_file_tutor(normalize_query(name), key_clean)
    await message.reply_text(
        f"✅ Tutor assignment updated to <code>{html.escape(key_clean or 'none')}</code>."
        if count
        else "No exact filename was found."
    )


@app.on_message(filters.command("tutors") & filters.private)
async def tutors_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    tutors = db.list_tutors()
    if not tutors:
        await message.reply_text(
            "No tutor groups are configured.\n"
            "Add one with:\n"
            "<code>/addtutor KEY | Group ID or @name | Invite URL | Display Name</code>"
        )
        return
    lines = []
    for tutor in tutors:
        lines.append(
            f"<b>{html.escape(tutor['display_name'])}</b> "
            f"(<code>{html.escape(tutor['key'])}</code>)\n"
            f"Group: <code>{html.escape(str(tutor['group_ref']))}</code> · "
            f"Invite: {html.escape(tutor['invite_url'] or 'not set')}"
        )
    await message.reply_text("👨‍🏫 <b>Tutor groups</b>\n\n" + "\n\n".join(lines))


@app.on_message(filters.command("addtutor") & filters.private)
async def addtutor_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    value = command_argument(message)
    parts = [part.strip() for part in value.split("|")]
    if len(parts) != 4 or not all(parts):
        await message.reply_text(
            "Usage: <code>/addtutor KEY | Group ID or @name | Invite URL | Display Name</code>\n\n"
            "Example:\n"
            "<code>/addtutor AP | @ap_papers | https://t.me/ap_papers | Anuradha Perera</code>\n\n"
            "KEY is also the folder name that auto-guards files of this tutor."
        )
        return
    key, group_ref, invite_url, display_name = parts
    group_ref = (
        int(group_ref) if group_ref.lstrip("-").isdigit() else group_ref
    )
    db.add_tutor(key, display_name, group_ref, invite_url)
    await message.reply_text(
        f"✅ Tutor group <code>{html.escape(key.lower())}</code> saved for "
        f"<b>{html.escape(display_name)}</b>."
    )


@app.on_message(filters.command("rmtutor") & filters.private)
async def rmtutor_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    key = command_argument(message).strip().lower()
    if not key:
        await message.reply_text("Usage: <code>/rmtutor KEY</code>")
        return
    count = db.remove_tutor(key)
    await message.reply_text(
        f"✅ Tutor group <code>{html.escape(key)}</code> removed." if count
        else f"No tutor named <code>{html.escape(key)}</code> was found."
    )


@app.on_message(filters.command("ban") & filters.private)
async def ban_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    parts = command_argument(message).split(None, 1)
    try:
        user_id = int(parts[0])
        reason = parts[1] if len(parts) > 1 else ""
        db.ban(user_id, reason)
        await message.reply_text(f"✅ Banned <code>{user_id}</code>.")
    except (ValueError, IndexError):
        await message.reply_text("Usage: <code>/ban USER_ID optional reason</code>")


@app.on_message(filters.command("unban") & filters.private)
async def unban_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    try:
        user_id = int(command_argument(message))
        db.unban(user_id)
        await message.reply_text(f"✅ Unbanned <code>{user_id}</code>.")
    except ValueError:
        await message.reply_text("Usage: <code>/unban USER_ID</code>")


@app.on_message(filters.command("stats") & filters.private)
async def stats_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    stats = db.stats()
    await message.reply_text(
        "📊 <b>Bot statistics</b>\n"
        f"PDFs: <b>{stats['files']}</b>\n"
        f"Users: <b>{stats['users']}</b>\n"
        f"Deliveries: <b>{stats['deliveries']}</b>\n"
        f"Successful: <b>{stats['successful']}</b>\n"
        f"Banned: <b>{stats['banned']}</b>\n"
        f"Tutor groups: <b>{stats['tutors']}</b>"
    )


@app.on_message(filters.command("checkaccess") & filters.private)
async def checkaccess_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    try:
        target_id = int(command_argument(message))
    except ValueError:
        await message.reply_text("Usage: <code>/checkaccess USER_ID</code>")
        return
    main, public, private = await asyncio.gather(
        membership_details(settings.main_channel, target_id),
        membership_details(settings.public_group, target_id),
        membership_details(settings.private_group, target_id),
    )
    tutors = db.list_tutors()
    tutor_results = await asyncio.gather(
        *(membership_details(t["group_ref"], target_id) for t in tutors)
    )

    def line(label, result):
        icon = "✅" if result["allowed"] else "❌"
        text = f"{icon} <b>{label}:</b> <code>{html.escape(result['status'])}</code>"
        if result["error"]:
            text += f"\n<code>{html.escape(result['error'][:300])}</code>"
        return text

    lines = [
        f"🔎 <b>Access check for</b> <code>{target_id}</code>\n",
        line("Main channel", main),
        line("Discussion group", public),
        line("Private group", private),
    ]
    for tutor, result in zip(tutors, tutor_results):
        lines.append(
            line(f"Tutor group: {tutor['display_name']}", result)
        )
    await message.reply_text("\n".join(lines))


@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    text = command_argument(message)
    if not text:
        await message.reply_text("Usage: <code>/broadcast your message</code>")
        return
    broadcast_id = secrets.token_hex(4).upper()
    sent = failed = 0
    progress = await message.reply_text("Broadcast started…")
    for user_id in db.all_user_ids():
        try:
            sent_msg = await app.send_message(user_id, text)
            sent += 1
            db.log_broadcast(broadcast_id, user_id, sent_msg.id)
        except FloodWait as exc:
            await asyncio.sleep(exc.value)
            try:
                sent_msg = await app.send_message(user_id, text)
                sent += 1
                db.log_broadcast(broadcast_id, user_id, sent_msg.id)
            except Exception:
                failed += 1
        except Exception:
            failed += 1
    await progress.edit_text(
        f"✅ Broadcast finished.\n"
        f"Sent: <b>{sent}</b>\nFailed: <b>{failed}</b>\n"
        f"Broadcast ID: <code>{broadcast_id}</code>\n"
        "Save this ID to delete the broadcast later with "
        "<code>/deletebroadcast " + broadcast_id + "</code>"
    )


@app.on_message(filters.command("deletebroadcast") & filters.private)
async def deletebroadcast_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    broadcast_id = command_argument(message).strip()
    if not broadcast_id:
        await message.reply_text("Usage: <code>/deletebroadcast BROADCAST_ID</code>")
        return
    logs = db.broadcast_targets(broadcast_id)
    if not logs:
        await message.reply_text(
            f"⚠️ No broadcast found with ID: <code>{html.escape(broadcast_id)}</code>"
        )
        return
    deleted = failed = 0
    for entry in logs:
        try:
            await app.delete_messages(entry["user_id"], entry["message_id"])
            deleted += 1
        except Exception:
            failed += 1
    db.clear_broadcast(broadcast_id)
    await message.reply_text(
        f"🗑️ Broadcast <code>{html.escape(broadcast_id)}</code> deleted.\n"
        f"Deleted from: <b>{deleted}</b> chats\nFailed: <b>{failed}</b> chats"
    )


@app.on_message(filters.command("cleardb") & filters.private)
async def cleardb_handler(client, message):
    if message.from_user.id not in settings.admin_ids:
        return
    await message.reply_text(
        "⚠️ <b>WARNING:</b> This will deactivate ALL stored file records. "
        "This action cannot be undone.\n\n"
        "To confirm, send: <code>/confirmclear</code>"
    )


@app.on_message(filters.command("confirmclear") & filters.private)
async def confirmclear_handler(client, message):
    if message.from_user.id not in settings.admin_ids:
        return
    count = db.clear_files()
    await send_log(
        "🗑️ <b>Database cleared</b>\n"
        f"<b>Admin:</b> {mention(message.from_user)}\n"
        f"<b>File records deactivated:</b> <code>{count}</code>"
    )
    await message.reply_text(
        f"✅ Database cleared. <b>{count}</b> file records deactivated."
    )


@app.on_message(filters.command("trace") & filters.private)
async def trace_handler(client, message):
    if not db.is_admin(message.from_user.id):
        return
    replied = message.reply_to_message
    if not replied or not replied.document:
        await message.reply_text("Reply to a suspicious PDF with <code>/trace</code>.")
        return
    status = await message.reply_text("Inspecting PDF…")
    with tempfile.TemporaryDirectory(dir=settings.data_dir) as temp_dir:
        path = os.path.join(temp_dir, "trace.pdf")
        try:
            downloaded = await app.download_media(
                replied.document.file_id, file_name=path
            )
            details = await asyncio.to_thread(extract_trace_details, downloaded)
            trace_id = details["trace_id"]
            if not trace_id:
                await status.edit_text("No Learn-X trace marker was found.")
                return
            embedded_uid = details["uid"]
            visible_code = details["visible_code"]
            expected_code = str(embedded_uid)[-3:].zfill(3) if embedded_uid else None
            code_consistent = (
                visible_code is None or expected_code is None
                or visible_code == expected_code
            )
            header = (
                "🔎 <b>PDF traced</b>\n"
                f"<b>Trace:</b> <code>{trace_id}</code>\n"
                f"<b>Recipient ID (embedded):</b> <code>{embedded_uid or 'not found'}</code>\n"
                f"<b>Visible code (embedded):</b> <code>{visible_code or 'not found'}</code>\n"
                f"<b>Delivered at (metadata):</b> <code>{details['delivered'] or 'not found'}</code>\n"
                f"<b>Marker sources:</b> <code>{', '.join(sorted(details['sources'])) or 'unknown'}</code>"
            )
            if visible_code and not code_consistent:
                header += (
                    "\n⚠️ <b>The visible code does not match the embedded "
                    "recipient ID — this copy may have been altered.</b>"
                )
            record = db.find_trace(trace_id)
            if not record:
                await status.edit_text(
                    header
                    + "\n\nNo matching local delivery record exists."
                )
                return
            if expected_code and record["visible_code"] != expected_code:
                header += (
                    "\n⚠️ <b>The stored delivery record does not match the "
                    "embedded recipient ID — this copy may have been altered.</b>"
                )
            await status.edit_text(
                header
                + "\n\n"
                + "📋 <b>Delivery record</b>\n"
                f"<b>Recipient ID:</b> <code>{record['user_id']}</code>\n"
                f"<b>Username at delivery:</b> "
                f"<code>@{html.escape(record['username'] or 'none')}</code>\n"
                f"<b>Name at delivery:</b> "
                f"<code>{html.escape((record['first_name'] + ' ' + record['last_name']).strip())}</code>\n"
                f"<b>Visible code:</b> <code>{record['visible_code']}</code>\n"
                f"<b>File:</b> <code>{html.escape(record['display_name'])}</code>\n"
                f"<b>Delivered:</b> <code>{record['created_at']}</code>\n"
                f"<b>Status:</b> <code>{record['status']}</code>"
            )
        except Exception as exc:
            log.exception("Trace inspection failed")
            await status.edit_text(
                f"Could not inspect this PDF: <code>{html.escape(str(exc))}</code>"
            )


async def startup_checks():
    me = await app.get_me()
    log.info("Started as @%s (%s)", me.username, me.id)
    global LOG_CHAT_ID
    for label, chat in (
        ("main channel", settings.main_channel),
        ("public group", settings.public_group),
        ("private group", settings.private_group),
        ("log group", settings.log_group),
    ):
        try:
            resolved = await bot_api_request("getChat", {"chat_id": chat})
            log.info("%s resolved to %s", label, resolved["id"])
            if label == "log group":
                LOG_CHAT_ID = int(resolved["id"])
        except Exception:
            log.exception("Could not resolve %s (%s)", label, chat)
    await send_log("🟢 <b>Learn-X PDF bot started</b>")


async def main():
    from pyrogram import idle
    from webapp import start_web_server

    await app.start()
    await startup_checks()
    web_runner = None
    try:
        web_runner = await start_web_server()
    except Exception:
        log.exception("Mini app web server failed to start; continuing bot-only")
    log.info("Bot is running")
    await idle()
    if web_runner:
        await web_runner.cleanup()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
