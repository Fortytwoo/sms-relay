from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from central_auth import (
    CentralAuthError,
    CentralAuthInvalidGrant,
    CentralAuthRejected,
    CentralAuthUnavailable,
    CentralOAuthClient,
    validate_introspection,
    validate_token_response,
)


MAX_BODY_BYTES = 64 * 1024
MAX_CONTENT_CHARS = 32 * 1024
SESSION_COOKIE_NAME = "sms_relay_session"
OAUTH_TRANSACTION_COOKIE_NAME = "sms_relay_oauth_tx"
SESSION_IDLE_SECONDS = 12 * 60 * 60
SESSION_ABSOLUTE_SECONDS = 7 * 24 * 60 * 60
OAUTH_STATE_SECONDS = 5 * 60
MAX_OAUTH_TRANSACTIONS = 2048
REFRESH_SKEW_SECONDS = 60
MESSAGE_DEDUPLICATION_WINDOW_SECONDS = 60
WEB_ROOT = Path(__file__).with_name("web")
STATIC_FILES = {
    "/copy": ("copy.html", "text/html; charset=utf-8", "no-store"),
    "/assets/copy.js": ("copy.js", "text/javascript; charset=utf-8", "no-cache"),
    "/assets/copy.css": ("copy.css", "text/css; charset=utf-8", "no-cache"),
    "/": ("index.html", "text/html; charset=utf-8", "no-cache"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8", "public, max-age=3600"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8", "public, max-age=3600"),
    "/assets/login-visual.svg": ("login-visual.svg", "image/svg+xml", "public, max-age=3600"),
}

