import json
import os
from dataclasses import dataclass, field


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _chat_ref(name: str):
    value = _required(name)
    if value.lstrip("-").isdigit():
        return int(value)
    return value if value.startswith("@") else f"@{value}"


def _id_set(name: str) -> set[int]:
    value = _required(name)
    return {int(part.strip()) for part in value.split(",") if part.strip()}


def _tutor_seed() -> list[dict]:
    raw = os.getenv("TUTOR_GROUPS", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"TUTOR_GROUPS is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("TUTOR_GROUPS must be a JSON list of tutor group objects")
    seed = []
    for item in data:
        key = str(item.get("key", "")).strip().lower()
        if not key:
            continue
        seed.append(
            {
                "key": key,
                "display_name": str(item.get("display_name") or key),
                "group_ref": item.get("group_id"),
                "invite_url": str(item.get("invite_url") or ""),
            }
        )
    return seed


def _discussions() -> dict:
    """Map tutor key -> discussion message refs (IDs and/or URLs).

    Example: {"ap": "123, 456", "sd": "https://t.me/Learn_X_Edu/789"}
    A bare number is a message ID in the main channel to forward;
    a t.me URL is sent as a link.
    """
    raw = os.getenv("DISCUSSIONS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in data.items() if str(v).strip()}


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    admin_ids: set[int]
    main_channel: object
    main_channel_url: str
    public_group: object
    public_group_url: str
    private_group: object
    log_group: object
    data_dir: str
    mongodb_uri: str
    session_name: str
    protect_content: bool
    app_url: str
    panel_password: str
    discussions: dict
    tutor_seed: list = field(default_factory=list)


def load_settings() -> Settings:
    data_dir = os.getenv("DATA_DIR", "/tmp").strip() or "/tmp"
    os.makedirs(data_dir, exist_ok=True)
    return Settings(
        api_id=int(_required("API_ID")),
        api_hash=_required("API_HASH"),
        bot_token=_required("BOT_TOKEN"),
        admin_ids=_id_set("ADMIN_IDS"),
        main_channel=_chat_ref("MAIN_CHANNEL_ID"),
        main_channel_url=os.getenv(
            "MAIN_CHANNEL_URL", "https://t.me/Learn_X_Edu"
        ).strip(),
        public_group=_chat_ref("PUBLIC_GROUP_ID"),
        public_group_url=os.getenv(
            "PUBLIC_GROUP_URL", "https://t.me/Learn_X_discussion_grp"
        ).strip(),
        private_group=_chat_ref("PRIVATE_GROUP_ID"),
        log_group=_chat_ref("LOG_GROUP_ID"),
        data_dir=data_dir,
        mongodb_uri=_required("MONGODB_URI"),
        # ":memory:" keeps the Pyrogram session in RAM: Heroku has no
        # persistent disk, and all persistent data lives in MongoDB.
        session_name=os.getenv("SESSION_NAME", ":memory:").strip() or ":memory:",
        protect_content=os.getenv("PROTECT_CONTENT", "false").lower()
        in {"1", "true", "yes", "on"},
        # Public HTTPS URL of this deployment, e.g. https://your-app.herokuapp.com
        # Required for the Telegram Mini App button (/app). Must match the
        # certificate Telegram expects (Heroku's herokuapp.com domain is fine).
        app_url=os.getenv("APP_URL", "").strip(),
        # Password for the web admin panel (/panel). Empty disables the panel.
        panel_password=os.getenv("PANEL_PASSWORD", "").strip(),
        discussions=_discussions(),
        tutor_seed=_tutor_seed(),
    )


PDF_CAPTION = """━━━━━━━━━━━━━━
Unauthorized sharing is prohibited.

Updates: @Learn_X_Edu
Discussion: @Learn_X_discussion_grp
━━━━━━━━━━━━━━"""
