"""MongoDB storage for the Learn-X PDF bot.

Replaces the previous SQLite layer. Collections: files, users, admins,
banned_users, deliveries, tutors, counters.
"""
import re
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, mongodb_uri: str, initial_admins: set[int]):
        self._client = MongoClient(mongodb_uri)
        self._db = self._client["learnx_pdf_bot"]
        self.files = self._db["files"]
        self.users = self._db["users"]
        self.admins = self._db["admins"]
        self.banned_users = self._db["banned_users"]
        self.deliveries = self._db["deliveries"]
        self.tutors = self._db["tutors"]
        self.counters = self._db["counters"]
        self.messages = self._db["messages"]
        self.visits = self._db["visits"]
        self.broadcasts = self._db["broadcasts"]

        self._create_indexes()
        for user_id in initial_admins:
            self.admins.update_one(
                {"user_id": user_id},
                {"$setOnInsert": {"user_id": user_id, "created_at": utc_now()}},
                upsert=True,
            )

    def _create_indexes(self):
        self.files.create_index(
            [("search_name", ASCENDING)], unique=True, name="uniq_search_name"
        )
        self.files.create_index("folder")
        self.users.create_index("user_id", unique=True)
        self.admins.create_index("user_id", unique=True)
        self.banned_users.create_index("user_id", unique=True)
        self.deliveries.create_index("trace_id", unique=True)
        self.deliveries.create_index("user_id")
        self.tutors.create_index("key", unique=True)
        self.messages.create_index("created_at")
        self.visits.create_index("created_at")
        self.broadcasts.create_index("broadcast_id")

    def _next_file_id(self) -> int:
        doc = self.counters.find_one_and_update(
            {"_id": "files"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(doc["seq"])

    # ---------------- admins ----------------

    def is_admin(self, user_id: int) -> bool:
        return self.admins.find_one({"user_id": user_id}, {"_id": 1}) is not None

    def add_admin(self, user_id: int):
        self.admins.update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id, "created_at": utc_now()}},
            upsert=True,
        )

    def remove_admin(self, user_id: int):
        self.admins.delete_one({"user_id": user_id})

    def list_admins(self):
        return [doc["user_id"] for doc in self.admins.find({}, sort=[("user_id", ASCENDING)])]

    # ---------------- bans ----------------

    def is_banned(self, user_id: int) -> bool:
        return self.banned_users.find_one({"user_id": user_id}, {"_id": 1}) is not None

    def ban(self, user_id: int, reason: str = ""):
        self.banned_users.update_one(
            {"user_id": user_id},
            {
                "$set": {"reason": reason, "created_at": utc_now()},
                "$setOnInsert": {"user_id": user_id},
            },
            upsert=True,
        )

    def unban(self, user_id: int):
        self.banned_users.delete_one({"user_id": user_id})

    # ---------------- users ----------------

    def upsert_user(self, user_id: int, username: str, first_name: str, last_name: str):
        now = utc_now()
        old = self.users.find_one({"user_id": user_id})
        self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                    "last_seen": now,
                },
                "$setOnInsert": {"user_id": user_id, "first_seen": now},
            },
            upsert=True,
        )
        return old

    def all_user_ids(self):
        return [doc["user_id"] for doc in self.users.find({}, {"user_id": 1})]

    # ---------------- files ----------------

    def add_file(
        self,
        display_name: str,
        search_name: str,
        telegram_file_id: str,
        telegram_file_unique_id: str,
        file_size: int,
        uploaded_by: int,
    ) -> int:
        file_id = self._next_file_id()
        self.files.insert_one(
            {
                "id": file_id,
                "display_name": display_name,
                "search_name": search_name,
                "telegram_file_id": telegram_file_id,
                "telegram_file_unique_id": telegram_file_unique_id,
                "file_size": file_size,
                "folder": "",
                "tutor": None,
                "active": True,
                "uploaded_by": uploaded_by,
                "uploaded_at": utc_now(),
            }
        )
        return file_id

    def get_file(self, file_id: int):
        doc = self.files.find_one({"id": file_id, "active": True})
        if doc:
            doc.pop("_id", None)
        return doc

    def search_files(self, query: str, limit: int, offset: int = 0):
        pattern = re.escape(query)
        flt = {
            "active": True,
            "search_name": {"$regex": pattern, "$options": "i"},
        }
        total = int(self.files.count_documents(flt))
        cursor = self.files.find(flt, sort=[("id", DESCENDING)]).skip(offset).limit(limit)
        rows = []
        for doc in cursor:
            doc.pop("_id", None)
            rows.append(doc)
        return total, rows

    def delete_file(self, exact_search_name: str) -> int:
        doc = self.files.find_one({"active": True, "search_name": exact_search_name})
        if not doc:
            return 0
        result = self.files.update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "active": False,
                    "search_name": f"{doc['search_name']}#deleted#{doc['id']}",
                }
            },
        )
        return int(result.modified_count)

    def rename_file(self, exact_search_name: str, new_display: str, new_search: str):
        result = self.files.update_one(
            {"active": True, "search_name": exact_search_name},
            {"$set": {"display_name": new_display, "search_name": new_search}},
        )
        return int(result.modified_count)

    def move_file(self, exact_search_name: str, folder: str):
        result = self.files.update_one(
            {"active": True, "search_name": exact_search_name},
            {"$set": {"folder": folder}},
        )
        return int(result.modified_count)

    def set_file_tutor(self, exact_search_name: str, tutor_key):
        """Assign a file to a tutor (None/'none' clears the assignment)."""
        result = self.files.update_one(
            {"active": True, "search_name": exact_search_name},
            {"$set": {"tutor": tutor_key}},
        )
        return int(result.modified_count)

    def folder_items(self, path: str):
        prefix = f"{path}/" if path else ""
        files = []
        for doc in self.files.find({"active": True, "folder": path}).sort(
            "display_name", ASCENDING
        ):
            doc.pop("_id", None)
            files.append(doc)
        children = set()
        for doc in self.files.find(
            {"active": True, "folder": {"$regex": f"^{re.escape(prefix)}"}},
            {"folder": 1},
        ):
            folder = doc["folder"]
            if folder == path:
                continue
            remainder = folder[len(prefix):]
            if remainder:
                children.add(remainder.split("/", 1)[0])
        return sorted(children), files

    # ---------------- tutor groups ----------------

    def list_tutors(self):
        return [
            {
                "key": doc["key"],
                "display_name": doc["display_name"],
                "group_ref": doc["group_ref"],
                "invite_url": doc.get("invite_url", ""),
            }
            for doc in self.tutors.find({}, sort=[("key", ASCENDING)])
        ]

    def get_tutor(self, key: str):
        key = (key or "").strip().lower()
        if not key:
            return None
        doc = self.tutors.find_one({"key": key})
        if doc:
            doc.pop("_id", None)
        return doc

    def add_tutor(self, key: str, display_name: str, group_ref, invite_url: str):
        key = key.strip().lower()
        self.tutors.update_one(
            {"key": key},
            {
                "$set": {
                    "key": key,
                    "display_name": display_name,
                    "group_ref": group_ref,
                    "invite_url": invite_url,
                }
            },
            upsert=True,
        )
        return key

    def remove_tutor(self, key: str) -> int:
        result = self.tutors.delete_one({"key": key.strip().lower()})
        return int(result.deleted_count)

    def tutor_for_file(self, file_record: dict):
        """Resolve the tutor group guarding a file.

        Priority:
        1. explicit tutor assignment (/setutor);
        2. the top-level folder name matching a tutor key;
        3. the tutor key appearing as a word in the filename
           (e.g. "SD paper 03.pdf" -> "sd"), so naming a file with a
           tutor's abbreviation is enough to guard it.
        """
        explicit = file_record.get("tutor")
        if explicit:
            return self.get_tutor(explicit)
        folder = (file_record.get("folder") or "").strip()
        if folder:
            tutor = self.get_tutor(folder.split("/", 1)[0])
            if tutor:
                return tutor
        name = (file_record.get("search_name") or "").lower()
        if name:
            for tutor in self.tutors.find({}, sort=[("key", ASCENDING)]):
                key = tutor["key"]
                if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", name):
                    tutor.pop("_id", None)
                    return tutor
        return None

    def files_for_tutor(self, key: str, limit: int = 200):
        """All active files guarded by / belonging to a tutor.

        Mirrors tutor_for_file's matching rules: explicit assignment,
        top-level folder name, or the tutor key appearing as a word in
        the filename.
        """
        tutor = self.get_tutor(key)
        if not tutor:
            return []
        key = tutor["key"]
        pattern = rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])"
        flt = {
            "active": True,
            "$or": [
                {"tutor": key},
                {"folder": {"$regex": rf"^{re.escape(key)}(/|$)"}},
                {"search_name": {"$regex": pattern, "$options": "i"}},
            ],
        }
        rows = []
        for doc in self.files.find(flt, sort=[("display_name", ASCENDING)]).limit(limit):
            doc.pop("_id", None)
            rows.append(doc)
        return rows

    # ---------------- deliveries / traces ----------------

    def create_delivery(
        self,
        trace_id: str,
        file_id: int,
        user,
        visible_code: str,
        requested_query: str,
    ):
        self.deliveries.insert_one(
            {
                "trace_id": trace_id,
                "file_id": file_id,
                "user_id": user.id,
                "visible_code": visible_code,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "requested_query": requested_query,
                "status": "processing",
                "error": "",
                "telegram_message_id": None,
                "created_at": utc_now(),
            }
        )

    def finish_delivery(self, trace_id: str, status: str, message_id=None, error: str = ""):
        self.deliveries.update_one(
            {"trace_id": trace_id},
            {"$set": {"status": status, "telegram_message_id": message_id, "error": error[:1000]}},
        )

    def find_trace(self, trace_id: str):
        doc = self.deliveries.find_one({"trace_id": trace_id})
        if not doc:
            return None
        doc.pop("_id", None)
        file_doc = self.files.find_one({"id": doc.get("file_id")}, {"display_name": 1})
        doc["display_name"] = (file_doc or {}).get("display_name", "")
        return doc

    def count_user_deliveries(self, user_id: int, status: str = "sent") -> int:
        return int(
            self.deliveries.count_documents({"user_id": user_id, "status": status})
        )

    # ---------------- stats ----------------

    # ---------------- messages / visits / broadcasts ----------------

    def log_message(self, user, text: str):
        """Store a user's search/message so the web panel can show it."""
        self.messages.insert_one(
            {
                "user_id": user.id,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "message": (text or "")[:2000],
                "created_at": utc_now(),
            }
        )

    def recent_messages(self, limit: int = 200):
        return [
            {
                "user_id": doc["user_id"],
                "username": doc.get("username", ""),
                "first_name": doc.get("first_name", ""),
                "last_name": doc.get("last_name", ""),
                "message": doc.get("message", ""),
                "created_at": doc.get("created_at", ""),
            }
            for doc in self.messages.find({}, sort=[("_id", DESCENDING)]).limit(limit)
        ]

    def log_visit(self, user, ip: str, platform: str, browser: str):
        """Store a mini app visit for the web panel."""
        self.visits.insert_one(
            {
                "user_id": user.id,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "ip": (ip or "")[:64],
                "platform": (platform or "")[:64],
                "browser": (browser or "")[:400],
                "created_at": utc_now(),
            }
        )

    def recent_visits(self, limit: int = 50):
        return [
            {
                "user_id": doc["user_id"],
                "username": doc.get("username", ""),
                "first_name": doc.get("first_name", ""),
                "last_name": doc.get("last_name", ""),
                "ip": doc.get("ip", ""),
                "platform": doc.get("platform", ""),
                "browser": doc.get("browser", ""),
                "created_at": doc.get("created_at", ""),
            }
            for doc in self.visits.find({}, sort=[("_id", DESCENDING)]).limit(limit)
        ]

    def log_broadcast(self, broadcast_id: str, user_id: int, message_id: int):
        self.broadcasts.insert_one(
            {
                "broadcast_id": broadcast_id,
                "user_id": user_id,
                "message_id": message_id,
                "created_at": utc_now(),
            }
        )

    def broadcast_targets(self, broadcast_id: str):
        return list(self.broadcasts.find({"broadcast_id": broadcast_id}))

    def clear_broadcast(self, broadcast_id: str) -> int:
        result = self.broadcasts.delete_many({"broadcast_id": broadcast_id})
        return int(result.deleted_count)

    def recent_deliveries(self, limit: int = 20):
        rows = []
        for doc in self.deliveries.find({}, sort=[("_id", DESCENDING)]).limit(limit):
            rows.append(
                {
                    "user_id": doc.get("user_id"),
                    "username": doc.get("username", ""),
                    "first_name": doc.get("first_name", ""),
                    "last_name": doc.get("last_name", ""),
                    "file_id": doc.get("file_id"),
                    "trace_id": doc.get("trace_id", ""),
                    "visible_code": doc.get("visible_code", ""),
                    "status": doc.get("status", ""),
                    "created_at": doc.get("created_at", ""),
                    "error": doc.get("error", ""),
                }
            )
        return rows

    def clear_files(self) -> int:
        """Deactivate every file record (used by /confirmclear)."""
        result = self.files.update_many(
            {"active": True},
            {
                "$set": {"active": False},
                "$currentDate": {"deleted_at": True},
            },
        )
        return int(result.modified_count)

    def stats(self):
        files_active = int(self.files.count_documents({"active": True}))
        deliveries = int(self.deliveries.count_documents({}))
        successful = int(self.deliveries.count_documents({"status": "sent"}))
        return {
            "files": files_active,
            "users": int(self.users.count_documents({})),
            "deliveries": deliveries,
            "successful": successful,
            "banned": int(self.banned_users.count_documents({})),
            "tutors": int(self.tutors.count_documents({})),
        }