_CODE_TOKEN = (
    r"(?<![A-Z0-9])"
    r"((?=[A-Z0-9]{4,8}(?![A-Z0-9]))(?=[A-Z0-9]*\d)[A-Z0-9]{4,8})"
    r"(?![A-Z0-9])"
)
_CODE_KEYWORD = (
    r"(?:验证码|校验码|动态码|短信码|一次性密码|解压密码|"
    r"verification\s*code|security\s*code|one[-\s]*time\s*password|otp)"
)
_ARCHIVE_PASSWORD_KEYWORD = (
    r"(?:(?:文件)?提取码|解压(?:缩)?密码|压缩(?:包|文件)?(?:的)?密码|"
    r"(?:导出|下载)文件(?:的)?(?:解压)?密码)"
)
_ARCHIVE_PASSWORD_TOKEN = (
    r"(?<![A-Z0-9])"
    r"([A-Z0-9]{4,32})"
    r"(?![A-Z0-9])"
)
_CODE_PATTERNS = (
    re.compile(
        rf"{_ARCHIVE_PASSWORD_KEYWORD}\s*"
        rf"(?:(?:是|为|is|[:：=,，-])\s*){{0,3}}{_ARCHIVE_PASSWORD_TOKEN}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_ARCHIVE_PASSWORD_TOKEN}\s*(?:是|为|is)?\s*"
        rf"(?:您的|本次|文件的)?\s*{_ARCHIVE_PASSWORD_KEYWORD}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_CODE_KEYWORD}\s*(?:(?:是|为|为您|is|[:：=,，-])\s*){{0,3}}{_CODE_TOKEN}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_CODE_TOKEN}\s*(?:是|为|is)?\s*(?:您的|本次|your)?\s*(?:快手)?\s*{_CODE_KEYWORD}",
        re.IGNORECASE,
    ),
)
_SIM_SLOT_PATTERN = re.compile(r"(?:SIM|卡)\s*([12])", re.IGNORECASE)
_MOBILE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[\s_-]?)?(1[3-9]\d{9})(?!\d)")
_MESSAGE_TAG_PATTERN = re.compile(r"【([^【】]*)】")
_PLATFORM_HOST_TAGS = {
    "ark.xiaohongshu.com": "小红书",
    "s.kwaixiaodian.com": "快手",
    "zhaoshang.dxycare.com": "丁香",
    "portal.maiscrm.com": "私域商城",
    "store.weixin.qq.com": "微信小店",
    "fxg.jinritemai.com": "抖音商城",
    "doudian.douyinec.com": "抖音商城",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_verification_code(content: str) -> str:
    for pattern in _CODE_PATTERNS:
        match = pattern.search(content)
        if match:
            return match.group(1)
    return ""


_EMAIL_VALIDATION_ACTION = re.compile(
    r"(?:输入|使用)\s*(?:以下|下列)\s*(?:验证代码|验证码|代码|口令)\s*"
    r"(?:以)?\s*(?:完成|进行)?\s*(?:邮箱|身份|登录|安全)?\s*(?:验证|认证)\s*[:：]"
)
_EMAIL_CODE_LINE = re.compile(r"([A-Za-z0-9]{4,8})[。.!！]?")


def extract_email_verification_code(subject: str, content: str) -> str:
    """Accept a unique contextual code; do not scan arbitrary numbers in email."""
    text = (subject + "\n" + content).replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    candidates = {match.group(1) for pattern in _CODE_PATTERNS for match in pattern.finditer(text)}
    for marker in _EMAIL_VALIDATION_ACTION.finditer(text):
        line = next((line.strip() for line in text[marker.end():].split("\n") if line.strip()), "")
        match = _EMAIL_CODE_LINE.fullmatch(line)
        if match:
            candidates.add(match.group(1))
    return next(iter(candidates)) if len(candidates) == 1 else ""


def parse_sim_info(sim_info: str) -> tuple[str, str]:
    slot_match = _SIM_SLOT_PATTERN.search(sim_info or "")
    phone_match = _MOBILE_PATTERN.search(sim_info or "")
    slot = f"SIM{slot_match.group(1)}" if slot_match else ""
    phone = phone_match.group(1) if phone_match else ""
    return slot, phone


def extract_message_tag(content: str) -> str:
    for match in _MESSAGE_TAG_PATTERN.finditer(content or ""):
        tag = " ".join(match.group(1).split())
        if tag:
            return tag
    return ""


def identify_platform(url: str) -> str:
    try:
        parsed = urlsplit((url or "").strip())
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return _PLATFORM_HOST_TAGS.get(host, "")


def enrich_message(row: dict[str, Any]) -> dict[str, Any]:
    message = dict(row)
    sim_slot, sim_phone = parse_sim_info(str(message.get("sim_info", "")))
    text = str(message.get("content", ""))
    if message.get("message_type") == "email":
        text = str(message.get("subject", "")) + "\n" + text
    message["verification_code"] = (
        extract_email_verification_code(str(message.get("subject", "")), str(message.get("content", "")))
        if message.get("message_type") == "email" else extract_verification_code(text)
    )
    message["tag"] = extract_message_tag(text)
    message["sim_slot"] = sim_slot
    message["sim_phone"] = sim_phone
    return message


def connect_db(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


@contextmanager
def open_db(db_path: str):
    connection = connect_db(db_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                message_type TEXT NOT NULL,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                source_received_at TEXT NOT NULL,
                sim_info TEXT NOT NULL,
                device_name TEXT NOT NULL,
                app_version TEXT NOT NULL,
                message_key TEXT NOT NULL UNIQUE,
                source_ip TEXT NOT NULL,
                lark_push_status TEXT NOT NULL DEFAULT 'skipped',
                lark_push_attempts INTEGER NOT NULL DEFAULT 0,
                lark_pushed_at TEXT NOT NULL DEFAULT '',
                lark_push_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_transactions (
                state_hash TEXT PRIMARY KEY,
                browser_hash TEXT NOT NULL,
                code_verifier TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_sessions (
                session_hash TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                access_expires_at INTEGER NOT NULL,
                principal_json TEXT NOT NULL,
                validated_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                idle_expires_at INTEGER NOT NULL,
                absolute_expires_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_oauth_sessions_expiry "
            "ON oauth_sessions(absolute_expires_at, idle_expires_at)"
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        migrations = {
            "recipient": "TEXT NOT NULL DEFAULT ''",
            "subject": "TEXT NOT NULL DEFAULT ''",
            "source_message_id": "TEXT NOT NULL DEFAULT ''",
            "lark_push_status": "TEXT NOT NULL DEFAULT 'skipped'",
            "lark_push_attempts": "INTEGER NOT NULL DEFAULT 0",
            "lark_pushed_at": "TEXT NOT NULL DEFAULT ''",
            "lark_push_error": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in migrations.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at DESC)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_source_identity
            ON messages(message_type, sender, source_received_at)
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_email_identity "
            "ON messages(message_type, recipient, source_message_id)"
        )


def parse_message(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")

    aliases = {
        "message_type": ("type", "message_type"),
        "sender": ("from", "sender"),
        "content": ("content", "message"),
        "source_received_at": ("received_at", "source_received_at"),
        "sim_info": ("sim_info", "card_slot"),
        "device_name": ("device_name", "device"),
        "app_version": ("app_version",),
        "recipient": ("recipient", "to"),
        "subject": ("subject",),
        "source_message_id": ("source_message_id",),
    }

    result: dict[str, str] = {}
    for target, candidates in aliases.items():
        value: Any = ""
        for candidate in candidates:
            if candidate in payload:
                value = payload[candidate]
                break
        if value is None:
            value = ""
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"{target} must be a scalar value")
        result[target] = str(value).strip()

    if not result["content"] and not (result["message_type"] == "email" and result["subject"]):
        raise ValueError("content is required")
    if len(result["content"]) > MAX_CONTENT_CHARS:
        raise ValueError("content is too long")

    limits = {
        "message_type": 32,
        "sender": 512,
        "source_received_at": 128,
        "sim_info": 1024,
        "device_name": 256,
        "app_version": 64,
        "recipient": 320,
        "subject": 2048,
        "source_message_id": 1024,
    }
    for field, limit in limits.items():
        if len(result[field]) > limit:
            raise ValueError(f"{field} is too long")

    if not result["message_type"]:
        result["message_type"] = "sms"
    if result["message_type"] == "email":
        from mail_receiver import normalize_address

        result["recipient"] = normalize_address(result["recipient"])
        if not result["source_message_id"]:
            raise ValueError("source_message_id is required for email")
    else:
        # Email identity must never affect the existing SMS deduplication contract.
        result.update(recipient="", subject="", source_message_id="")
    return result


def fingerprint(message: dict[str, str]) -> str:
    if message["message_type"] == "email":
        identity = ["email", message["recipient"], message["source_message_id"]]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()
    canonical = json.dumps(
        {
            "message_type": message["message_type"],
            "sender": message["sender"],
            "content": message["content"],
            "source_received_at": message["source_received_at"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_source_received_at(value: str) -> datetime | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _source_timestamp_delta_seconds(left: str, right: str) -> float | None:
    if left == right:
        return 0.0
    left_timestamp = _parse_source_received_at(left)
    right_timestamp = _parse_source_received_at(right)
    if left_timestamp is None or right_timestamp is None:
        return None
    left_aware = left_timestamp.tzinfo is not None
    right_aware = right_timestamp.tzinfo is not None
    if left_aware != right_aware:
        return None
    if left_aware:
        left_timestamp = left_timestamp.astimezone(timezone.utc)
        right_timestamp = right_timestamp.astimezone(timezone.utc)
    return abs((left_timestamp - right_timestamp).total_seconds())


def _find_existing_message(
    connection: sqlite3.Connection,
    message: dict[str, str],
) -> sqlite3.Row | None:
    if message["message_type"] == "email":
        return connection.execute(
            "SELECT id, message_key, lark_push_status FROM messages "
            "WHERE message_type = 'email' AND recipient = ? AND source_message_id = ?",
            (message["recipient"], message["source_message_id"]),
        ).fetchone()
    candidates = connection.execute(
        """
        SELECT id, source_received_at, message_key, lark_push_status
        FROM messages
        WHERE message_type = ? AND sender = ? AND content = ?
        """,
        (message["message_type"], message["sender"], message["content"]),
    ).fetchall()
    matches: list[tuple[float, int, sqlite3.Row]] = []
    for candidate in candidates:
        delta = _source_timestamp_delta_seconds(
            str(candidate["source_received_at"]),
            message["source_received_at"],
        )
        if delta is not None and delta <= MESSAGE_DEDUPLICATION_WINDOW_SECONDS:
            matches.append((delta, int(candidate["id"]), candidate))
    if not matches:
        return None
    return min(matches, key=lambda item: (item[0], item[1]))[2]


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    request_headers = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace")
        raise RuntimeError(f"upstream_http_{exc.code}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"upstream_request_failed: {type(exc).__name__}") from exc


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, *, request_interval: float = 5.0):
        self.app_id = app_id
        self.app_secret = app_secret
        self.request_interval = max(request_interval, 0.0)
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._last_request = 0.0

    def _wait_for_request_slot(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)

    def _record_request(self) -> None:
        self._last_request = time.monotonic()

    def tenant_access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires_at - 120:
                return self._token
            self._wait_for_request_slot()
            response = _json_request(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                method="POST",
                payload={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            self._record_request()
            if response.get("code") != 0 or not response.get("tenant_access_token"):
                raise RuntimeError(f"feishu_token_error_{response.get('code', 'unknown')}")
            self._token = str(response["tenant_access_token"])
            self._token_expires_at = time.time() + int(response.get("expire", 7200))
            return self._token

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._request_lock:
            token = self.tenant_access_token()
            self._wait_for_request_slot()
            url = f"https://open.feishu.cn{path}"
            if params:
                url += "?" + urlencode(params)
            response = _json_request(
                url,
                method=method,
                headers={"Authorization": f"Bearer {token}"},
                payload=payload,
                timeout=20,
            )
            self._record_request()
        if response.get("code") != 0:
            raise RuntimeError(
                f"feishu_api_error_{response.get('code', 'unknown')}: "
                f"{str(response.get('msg') or 'unknown')[:240]}"
            )
        return response

def build_verification_card(message: dict[str, Any], public_base_url: str = "https://api.midi.lizhijian.xyz/sms-relay/") -> dict[str, Any]:
    """Card 2.0: trusted code block and plain-text (not executable Markdown) metadata."""
    code = str(message.get("verification_code", ""))
    if not re.fullmatch(r"[A-Za-z0-9]{4,32}", code):
        raise ValueError("invalid_verification_code")
    is_email = message.get("message_type") == "email"
    receiver = (message.get("recipient") if is_email else
                message.get("sim_phone") or message.get("sim_slot")) or "未知"

    def plain(value: str) -> dict[str, Any]:
        return {"tag": "div", "text": {"tag": "plain_text", "content": value}}

    elements = [
        plain(f"{'接收邮箱' if is_email else '接收号码'}：{receiver}"),
        {"tag": "markdown", "content": f"```\n{code}\n```"},
    ]
    tag = str(message.get("tag") or "").strip()[:128]
    if tag:
        elements.append(plain(f"平台：{tag}"))
    elements.append(plain(f"来源：{str(message.get('sender') or '未知')[:512]}"))
    if is_email:
        elements.append(plain(f"邮件主题：{str(message.get('subject') or '无主题')[:2048]}"))
    received_at = str(message.get("source_received_at") or message.get("received_at") or "未知")
    try:
        parsed_time = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        if parsed_time.tzinfo is not None:
            from datetime import timedelta
            received_at = parsed_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S UTC+08:00")
    except ValueError:
        pass
    elements.append(plain(f"接收时间：{received_at}"))
    parsed_base = urlsplit(public_base_url)
    if parsed_base.scheme != "https" or not parsed_base.netloc or parsed_base.query or parsed_base.fragment or parsed_base.username:
        raise ValueError("card_copy_url_must_be_https")
    copy_url = public_base_url.rstrip("/") + "/copy?message_id=" + str(int(message["id"]))
    elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "复制验证码"},
                     "type": "primary", "behaviors": [{"type": "open_url", "default_url": copy_url}]})
    return {
        "schema": "2.0",
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "邮箱验证码" if is_email else "短信验证码"}},
        "body": {"elements": elements},
    }


class FeishuNotifier:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        chat_id: str,
        *,
        client: FeishuClient | None = None,
        public_base_url: str = "https://api.midi.lizhijian.xyz/sms-relay/",
    ):
        self.client = client or FeishuClient(app_id, app_secret)
        self.chat_id = chat_id
        self.public_base_url = public_base_url

    def send(self, message: dict[str, Any]) -> None:
        code = str(message.get("verification_code", ""))
        if not code:
            return
        self.client.request(
            "/open-apis/im/v1/messages",
            method="POST",
            params={"receive_id_type": "chat_id"},
            payload={
                "receive_id": self.chat_id,
                "msg_type": "interactive",
                "content": json.dumps(build_verification_card(message, self.public_base_url), ensure_ascii=False, separators=(",", ":")),
                "uuid": f"sms-relay-{message['id']}",
            },
        )


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        api_key: str,
        db_path: str,
        *,
        read_api_key: str,
        feishu_app_id: str = "",
        feishu_app_secret: str = "",
        feishu_chat_id: str = "",
        auth_issuer: str = "https://auth.midi.lizhijian.xyz",
        auth_client_id: str = "sms-relay-web",
        auth_audience: str = "sms-relay-api",
        auth_scopes: tuple[str, ...] = ("sms-relay:access",),
        auth_redirect_uri: str = "https://api.midi.lizhijian.xyz/sms-relay/auth/callback",
        auth_post_logout_redirect_uri: str = (
            "https://api.midi.lizhijian.xyz/sms-relay/?auto_sso=off"
        ),
        auth_backchannel_ip: str = "",
        public_cookie_path: str = "/sms-relay/",
        session_idle_seconds: int = SESSION_IDLE_SECONDS,
        session_absolute_seconds: int = SESSION_ABSOLUTE_SECONDS,
        introspection_cache_seconds: int = 5,
        notifier: FeishuNotifier | None = None,
        feishu_client: FeishuClient | None = None,
        auth_client: CentralOAuthClient | Any | None = None,
    ):
        if len(api_key) != 64:
            raise ValueError("SMS_RELAY_API_KEY must contain exactly 64 characters")
        if len(read_api_key) != 64:
            raise ValueError("SMS_RELAY_READ_API_KEY must contain exactly 64 characters")
        if hmac.compare_digest(api_key, read_api_key):
            raise ValueError("SMS_RELAY_READ_API_KEY must differ from SMS_RELAY_API_KEY")
        self.api_key = api_key
        self.read_api_key = read_api_key
        self.db_path = db_path
        self.feishu_app_id = feishu_app_id
        self.feishu_app_secret = feishu_app_secret
        self.public_cookie_path = public_cookie_path
        self.session_idle_seconds = max(int(session_idle_seconds), 60)
        self.session_absolute_seconds = max(
            int(session_absolute_seconds), self.session_idle_seconds
        )
        self.introspection_cache_seconds = max(int(introspection_cache_seconds), 0)
        self.auth_client = auth_client or CentralOAuthClient(
            issuer=auth_issuer,
            client_id=auth_client_id,
            audience=auth_audience,
            scopes=auth_scopes,
            redirect_uri=auth_redirect_uri,
            post_logout_redirect_uri=auth_post_logout_redirect_uri,
            backchannel_ip=auth_backchannel_ip,
        )
        self.auth_issuer = str(self.auth_client.issuer).rstrip("/")
        self.auth_client_id = str(self.auth_client.client_id)
        self.auth_audience = str(self.auth_client.audience)
        self.auth_scopes = tuple(self.auth_client.scopes)
        self.auth_redirect_uri = str(self.auth_client.redirect_uri)
        self.auth_post_logout_redirect_uri = str(
            self.auth_client.post_logout_redirect_uri
        )
        self.auth_success_uri = self._derive_success_uri(self.auth_redirect_uri)
        self.oauth_lock = threading.Lock()
        self.invalid_session_hashes: set[str] = set()
        self.notifier = notifier
        self.feishu_client = feishu_client
        if self.feishu_client is None and feishu_app_id and feishu_app_secret:
            self.feishu_client = FeishuClient(feishu_app_id, feishu_app_secret)
        if self.notifier is None and feishu_app_id and feishu_app_secret and feishu_chat_id:
            self.notifier = FeishuNotifier(
                feishu_app_id,
                feishu_app_secret,
                feishu_chat_id,
                client=self.feishu_client,
                public_base_url=self.auth_success_uri,
            )
        self.notification_lock = threading.Lock()
        self.notification_stop = threading.Event()
        self.notification_event = threading.Event()
        self.notification_thread: threading.Thread | None = None
        self.mail_receiver = None
        init_db(db_path)
        super().__init__(address, RelayHandler)
        if self.notifier is not None:
            self.notification_thread = threading.Thread(
                target=self._notification_loop,
                name="feishu-notification-worker",
                daemon=True,
            )
            self.notification_thread.start()
            self.notification_event.set()

    @staticmethod
    def _derive_success_uri(redirect_uri: str) -> str:
        parsed = urlsplit(redirect_uri)
        suffix = "/auth/callback"
        if not parsed.path.endswith(suffix):
            raise ValueError("AUTH_REDIRECT_URI must end with /auth/callback")
        path = parsed.path[: -len(suffix)].rstrip("/") + "/"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    @staticmethod
    def _handle_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _cookie_value(raw_cookie: str, name: str) -> str:
        try:
            cookie = SimpleCookie(raw_cookie)
            return str(cookie[name].value)
        except (KeyError, ValueError):
            return ""

    def session_set_cookie(self, handle: str) -> str:
        return (
            f"{SESSION_COOKIE_NAME}={handle}; Path={self.public_cookie_path}; "
            f"Max-Age={self.session_absolute_seconds}; HttpOnly; Secure; SameSite=Lax"
        )

    def session_clear_cookie(self) -> str:
        return (
            f"{SESSION_COOKIE_NAME}=; Path={self.public_cookie_path}; Max-Age=0; "
            "HttpOnly; Secure; SameSite=Lax"
        )

    def transaction_set_cookie(self, browser_binding: str) -> str:
        return (
            f"{OAUTH_TRANSACTION_COOKIE_NAME}={browser_binding}; "
            f"Path={self.public_cookie_path}; Max-Age={OAUTH_STATE_SECONDS}; "
            "HttpOnly; Secure; SameSite=Lax"
        )

    def transaction_clear_cookie(self) -> str:
        return (
            f"{OAUTH_TRANSACTION_COOKIE_NAME}=; Path={self.public_cookie_path}; "
            "Max-Age=0; HttpOnly; Secure; SameSite=Lax"
        )

    def create_oauth_transaction(self) -> tuple[str, str, str]:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        browser_binding = secrets.token_urlsafe(32)
        now = int(time.time())
        with open_db(self.db_path) as connection:
            connection.execute(
                "DELETE FROM oauth_transactions WHERE expires_at <= ?",
                (now,),
            )
            connection.execute(
                """
                INSERT INTO oauth_transactions (
                    state_hash, browser_hash, code_verifier, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._handle_hash(state),
                    self._handle_hash(browser_binding),
                    verifier,
                    now + OAUTH_STATE_SECONDS,
                    now,
                ),
            )
            connection.execute(
                """
                DELETE FROM oauth_transactions
                WHERE state_hash IN (
                    SELECT state_hash FROM oauth_transactions
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (MAX_OAUTH_TRANSACTIONS,),
            )
        return state, verifier, browser_binding

    def consume_oauth_transaction(self, state: str, browser_binding: str) -> str | None:
        if not state or not browser_binding:
            return None
        state_hash = self._handle_hash(state)
        browser_hash = self._handle_hash(browser_binding)
        now = int(time.time())
        with open_db(self.db_path) as connection:
            row = connection.execute(
                """
                DELETE FROM oauth_transactions
                WHERE state_hash = ? AND browser_hash = ? AND expires_at > ?
                RETURNING code_verifier
                """,
                (state_hash, browser_hash, now),
            ).fetchone()
            if row is None:
                return None
        return str(row["code_verifier"])

    def create_session(
        self,
        token: dict[str, Any],
        principal: dict[str, Any],
    ) -> str:
        handle = secrets.token_urlsafe(48)
        now = int(time.time())
        with open_db(self.db_path) as connection:
            connection.execute(
                """
                DELETE FROM oauth_sessions
                WHERE absolute_expires_at <= ? OR idle_expires_at <= ?
                """,
                (now, now),
            )
            connection.execute(
                """
                INSERT INTO oauth_sessions (
                    session_hash, access_token, refresh_token, access_expires_at,
                    principal_json, validated_at, created_at, last_seen_at,
                    idle_expires_at, absolute_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._handle_hash(handle),
                    str(token["access_token"]),
                    str(token["refresh_token"]),
                    now + int(token["expires_in"]),
                    json.dumps(principal, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                    now,
                    now + self.session_idle_seconds,
                    now + self.session_absolute_seconds,
                ),
            )
        return handle

    def resolve_session(self, raw_cookie: str) -> dict[str, Any] | None:
        handle = self._cookie_value(raw_cookie, SESSION_COOKIE_NAME)
        if not handle:
            return None
        session_hash = self._handle_hash(handle)
        required_scopes = set(self.auth_scopes)
        now = int(time.time())
        with self.oauth_lock:
            if session_hash in self.invalid_session_hashes:
                try:
                    with open_db(self.db_path) as connection:
                        connection.execute(
                            "DELETE FROM oauth_sessions WHERE session_hash = ?",
                            (session_hash,),
                        )
                except sqlite3.Error:
                    pass
                return None
            with open_db(self.db_path) as connection:
                row = connection.execute(
                    "SELECT * FROM oauth_sessions WHERE session_hash = ?",
                    (session_hash,),
                ).fetchone()
            if row is None:
                return None
            if now >= int(row["idle_expires_at"]) or now >= int(
                row["absolute_expires_at"]
            ):
                with open_db(self.db_path) as connection:
                    connection.execute(
                        "DELETE FROM oauth_sessions WHERE session_hash = ?",
                        (session_hash,),
                    )
                return None

            access_token = str(row["access_token"])
            refresh_token = str(row["refresh_token"])
            access_expires_at = int(row["access_expires_at"])
            validated_at = int(row["validated_at"])
            principal_json = str(row["principal_json"])
            token_update: dict[str, Any] | None = None

            if access_expires_at <= now + REFRESH_SKEW_SECONDS:
                try:
                    token_update = validate_token_response(
                        self.auth_client.refresh(refresh_token),
                        required_scopes,
                    )
                except CentralAuthInvalidGrant:
                    with open_db(self.db_path) as connection:
                        connection.execute(
                            "DELETE FROM oauth_sessions WHERE session_hash = ?",
                            (session_hash,),
                        )
                    return None
                except CentralAuthRejected as exc:
                    raise CentralAuthUnavailable("refresh_contract_failed") from exc
                access_token = str(token_update["access_token"])
                try:
                    with open_db(self.db_path) as connection:
                        cursor = connection.execute(
                            """
                            UPDATE oauth_sessions
                            SET access_token = ?, refresh_token = ?, access_expires_at = ?,
                                validated_at = 0
                            WHERE session_hash = ? AND refresh_token = ?
                            """,
                            (
                                access_token,
                                str(token_update["refresh_token"]),
                                now + int(token_update["expires_in"]),
                                session_hash,
                                refresh_token,
                            ),
                        )
                except sqlite3.Error as exc:
                    self.invalid_session_hashes.add(session_hash)
                    raise CentralAuthUnavailable("session_refresh_persist_failed") from exc
                if cursor.rowcount != 1:
                    self.invalid_session_hashes.add(session_hash)
                    raise CentralAuthUnavailable("session_refresh_conflict")

            if token_update is not None or now - validated_at >= self.introspection_cache_seconds:
                try:
                    principal = validate_introspection(
                        self.auth_client.introspect(access_token),
                        client_id=self.auth_client_id,
                        required_scopes=required_scopes,
                    )
                except CentralAuthRejected:
                    with open_db(self.db_path) as connection:
                        connection.execute(
                            "DELETE FROM oauth_sessions WHERE session_hash = ?",
                            (session_hash,),
                        )
                    return None
                principal_json = json.dumps(
                    principal, ensure_ascii=False, separators=(",", ":")
                )
                validated_at = now
            else:
                try:
                    principal = json.loads(principal_json)
                except json.JSONDecodeError:
                    with open_db(self.db_path) as connection:
                        connection.execute(
                            "DELETE FROM oauth_sessions WHERE session_hash = ?",
                            (session_hash,),
                        )
                    return None

            idle_expires_at = min(
                now + self.session_idle_seconds,
                int(row["absolute_expires_at"]),
            )
            with open_db(self.db_path) as connection:
                connection.execute(
                    """
                    UPDATE oauth_sessions
                    SET principal_json = ?, validated_at = ?, last_seen_at = ?,
                        idle_expires_at = ?
                    WHERE session_hash = ?
                    """,
                    (principal_json, validated_at, now, idle_expires_at, session_hash),
                )
        return principal

    def logout_session(self, raw_cookie: str) -> None:
        handle = self._cookie_value(raw_cookie, SESSION_COOKIE_NAME)
        if not handle:
            return
        session_hash = self._handle_hash(handle)
        with self.oauth_lock:
            with open_db(self.db_path) as connection:
                row = connection.execute(
                    "SELECT refresh_token FROM oauth_sessions WHERE session_hash = ?",
                    (session_hash,),
                ).fetchone()
            if row is None:
                return
            try:
                self.auth_client.revoke(str(row["refresh_token"]))
            except CentralAuthInvalidGrant:
                pass
            with open_db(self.db_path) as connection:
                connection.execute(
                    "DELETE FROM oauth_sessions WHERE session_hash = ?",
                    (session_hash,),
                )

    def _load_message(self, row_id: int) -> dict[str, Any] | None:
        with open_db(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT id, received_at, message_type, sender, content,
                       source_received_at, sim_info, device_name, app_version,
                       message_key, lark_push_status, lark_push_attempts,
                       lark_pushed_at, lark_push_error, recipient, subject, source_message_id
                FROM messages WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
        return enrich_message(dict(row)) if row else None

    def ingest_message(self, payload: Any, source_ip: str = "") -> dict[str, Any]:
        """Shared transactional ingestion for HTTP senders and mailbox polling."""
        message = parse_message(payload)
        enriched = enrich_message(message)
        message_key = fingerprint(message)
        has_code = bool(enriched["verification_code"])
        initial_status = "pending" if has_code and self.notifier else "disabled" if has_code else "skipped"
        with open_db(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            stored = _find_existing_message(connection, message)
            if stored is None:
                fields = ["message_type", "sender", "content", "source_received_at",
                          "sim_info", "device_name", "app_version", "recipient",
                          "subject", "source_message_id"]
                cursor = connection.execute(
                    "INSERT INTO messages (received_at, " + ", ".join(fields) +
                    ", message_key, source_ip, lark_push_status) VALUES (" +
                    ", ".join("?" for _ in range(len(fields) + 4)) + ")",
                    [utc_now(), *(message[field] for field in fields),
                     message_key, source_ip, initial_status],
                )
                row_id, push_status, duplicate = int(cursor.lastrowid), initial_status, False
            else:
                row_id, push_status, duplicate = int(stored["id"]), str(stored["lark_push_status"]), True
                message_key = str(stored["message_key"])
        if not duplicate and push_status == "pending":
            if self.notification_thread is not None:
                self.notification_event.set()
            else:
                push_status = self.deliver_notification(row_id)
        return {"ok": True, "id": row_id, "duplicate": duplicate,
                "message_key": message_key, "tag": enriched["tag"],
                "sim_slot": enriched["sim_slot"], "sim_phone": enriched["sim_phone"],
                "recipient": message["recipient"], "lark_push_status": push_status}

    def deliver_notification(self, row_id: int) -> str:
        if self.notifier is None:
            return "disabled"
        with self.notification_lock:
            message = self._load_message(row_id)
            if message is None:
                return "missing"
            if message["lark_push_status"] in {"sent", "skipped", "disabled"}:
                return str(message["lark_push_status"])
            if not message["verification_code"]:
                status, pushed_at, error = "skipped", "", ""
            else:
                try:
                    self.notifier.send(message)
                    status, pushed_at, error = "sent", utc_now(), ""
                except Exception as exc:  # notification failures must not reject SMS ingestion
                    status, pushed_at = "failed", ""
                    error = str(exc)[:512]
            with open_db(self.db_path) as connection:
                connection.execute(
                    """
                    UPDATE messages
                    SET lark_push_status = ?, lark_push_attempts = lark_push_attempts + 1,
                        lark_pushed_at = ?, lark_push_error = ?
                    WHERE id = ?
                    """,
                    (status, pushed_at, error, row_id),
                )
            if status == "failed":
                print(
                    json.dumps(
                        {"time": utc_now(), "event": "feishu_push_failed", "message_id": row_id},
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            return status

    def _notification_loop(self) -> None:
        while not self.notification_stop.is_set():
            self.notification_event.wait(60)
            self.notification_event.clear()
            if self.notification_stop.is_set():
                break
            with open_db(self.db_path) as connection:
                ids = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT id FROM messages
                        WHERE lark_push_status IN ('pending', 'failed')
                          AND lark_push_attempts < 10
                        ORDER BY id LIMIT 10
                        """
                    ).fetchall()
                ]
            for index, row_id in enumerate(ids):
                if self.notification_stop.is_set():
                    break
                self.deliver_notification(int(row_id))
                if index + 1 < len(ids):
                    self.notification_stop.wait(5)

    def server_close(self) -> None:
        if self.mail_receiver is not None:
            self.mail_receiver.close()
        self.notification_stop.set()
        self.notification_event.set()
        if self.notification_thread and self.notification_thread is not threading.current_thread():
            self.notification_thread.join(timeout=2)
        super().server_close()


class RelayHandler(BaseHTTPRequestHandler):
    server: RelayServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        path = urlsplit(self.path).path
        status = str(args[1]) if len(args) > 1 else ""
        print(
            json.dumps(
                {
                    "time": utc_now(),
                    "client": self.client_address[0],
                    "method": self.command,
                    "path": path,
                    "status": status,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )

    def send_security_headers(self) -> None:
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )

    def send_json(
        self,
        status: int,
        payload: dict[str, Any],
        headers: dict[str, str | list[str]] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._send_extra_headers(headers)
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_extra_headers(
        self, headers: dict[str, str | list[str]] | None
    ) -> None:
        for name, value in (headers or {}).items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                self.send_header(name, item)

    def redirect(
        self,
        location: str,
        headers: dict[str, str | list[str]] | None = None,
    ) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._send_extra_headers(headers)
        self.send_security_headers()
        self.end_headers()

    def send_static(self, path: str) -> bool:
        static_file = STATIC_FILES.get(path)
        if static_file is None:
            return False
        filename, content_type, cache_control = static_file
        try:
            body = (WEB_ROOT / filename).read_bytes()
        except OSError:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "web_ui_unavailable"},
            )
            return True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)
        return True

    def api_key_is_valid(self, expected: str) -> bool:
        supplied = self.headers.get("X-API-Key", "")
        authorization = self.headers.get("Authorization", "")
        if not supplied and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def session_user(self) -> dict[str, Any] | None:
        return self.server.resolve_session(self.headers.get("Cookie", ""))

    def require_read_auth(self) -> bool:
        if self.api_key_is_valid(self.server.read_api_key):
            return True
        try:
            if self.session_user() is not None:
                return True
        except CentralAuthUnavailable:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": "authorization_service_unavailable"},
            )
            return False
        self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
        return False

    def require_ingest_auth(self) -> bool:
        if self.api_key_is_valid(self.server.api_key):
            return True
        self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
        return False

    def handle_oauth_login(self) -> None:
        state, verifier, browser_binding = self.server.create_oauth_transaction()
        challenge = _base64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
        try:
            location = self.server.auth_client.authorization_url(state, challenge)
        except CentralAuthError:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": "authorization_service_unavailable"},
            )
            return
        self.redirect(
            location,
            {"Set-Cookie": self.server.transaction_set_cookie(browser_binding)},
        )

    def handle_oauth_callback(self, query: dict[str, list[str]]) -> None:
        if any(len(values) != 1 for values in query.values()):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "duplicate_oauth_parameter"},
            )
            return
        state = query.get("state", [""])[0]
        browser_binding = self.server._cookie_value(
            self.headers.get("Cookie", ""), OAUTH_TRANSACTION_COOKIE_NAME
        )
        verifier = self.server.consume_oauth_transaction(state, browser_binding)
        if verifier is None:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_oauth_state"})
            return
        clear_transaction = {"Set-Cookie": self.server.transaction_clear_cookie()}
        if query.get("iss", [""])[0] != self.server.auth_issuer:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_oauth_issuer"},
                clear_transaction,
            )
            return
        oauth_error = query.get("error", [""])[0]
        if oauth_error:
            if oauth_error != "access_denied":
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "oauth_request_rejected"},
                    clear_transaction,
                )
                return
            self.redirect(
                self.server.auth_success_uri + "?login_error=access_denied",
                clear_transaction,
            )
            return
        code = query.get("code", [""])[0]
        if not code:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_oauth_code"},
                clear_transaction,
            )
            return
        token: dict[str, Any] | None = None
        try:
            token = validate_token_response(
                self.server.auth_client.exchange_code(code, verifier),
                set(self.server.auth_scopes),
            )
            principal = validate_introspection(
                self.server.auth_client.introspect(str(token["access_token"])),
                client_id=self.server.auth_client_id,
                required_scopes=set(self.server.auth_scopes),
            )
        except CentralAuthUnavailable:
            self._revoke_failed_login_token(token)
            print(
                json.dumps(
                    {"time": utc_now(), "event": "central_oauth_unavailable"},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": "authorization_service_unavailable"},
                clear_transaction,
            )
            return
        except CentralAuthRejected:
            self._revoke_failed_login_token(token)
            self.send_json(
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error": "central_authorization_rejected"},
                clear_transaction,
            )
            return
        try:
            handle = self.server.create_session(token, principal)
        except sqlite3.Error:
            self._revoke_failed_login_token(token)
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": "session_store_unavailable"},
                clear_transaction,
            )
            return
        self.redirect(
            self.server.auth_success_uri,
            {
                "Set-Cookie": [
                    self.server.transaction_clear_cookie(),
                    self.server.session_set_cookie(handle),
                ]
            },
        )

    def _revoke_failed_login_token(self, token: dict[str, Any] | None) -> None:
        refresh_token = str((token or {}).get("refresh_token") or "")
        if not refresh_token:
            return
        try:
            self.server.auth_client.revoke(refresh_token)
        except CentralAuthError:
            pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if self.send_static(parsed.path):
            return
        if parsed.path == "/health":
            self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "status": "healthy",
                },
            )
            return
        if parsed.path == "/auth/login":
            self.handle_oauth_login()
            return
        if parsed.path == "/auth/callback":
            self.handle_oauth_callback(parse_qs(parsed.query, keep_blank_values=True))
            return
        if parsed.path == "/auth/session":
            try:
                user = self.session_user()
            except CentralAuthUnavailable:
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "authorization_service_unavailable"},
                )
                return
            if user is None:
                self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            else:
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "user": {
                            "subject": user["subject"],
                            "open_id": user.get("open_id", ""),
                            "name": user["name"],
                        },
                        "authentication": "central_oauth",
                    },
                )
            return
        if parsed.path == "/v1/platforms/identify":
            if not self.require_read_auth():
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            url = str(query.get("url", [""])[0]).strip()
            if not url or len(url) > 4096:
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "url_is_required"},
                )
                return
            tag = identify_platform(url)
            self.send_json(
                HTTPStatus.OK,
                {"ok": True, "recognized": bool(tag), "tag": tag},
            )
            return
        code_path = re.fullmatch(r"/v1/messages/([1-9][0-9]{0,17})/code", parsed.path)
        if code_path:
            if not self.require_read_auth():
                return
            message = self.server._load_message(int(code_path[1]))
            if message is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
                return
            fields = ("id", "message_type", "verification_code", "recipient", "sim_phone", "sim_slot", "sender", "subject")
            self.send_json(HTTPStatus.OK, {"ok": True, "message": {field: message.get(field, "") for field in fields}})
            return
        if parsed.path == "/v1/mailboxes":
            if not self.require_read_auth():
                return
            accounts = self.server.mail_receiver.status() if self.server.mail_receiver else []
            self.send_json(HTTPStatus.OK, {"ok": True, "mailboxes": accounts})
            return
        if parsed.path != "/v1/messages":
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        if not self.require_read_auth():
            return

        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            limit = min(max(int(query.get("limit", ["50"])[0]), 1), 200)
            before_id = int(query.get("before_id", ["0"])[0])
            after_id = int(query.get("after_id", ["0"])[0])
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_query"})
            return
        incremental = "after_id" in query
        if (incremental and "before_id" in query) or after_id < 0:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_query"})
            return

        sql = """
            SELECT id, received_at, message_type, sender, content,
                   source_received_at, sim_info, device_name, app_version, message_key,
                   lark_push_status, lark_push_attempts, lark_pushed_at,
                   recipient, subject, source_message_id
            FROM messages
        """
        parameters: list[Any] = []
        conditions: list[str] = []
        for field in ("message_type", "recipient"):
            value = query.get(field, [""])[0]
            if value:
                if field == "message_type" and value not in {"sms", "email"}:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_query"})
                    return
                conditions.append(f"{field} = ?")
                parameters.append(value)
        if incremental:
            conditions.append("id > ?")
            parameters.append(after_id)
        elif before_id > 0:
            conditions.append("id < ?")
            parameters.append(before_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id " + ("ASC" if incremental else "DESC") + " LIMIT ?"
        parameters.append(limit + 1 if incremental else limit)

        with open_db(self.server.db_path) as connection:
            rows = [enrich_message(dict(row)) for row in connection.execute(sql, parameters)]
        if incremental:
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_after_id = int(rows[-1]["id"]) if rows else after_id
            self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "count": len(rows),
                    "messages": rows,
                    "next_after_id": next_after_id,
                    "has_more": has_more,
                },
            )
            return
        self.send_json(HTTPStatus.OK, {"ok": True, "count": len(rows), "messages": rows})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/auth/logout":
            try:
                self.server.logout_session(self.headers.get("Cookie", ""))
            except CentralAuthError:
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "authorization_service_unavailable"},
                )
                return
            self.send_json(
                HTTPStatus.OK,
                {"ok": True},
                {"Set-Cookie": self.server.session_clear_cookie()},
            )
            return
        if parsed.path != "/v1/messages":
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        if not self.require_ingest_auth():
            return

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"ok": False, "error": "content_type_must_be_application_json"},
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": "invalid_body_size"},
            )
            return

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            message = parse_message(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        source_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(
            ",", 1)[0].strip()[:64]
        self.send_json(HTTPStatus.OK, self.server.ingest_message(message, source_ip))

    def do_PUT(self) -> None:  # noqa: N802
        self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})


