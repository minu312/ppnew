"""Functional test for the mini app web server (mocked bot backend).

Run: python tests/test_webapp_api.py
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer
import webapp

# ---------- build a fake bot module ("app") ----------

fake_app = types.ModuleType("app")

class FakeDB:
    def __init__(self):
        self.renamed = None
        self.deleted = None
    def is_banned(self, uid):
        return uid == 999
    def upsert_user(self, uid, username, first_name, last_name):
        return None
    def search_files(self, query, limit, offset=0):
        if "sd" in query:
            return 1, [{"id": 1, "display_name": "SD paper 03.pdf", "search_name": "sd paper 03.pdf", "folder": "sd"}]
        return 0, []
    def list_tutors(self):
        return [{"key": "sd", "display_name": "Sashanka Danujaya", "group_ref": "@sd_papers",
                 "invite_url": "https://t.me/sd_papers"}]
    def get_tutor(self, key):
        return {"key": "sd", "display_name": "Sashanka Danujaya", "group_ref": "@sd_papers",
                "invite_url": "https://t.me/sd_papers"} if key == "sd" else None
    def files_for_tutor(self, key):
        return [{"id": 1, "display_name": "SD paper 03.pdf", "search_name": "sd paper 03.pdf", "folder": "sd"}]
    def get_file(self, file_id):
        if file_id != 1:
            return None
        return {"id": 1, "display_name": "SD paper 03.pdf", "search_name": "sd paper 03.pdf",
                "telegram_file_id": "AAA", "file_size": 1024, "folder": "sd"}
    def tutor_for_file(self, rec):
        return self.get_tutor("sd")
    def is_admin(self, uid):
        return uid == 8094431666
    def rename_file(self, exact, new_display, new_search):
        self.renamed = (exact, new_display, new_search)
        return 1
    def delete_file(self, exact):
        self.deleted = exact
        return 1
    def log_visit(self, user, ip, platform, browser):
        self.visit_stored = (user.id, ip, platform, browser)
    def recent_visits(self, limit=50):
        return [{"user_id": 1, "username": "", "first_name": "A", "last_name": "",
                 "ip": "1.2.3.4", "platform": "android", "browser": "UA", "created_at": "2026-01-01"}]
    def recent_deliveries(self, limit=20):
        return [{"user_id": 1, "username": "u", "first_name": "A", "last_name": "",
                 "trace_id": "LX-AAAA", "visible_code": "123", "status": "sent", "created_at": "2026-01-01"}]
    def recent_messages(self, limit=200):
        return [{"user_id": 1, "username": "u", "first_name": "A", "last_name": "",
                 "message": "sd paper", "created_at": "2026-01-01"}]
    def stats(self):
        return {"files": 1, "users": 2, "deliveries": 3, "successful": 2, "banned": 0, "tutors": 1}

fake_app.db = FakeDB()

class FakeSettings:
    main_channel_url = "https://t.me/Learn_X_Edu"
    public_group_url = "https://t.me/Learn_X_discussion_grp"
    panel_password = "sekret"
    discussions = {"sd": "123", "ap": "https://t.me/Learn_X_Edu/456"}

fake_app.settings = FakeSettings()
fake_app.logs = []

async def sync_user(user):
    pass

async def access_status(uid):
    if uid == 777:  # missing everything
        return {"main": False, "public": False, "private": False}
    if uid == 888:  # joined channel+group but not private
        return {"main": True, "public": True, "private": False}
    return {"main": True, "public": True, "private": True}

async def membership(chat_ref, uid):
    if uid == 777:
        return False  # joined nothing at all
    if chat_ref == "@sd_papers":
        return uid != 555  # 555 is not in the tutor group
    return True

async def mention(user):
    return f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"

def normalize_query(v):
    return " ".join((v or "").strip().lower().split())

async def send_log(text):
    fake_app.logs.append(text)

async def deliver_pdf_to_user(user, file_record, query, source="Bot"):
    fake_app.logs.append(f"DELIVERED source={source} file={file_record['display_name']} uid={user.id}")

fake_app.sync_user = sync_user
fake_app.access_status = access_status
fake_app.membership = membership
fake_app.mention = mention
fake_app.normalize_query = normalize_query
fake_app.send_log = send_log
fake_app.deliver_pdf_to_user = deliver_pdf_to_user

async def send_discussion_messages(user_id, key):
    fake_app.logs.append(f"DISCUSSIONS uid={user_id} key={key}")
    return key in fake_app.settings.discussions, ""

fake_app.send_discussion_messages = send_discussion_messages
sys.modules["app"] = fake_app

# ---------- tests ----------

async def run():
    failures = []
    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f"  {detail}" if (detail and not cond) else ""))
        if not cond:
            failures.append(name)

    client = TestClient(TestServer(webapp.make_app()))
    await client.start_server()

    # 1. root + miniapp html
    r = await client.get("/")
    check("GET / returns 200", r.status == 200)
    r = await client.get("/miniapp")
    body = await r.text()
    check("GET /miniapp returns 200", r.status == 200)
    check("HTML contains Learn-X branding", "Learn-X" in body)
    check("HTML calls logVisit", "logVisit" in body)
    check("HTML JS split regex intact", r"/\s+/" in body.replace("\r", ""))
    check("HTML has escaped onclick quotes", "\\'" in body)
    check("HTML loads telegram-web-app.js", "telegram-web-app.js" in body)

    # 2. tutors
    r = await client.get("/api/tutors")
    data = await r.json()
    check("GET /api/tutors lists tutors", data["tutors"] and data["tutors"][0]["key"] == "sd" and data["tutors"][0]["display_name"] == "Sashanka Danujaya")

    # 3. tutor papers
    r = await client.get("/api/tutor-papers?key=sd")
    data = await r.json()
    check("GET /api/tutor-papers returns files", data["files"] and data["files"][0]["file_name"] == "SD paper 03.pdf")

    # 4. search
    r = await client.get("/api/search?q=sd")
    data = await r.json()
    check("GET /api/search finds files", data["files"] and data["files"][0]["file_name"] == "SD paper 03.pdf")
    r = await client.get("/api/search?q=")
    data = await r.json()
    check("GET /api/search rejects empty query", r.status == 200 and data.get("error"))

    # 5. verify_sub variants
    r = await client.get("/api/verify_sub?user_id=123")
    check("verify_sub subscribed user", (await r.json())["subscribed"] is True)
    r = await client.get("/api/verify_sub?user_id=777")
    data = await r.json()
    check("verify_sub unsubscribed gets join URLs", data["subscribed"] is False and data.get("channel_url") and data.get("group_url"))
    r = await client.get("/api/verify_sub?user_id=888")
    data = await r.json()
    check("verify_sub private-missing flagged, no links", data.get("private_required") is True and "channel_url" not in data and "group_url" not in data)
    r = await client.get("/api/verify_sub?user_id=999")
    check("verify_sub banned flag", (await r.json()).get("banned") is True)

    # 6. visit log (exact format)
    r = await client.post("/api/visit", json={
        "user_id": 6717692247, "username": "", "first_name": "Xenon", "last_name": "",
        "device": {"platform": "android", "userAgent": "Mozilla/5.0 (Linux; Android 11; K) Telegram-Android/12.10.6"},
    }, headers={"X-Forwarded-For": "123.231.86.43, 10.0.0.1"})
    data = await r.json()
    check("POST /api/visit ok", data.get("ok") is True)
    visit = fake_app.logs[-1]
    print("--- visit log ---")
    print(visit)
    print("-----------------")
    check("visit log title", visit.startswith("👀 <b>Mini App Visited</b>"))
    check("visit log user line", "User: Xenon" in visit)
    check("visit log username line", "Username: No username" in visit)
    check("visit log id line", "ID: <code>6717692247</code>" in visit)
    check("visit log device header", "🌐 <b>Device &amp; Network</b>" in visit)
    check("visit log ip from X-Forwarded-For", "IP: <code>123.231.86.43</code>" in visit)
    check("visit log platform", "Platform: <code>android</code>" in visit)
    check("visit log browser", "Telegram-Android/12.10.6" in visit)
    # dedupe
    await client.post("/api/visit", json={"user_id": 6717692247, "device": {"platform": "android"}})
    check("visit deduped within window", fake_app.logs[-1] == visit)

    # 7. download gating
    r = await client.post("/api/download", json={"file_id": 1, "user_id": 999, "file_name": "SD paper 03.pdf"})
    check("download banned", (await r.json())["error"] == "banned")
    r = await client.post("/api/download", json={"file_id": 1, "user_id": 777, "file_name": "SD paper 03.pdf"})
    check("download subscription_required", (await r.json())["error"] == "subscription_required")
    r = await client.post("/api/download", json={"file_id": 42, "user_id": 123, "file_name": "x.pdf"})
    check("download not_found", (await r.json())["error"] == "not_found")
    # tutor gate: user 555 is not an sd member
    r = await client.post("/api/download", json={"file_id": 1, "user_id": 555, "file_name": "SD paper 03.pdf"})
    data = await r.json()
    check("download tutor_group_required with invite", data["error"] == "tutor_group_required" and data["invite_url"] == "https://t.me/sd_papers")
    check("tutor block logged", any("Tutor group membership required" in t for t in fake_app.logs))
    # happy path: user 123 passes tutor membership? membership() returns uid not in (777,) or chat!=sd
    r = await client.post("/api/download", json={"file_id": 1, "user_id": 123, "file_name": "SD paper 03.pdf"})
    data = await r.json()
    check("download ok", data.get("ok") is True)
    check("delivery via shared function with Mini App source", any("DELIVERED source=Mini App" in t for t in fake_app.logs))

    # 8. admin endpoints
    r = await client.get("/api/check_admin?user_id=8094431666")
    check("check_admin true", (await r.json())["is_admin"] is True)
    r = await client.get("/api/check_admin?user_id=123")
    check("check_admin false", (await r.json())["is_admin"] is False)
    r = await client.post("/api/rename_file", json={"file_id": 1, "new_name": "SD Paper 03 v2.pdf", "user_id": 8094431666})
    data = await r.json()
    check("rename ok returns new name", data.get("ok") is True and data.get("file_name") == "SD Paper 03 v2.pdf")
    check("rename stored with normalized search", fake_app.db.renamed == ("sd paper 03.pdf", "SD Paper 03 v2.pdf", "sd paper 03 v2.pdf"))
    r = await client.post("/api/rename_file", json={"file_id": 1, "new_name": "x.pdf", "user_id": 123})
    check("rename unauthorized for non-admin", r.status == 403)
    r = await client.post("/api/delete_file", json={"file_id": 1, "user_id": 8094431666})
    check("delete ok", (await r.json()).get("ok") is True and fake_app.db.deleted == "sd paper 03.pdf")
    r = await client.post("/api/delete_file", json={"file_id": 1, "user_id": 123})
    check("delete unauthorized for non-admin", r.status == 403)

    # 9. tutors include image url + discussions flag
    r = await client.get("/api/tutors")
    data = await r.json()
    sd = [t for t in data["tutors"] if t["key"] == "sd"][0]
    check("tutor image url present", sd.get("image_url") == "/static/sd.jpg")
    check("tutor discussions flag", sd.get("discussions") is True)

    # 10. discussions send
    r = await client.post("/api/discussions/send", json={"user_id": 123, "tutor": "sd"})
    check("discussions send ok", (await r.json()).get("ok") is True)
    check("discussion forwarded", any("DISCUSSIONS uid=123 key=sd" in t for t in fake_app.logs))
    r = await client.post("/api/discussions/send", json={"user_id": 123, "tutor": "rk"})
    check("discussions unconfigured rejected", r.status == 404)
    r = await client.post("/api/discussions/send", json={"user_id": 777, "tutor": "sd"})
    check("discussions needs subscription", r.status == 403)

    # 11. panel auth
    r = await client.get("/panel")
    check("panel without key unauthorized", r.status == 401)
    r = await client.get("/panel?key=wrong")
    check("panel wrong key unauthorized", r.status == 401)
    r = await client.get("/panel?key=sekret")
    body = await r.text()
    check("panel with key renders", r.status == 200 and "Total Users" in body and "Recent Mini App Visits" in body)
    r = await client.get("/messages?key=sekret")
    body = await r.text()
    check("messages page renders", r.status == 200 and "Recent User Messages" in body)

    # 12. visit stored for panel
    check("visit stored in db", fake_app.db.visit_stored[0] == 6717692247 and fake_app.db.visit_stored[1] == "123.231.86.43")

    # 13. static images served
    r = await client.get("/static/sd.jpg")
    check("static tutor image served", r.status == 200)

    await client.close()
    print()
    if failures:
        print(f"FAILED: {len(failures)} -> {failures}")
        return 1
    print("ALL WEBAPP TESTS PASSED")
    return 0

sys.exit(asyncio.run(run()))