def main() -> None:
    from mail_receiver import MailReceiver, load_accounts

    accounts = load_accounts(os.environ.get("SMS_RELAY_MAILBOXES_FILE", ""))
    api_key = os.environ.get("SMS_RELAY_API_KEY", "")
    read_api_key = os.environ.get("SMS_RELAY_READ_API_KEY", "")
    db_path = os.environ.get("SMS_RELAY_DB_PATH", "/data/sms-relay.db")
    host = os.environ.get("SMS_RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("SMS_RELAY_PORT", "8000"))
    feishu_app_id = os.environ.get("FEISHU_APP_ID", "")
    feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    feishu_chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    if bool(feishu_app_id) != bool(feishu_app_secret):
        raise ValueError("Feishu bot credentials must be configured together")
    if feishu_chat_id and not (feishu_app_id and feishu_app_secret):
        raise ValueError("FEISHU_CHAT_ID requires Feishu bot credentials")
    auth_scope_text = os.environ.get("AUTH_SCOPES", "sms-relay:access")
    auth_scopes = tuple(
        value for value in auth_scope_text.replace(",", " ").split() if value
    )
    server = RelayServer(
        (host, port),
        api_key,
        db_path,
        read_api_key=read_api_key,
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        feishu_chat_id=feishu_chat_id,
        auth_issuer=os.environ.get(
            "AUTH_ISSUER", "https://auth.midi.lizhijian.xyz"
        ),
        auth_client_id=os.environ.get("AUTH_CLIENT_ID", "sms-relay-web"),
        auth_audience=os.environ.get("AUTH_AUDIENCE", "sms-relay-api"),
        auth_scopes=auth_scopes,
        auth_redirect_uri=os.environ.get(
            "AUTH_REDIRECT_URI",
            "https://api.midi.lizhijian.xyz/sms-relay/auth/callback",
        ),
        auth_post_logout_redirect_uri=os.environ.get(
            "AUTH_POST_LOGOUT_REDIRECT_URI",
            "https://api.midi.lizhijian.xyz/sms-relay/?auto_sso=off",
        ),
        auth_backchannel_ip=os.environ.get("AUTH_BACKCHANNEL_IP", ""),
        public_cookie_path=os.environ.get("SMS_RELAY_COOKIE_PATH", "/sms-relay/"),
        session_idle_seconds=int(
            os.environ.get("AUTH_SESSION_IDLE_SECONDS", str(SESSION_IDLE_SECONDS))
        ),
        session_absolute_seconds=int(
            os.environ.get(
                "AUTH_SESSION_ABSOLUTE_SECONDS", str(SESSION_ABSOLUTE_SECONDS)
            )
        ),
        introspection_cache_seconds=int(
            os.environ.get("AUTH_INTROSPECTION_CACHE_SECONDS", "5")
        ),
    )
    print(
        json.dumps(
            {
                "event": "started",
                "mailboxes": len(accounts),
                "host": host,
                "port": port,
                "db_path": db_path,
                "authentication": "central_oauth",
                "auth_issuer": server.auth_issuer,
                "auth_client_id": server.auth_client_id,
                "feishu_push": server.notifier is not None,
            }
        ),
        flush=True,
    )
    try:
        if accounts:
            server.mail_receiver = MailReceiver(server.db_path, accounts, server.ingest_message)
            server.mail_receiver.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
