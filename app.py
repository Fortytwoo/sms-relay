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
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from access_control import (
    AccessConflictError,
    AccessControl,
    InvalidAccessSubjectError,
)


MAX_BODY_BYTES = 64 * 1024
MAX_CONTENT_CHARS = 32 * 1024
SESSION_COOKIE_NAME = "sms_relay_session"
SESSION_SECONDS = 12 * 60 * 60
OAUTH_STATE_SECONDS = 5 * 60
MAX_OAUTH_STATES = 2048
WEB_ROOT = Path(__file__).with_name("web")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8", "no-cache"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8", "public, max-age=3600"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8", "public, max-age=3600"),
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
_CODE_PATTERNS = (
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
            return match.group(1).upper()
    return ""


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
    message["verification_code"] = extract_verification_code(str(message.get("content", "")))
    message["tag"] = extract_message_tag(str(message.get("content", "")))
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
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        migrations = {
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

    if not result["content"]:
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
    }
    for field, limit in limits.items():
        if len(result[field]) > limit:
            raise ValueError(f"{field} is too long")

    if not result["message_type"]:
        result["message_type"] = "sms"
    return result


def fingerprint(message: dict[str, str]) -> str:
    canonical = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


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
        directory_request: bool = False,
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

    def fetch_directory(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[tuple[str, str]]]:
        departments: list[dict[str, Any]] = [
            {
                "department_id": "0",
                "parent_department_id": "",
                "name": "企业",
                "order": 0,
                "member_count": 0,
            }
        ]
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "department_id_type": "open_department_id",
                "user_id_type": "open_id",
                "page_size": 50,
                "fetch_child": "true",
            }
            if page_token:
                params["page_token"] = page_token
            response = self.request(
                "/open-apis/contact/v3/departments/0/children",
                params=params,
                directory_request=True,
            )
            data = response.get("data") or {}
            for item in data.get("items") or []:
                department_id = str(item.get("open_department_id") or "")
                if not department_id:
                    continue
                departments.append(
                    {
                        "department_id": department_id,
                        "parent_department_id": str(
                            item.get("parent_department_id")
                            or item.get("parent_open_department_id")
                            or "0"
                        ),
                        "name": str(item.get("name") or "未命名部门"),
                        "order": int(item.get("order") or 0),
                        "member_count": int(item.get("member_count") or 0),
                    }
                )
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise RuntimeError("feishu_directory_missing_page_token")

        users_by_id: dict[str, dict[str, Any]] = {}
        memberships: set[tuple[str, str]] = set()
        for department in departments:
            department_id = str(department["department_id"])
            page_token = ""
            while True:
                params = {
                    "department_id": department_id,
                    "department_id_type": "open_department_id",
                    "user_id_type": "open_id",
                    "page_size": 50,
                }
                if page_token:
                    params["page_token"] = page_token
                response = self.request(
                    "/open-apis/contact/v3/users/find_by_department",
                    params=params,
                    directory_request=True,
                )
                data = response.get("data") or {}
                for item in data.get("items") or []:
                    open_id = str(item.get("open_id") or "")
                    if not open_id:
                        continue
                    status = item.get("status") or {}
                    active = not bool(status.get("is_resigned") or status.get("is_frozen"))
                    if "is_activated" in status:
                        active = active and bool(status.get("is_activated"))
                    avatar = item.get("avatar") or {}
                    users_by_id[open_id] = {
                        "open_id": open_id,
                        "union_id": str(item.get("union_id") or ""),
                        "name": str(item.get("name") or "飞书用户"),
                        "avatar_url": str(
                            avatar.get("avatar_72") or avatar.get("avatar_origin") or ""
                        ),
                        "active": active,
                    }
                    memberships.add((open_id, department_id))
                    for listed_department_id in item.get("department_ids") or []:
                        memberships.add((open_id, str(listed_department_id)))
                if not data.get("has_more"):
                    break
                page_token = str(data.get("page_token") or "")
                if not page_token:
                    raise RuntimeError("feishu_users_missing_page_token")
        return departments, list(users_by_id.values()), memberships


class FeishuNotifier:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        chat_id: str,
        *,
        client: FeishuClient | None = None,
    ):
        self.client = client or FeishuClient(app_id, app_secret)
        self.chat_id = chat_id

    def send(self, message: dict[str, Any]) -> None:
        code = str(message.get("verification_code", ""))
        if not code:
            return
        receiver = str(message.get("sim_phone") or message.get("sim_slot") or "未知")
        received_at = str(message.get("source_received_at") or message.get("received_at") or "未知")
        tag = str(message.get("tag") or "").strip()[:128]
        lines = [f"验证码：{code}"]
        if tag:
            lines.append(f"平台：{tag}")
        lines.extend(
            (
                f"来源：{message.get('sender') or '未知'}",
                f"接收号码：{receiver}",
                f"接收时间：{received_at}",
            )
        )
        text = "\n".join(lines)
        response = self.client.request(
            "/open-apis/im/v1/messages",
            method="POST",
            params={"receive_id_type": "chat_id"},
            payload={
                "receive_id": self.chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":")),
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
        session_secret: str | None = None,
        feishu_app_id: str = "",
        feishu_app_secret: str = "",
        feishu_redirect_uri: str = "",
        feishu_chat_id: str = "",
        allowed_open_ids: set[str] | None = None,
        admin_open_ids: set[str] | None = None,
        admin_union_ids: set[str] | None = None,
        public_cookie_path: str = "/sms-relay/",
        notifier: FeishuNotifier | None = None,
        feishu_client: FeishuClient | None = None,
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
        self.session_secret = (session_secret or api_key).encode("utf-8")
        if len(self.session_secret) < 32:
            raise ValueError("SMS_RELAY_SESSION_SECRET must contain at least 32 characters")
        self.feishu_app_id = feishu_app_id
        self.feishu_app_secret = feishu_app_secret
        self.feishu_redirect_uri = feishu_redirect_uri
        self.admin_open_ids = set(admin_open_ids or allowed_open_ids or set())
        self.admin_union_ids = set(admin_union_ids or set())
        self.public_cookie_path = public_cookie_path
        self.oauth_states: dict[str, tuple[float, str]] = {}
        self.oauth_states_lock = threading.Lock()
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
            )
        self.notification_lock = threading.Lock()
        self.notification_stop = threading.Event()
        self.notification_event = threading.Event()
        self.notification_thread: threading.Thread | None = None
        self.directory_thread: threading.Thread | None = None
        self.directory_thread_lock = threading.Lock()
        init_db(db_path)
        self.access = AccessControl(
            db_path,
            admin_open_ids=self.admin_open_ids,
            admin_union_ids=self.admin_union_ids,
        )
        super().__init__(address, RelayHandler)
        if self.notifier is not None:
            self.notification_thread = threading.Thread(
                target=self._notification_loop,
                name="feishu-notification-worker",
                daemon=True,
            )
            self.notification_thread.start()
            self.notification_event.set()

    @property
    def oauth_is_configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret and self.feishu_redirect_uri)

    def _session_value(self, open_id: str, name: str, union_id: str = "") -> str:
        payload = _base64url_encode(
            json.dumps(
                {
                    "open_id": open_id,
                    "union_id": union_id,
                    "name": name,
                    "csrf": secrets.token_urlsafe(24),
                    "exp": int(time.time()) + SESSION_SECONDS,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signature = _base64url_encode(
            hmac.new(self.session_secret, payload.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{payload}.{signature}"

    def make_session_cookie(self, open_id: str, name: str, union_id: str = "") -> str:
        return f"{SESSION_COOKIE_NAME}={self._session_value(open_id, name, union_id)}"

    def session_set_cookie(self, open_id: str, name: str, union_id: str = "") -> str:
        return (
            f"{self.make_session_cookie(open_id, name, union_id)}; Path={self.public_cookie_path}; "
            f"Max-Age={SESSION_SECONDS}; HttpOnly; Secure; SameSite=Lax"
        )

    def session_clear_cookie(self) -> str:
        return (
            f"{SESSION_COOKIE_NAME}=; Path={self.public_cookie_path}; Max-Age=0; "
            "HttpOnly; Secure; SameSite=Lax"
        )

    def parse_session_cookie(self, raw_cookie: str) -> dict[str, Any] | None:
        try:
            cookie = SimpleCookie(raw_cookie)
            value = cookie[SESSION_COOKIE_NAME].value
            payload, signature = value.split(".", 1)
            expected = hmac.new(
                self.session_secret, payload.encode("ascii"), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(_base64url_decode(signature), expected):
                return None
            data = json.loads(_base64url_decode(payload).decode("utf-8"))
            if int(data.get("exp", 0)) < int(time.time()):
                return None
            if not data.get("open_id") or not data.get("csrf"):
                return None
            return data
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def create_oauth_state(self) -> tuple[str, str]:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        now = time.time()
        with self.oauth_states_lock:
            self.oauth_states = {
                key: value for key, value in self.oauth_states.items() if value[0] > now
            }
            self.oauth_states[state] = (now + OAUTH_STATE_SECONDS, verifier)
            while len(self.oauth_states) > MAX_OAUTH_STATES:
                self.oauth_states.pop(next(iter(self.oauth_states)))
        return state, verifier

    def consume_oauth_state(self, state: str) -> str | None:
        with self.oauth_states_lock:
            stored = self.oauth_states.pop(state, None)
        if not stored or stored[0] < time.time():
            return None
        return stored[1]

    def exchange_feishu_code(self, code: str) -> dict[str, Any]:
        response = _json_request(
            "https://accounts.feishu.cn/oauth/v3/token",
            method="POST",
            payload={
                "grant_type": "authorization_code",
                "client_id": self.feishu_app_id,
                "client_secret": self.feishu_app_secret,
                "code": code,
                "redirect_uri": self.feishu_redirect_uri,
            },
        )
        if response.get("code") not in (None, 0) or not response.get("access_token"):
            raise RuntimeError(f"feishu_oauth_error_{response.get('code', 'unknown')}")
        return response

    def fetch_feishu_user(self, access_token: str) -> dict[str, Any]:
        response = _json_request(
            "https://open.feishu.cn/open-apis/authen/v1/user_info",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.get("code") != 0 or not isinstance(response.get("data"), dict):
            raise RuntimeError(f"feishu_user_error_{response.get('code', 'unknown')}")
        return response["data"]

    def resolve_session(self, raw_cookie: str) -> dict[str, Any] | None:
        session = self.parse_session_cookie(raw_cookie)
        if session is None:
            return None
        user = self.access.resolve_user(
            str(session.get("open_id") or ""),
            union_id=str(session.get("union_id") or ""),
            fallback_name=str(session.get("name") or ""),
        )
        if user is None:
            return None
        return {**user, "csrf": str(session["csrf"]), "exp": int(session["exp"])}

    def start_directory_sync(self, actor_open_id: str) -> bool:
        if self.feishu_client is None or not self.oauth_is_configured:
            raise RuntimeError("feishu_directory_not_configured")
        if not self.access.begin_sync():
            return False

        def run() -> None:
            try:
                departments, users, memberships = self.feishu_client.fetch_directory()
                self.access.complete_sync(departments, users, memberships)
                print(
                    json.dumps(
                        {
                            "time": utc_now(),
                            "event": "feishu_directory_sync_completed",
                            "departments": len(departments),
                            "users": len(users),
                            "actor": actor_open_id,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:
                reason = str(exc)[:512]
                self.access.fail_sync(reason)
                print(
                    json.dumps(
                        {
                            "time": utc_now(),
                            "event": "feishu_directory_sync_failed",
                            "reason": reason,
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                    flush=True,
                )

        with self.directory_thread_lock:
            self.directory_thread = threading.Thread(
                target=run,
                name="feishu-directory-sync",
                daemon=True,
            )
            self.directory_thread.start()
        return True

    def _load_message(self, row_id: int) -> dict[str, Any] | None:
        with open_db(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT id, received_at, message_type, sender, content,
                       source_received_at, sim_info, device_name, app_version,
                       message_key, lark_push_status, lark_push_attempts,
                       lark_pushed_at, lark_push_error
                FROM messages WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
        return enrich_message(dict(row)) if row else None

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
        self.notification_stop.set()
        self.notification_event.set()
        if self.notification_thread and self.notification_thread is not threading.current_thread():
            self.notification_thread.join(timeout=2)
        if self.directory_thread and self.directory_thread is not threading.current_thread():
            self.directory_thread.join(timeout=2)
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
        self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
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

    def require_admin(self) -> dict[str, Any] | None:
        user = self.session_user()
        if user is None:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return None
        if user.get("role") != "admin":
            self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden"})
            return None
        return user

    def require_csrf(self, user: dict[str, Any]) -> bool:
        supplied = self.headers.get("X-CSRF-Token", "")
        expected = str(user.get("csrf") or "")
        if supplied and expected and hmac.compare_digest(supplied, expected):
            return True
        self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "invalid_csrf_token"})
        return False

    def read_json_body(self) -> Any:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("content_type_must_be_application_json")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid_body_size") from exc
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            raise ValueError("invalid_body_size")
        try:
            return json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid_json") from exc

    def require_read_auth(self) -> bool:
        if self.api_key_is_valid(self.server.read_api_key) or self.session_user() is not None:
            return True
        self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
        return False

    def require_ingest_auth(self) -> bool:
        if self.api_key_is_valid(self.server.api_key):
            return True
        self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
        return False

    def handle_oauth_login(self) -> None:
        if not self.server.oauth_is_configured:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": "feishu_oauth_not_configured"},
            )
            return
        state, _ = self.server.create_oauth_state()
        location = "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode(
            {
                "client_id": self.server.feishu_app_id,
                "response_type": "code",
                "redirect_uri": self.server.feishu_redirect_uri,
                "state": state,
            }
        )
        self.redirect(location)

    def handle_oauth_callback(self, query: dict[str, list[str]]) -> None:
        state = query.get("state", [""])[0]
        verifier = self.server.consume_oauth_state(state)
        if verifier is None:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_oauth_state"})
            return
        if query.get("error"):
            self.redirect("./?login_error=access_denied")
            return
        code = query.get("code", [""])[0]
        if not code:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_oauth_code"})
            return
        try:
            token = self.server.exchange_feishu_code(code)
            user = self.server.fetch_feishu_user(str(token["access_token"]))
        except RuntimeError as exc:
            safe_reason = re.sub(
                r"(?i)(access_token|refresh_token|client_secret|code(?:_verifier)?)[^,}\s]*",
                r"\1=<redacted>",
                str(exc),
            )[:512]
            print(
                json.dumps(
                    {"time": utc_now(), "event": "feishu_oauth_failed", "reason": safe_reason},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            self.send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "feishu_oauth_failed"})
            return
        open_id = str(user.get("open_id", ""))
        union_id = str(user.get("union_id") or "")
        name = str(user.get("name") or "飞书用户")[:128]
        authorized = self.server.access.resolve_user(
            open_id,
            union_id=union_id,
            fallback_name=name,
        )
        if authorized is None:
            self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "user_not_allowed"})
            return
        self.server.access.record_login(open_id, union_id, name)
        root_url = self.server.feishu_redirect_uri.rsplit("auth/callback", 1)[0]
        self.redirect(
            root_url,
            {"Set-Cookie": self.server.session_set_cookie(open_id, name, union_id)},
        )

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
            self.handle_oauth_callback(parse_qs(parsed.query))
            return
        if parsed.path == "/auth/session":
            user = self.session_user()
            if user is None:
                self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            else:
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "user": {
                            "open_id": user["open_id"],
                            "name": user["name"],
                            "role": user["role"],
                        },
                        "csrf_token": user["csrf"],
                    },
                )
            return
        if parsed.path == "/v1/admin/directory":
            user = self.require_admin()
            if user is None:
                return
            snapshot = self.server.access.directory_snapshot()
            auto_sync_started = False
            if not snapshot["departments"] and snapshot["sync"]["status"] != "running":
                try:
                    auto_sync_started = self.server.start_directory_sync(str(user["open_id"]))
                    snapshot = self.server.access.directory_snapshot()
                except RuntimeError as exc:
                    self.server.access.fail_sync(str(exc))
                    snapshot = self.server.access.directory_snapshot()
            self.send_json(
                HTTPStatus.OK,
                {"ok": True, **snapshot, "auto_sync_started": auto_sync_started},
            )
            return
        if parsed.path == "/v1/admin/directory/users":
            if self.require_admin() is None:
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                limit = int(query.get("limit", ["100"])[0])
                offset = int(query.get("offset", ["0"])[0])
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_query"})
                return
            result = self.server.access.list_users(
                department_id=str(query.get("department_id", [""])[0]),
                query=str(query.get("query", [""])[0]),
                limit=limit,
                offset=offset,
            )
            self.send_json(HTTPStatus.OK, {"ok": True, **result})
            return
        if parsed.path == "/v1/admin/access":
            if self.require_admin() is None:
                return
            self.send_json(
                HTTPStatus.OK,
                {"ok": True, **self.server.access.access_summary()},
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
                   lark_push_status, lark_push_attempts, lark_pushed_at
            FROM messages
        """
        parameters: list[Any] = []
        if incremental:
            sql += " WHERE id > ? ORDER BY id ASC LIMIT ?"
            parameters.extend((after_id, limit + 1))
        elif before_id > 0:
            sql += " WHERE id < ?"
            parameters.append(before_id)
        if not incremental:
            sql += " ORDER BY id DESC LIMIT ?"
            parameters.append(limit)

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
            self.send_json(
                HTTPStatus.OK,
                {"ok": True},
                {"Set-Cookie": self.server.session_clear_cookie()},
            )
            return
        if parsed.path == "/v1/admin/directory/sync":
            user = self.require_admin()
            if user is None or not self.require_csrf(user):
                return
            try:
                started = self.server.start_directory_sync(str(user["open_id"]))
            except RuntimeError as exc:
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": str(exc)},
                )
                return
            if not started:
                self.send_json(
                    HTTPStatus.CONFLICT,
                    {"ok": False, "error": "directory_sync_already_running"},
                )
                return
            self.send_json(
                HTTPStatus.ACCEPTED,
                {"ok": True, "status": "running"},
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

        message_key = fingerprint(message)
        source_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",", 1)[0].strip()[:64]
        server_received_at = utc_now()
        has_code = bool(extract_verification_code(message["content"]))
        tag = extract_message_tag(message["content"])
        sim_slot, sim_phone = parse_sim_info(message["sim_info"])
        initial_push_status = "pending" if has_code and self.server.notifier else "disabled" if has_code else "skipped"

        with open_db(self.server.db_path) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO messages (
                    received_at, message_type, sender, content, source_received_at,
                    sim_info, device_name, app_version, message_key, source_ip,
                    lark_push_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    server_received_at,
                    message["message_type"],
                    message["sender"],
                    message["content"],
                    message["source_received_at"],
                    message["sim_info"],
                    message["device_name"],
                    message["app_version"],
                    message_key,
                    source_ip,
                    initial_push_status,
                ),
            )
            duplicate = cursor.rowcount == 0
            if duplicate:
                stored = connection.execute(
                    "SELECT id, lark_push_status FROM messages WHERE message_key = ?",
                    (message_key,),
                ).fetchone()
                row_id, push_status = int(stored[0]), str(stored[1])
            else:
                row_id, push_status = int(cursor.lastrowid), initial_push_status

        if push_status in {"pending", "failed"} and self.server.notifier is not None:
            if self.server.notification_thread is not None:
                self.server.notification_event.set()
            else:
                push_status = self.server.deliver_notification(row_id)

        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "id": row_id,
                "duplicate": duplicate,
                "message_key": message_key,
                "tag": tag,
                "sim_slot": sim_slot,
                "sim_phone": sim_phone,
                "lark_push_status": push_status,
            },
        )

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path != "/v1/admin/access":
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        user = self.require_admin()
        if user is None or not self.require_csrf(user):
            return
        try:
            payload = self.read_json_body()
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            department_ids = payload.get("department_ids", [])
            user_open_ids = payload.get("user_open_ids", [])
            revision = payload.get("revision")
            if not isinstance(department_ids, list) or not isinstance(user_open_ids, list):
                raise ValueError("access subjects must be arrays")
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise ValueError("revision must be an integer")
            result = self.server.access.replace_grants(
                department_ids=[str(value) for value in department_ids],
                user_open_ids=[str(value) for value in user_open_ids],
                expected_revision=revision,
                actor_open_id=str(user["open_id"]),
            )
        except AccessConflictError:
            self.send_json(
                HTTPStatus.CONFLICT,
                {"ok": False, "error": "access_revision_conflict"},
            )
            return
        except InvalidAccessSubjectError as exc:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc)},
            )
            return
        except ValueError as exc:
            status = (
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE
                if str(exc) == "content_type_must_be_application_json"
                else HTTPStatus.BAD_REQUEST
            )
            self.send_json(status, {"ok": False, "error": str(exc)})
            return
        self.send_json(HTTPStatus.OK, {"ok": True, **result})


def main() -> None:
    api_key = os.environ.get("SMS_RELAY_API_KEY", "")
    read_api_key = os.environ.get("SMS_RELAY_READ_API_KEY", "")
    db_path = os.environ.get("SMS_RELAY_DB_PATH", "/data/sms-relay.db")
    host = os.environ.get("SMS_RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("SMS_RELAY_PORT", "8000"))
    session_secret = os.environ.get("SMS_RELAY_SESSION_SECRET", "")
    feishu_app_id = os.environ.get("FEISHU_APP_ID", "")
    feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    feishu_redirect_uri = os.environ.get("FEISHU_REDIRECT_URI", "")
    feishu_chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    legacy_allowed_open_ids = {
        value.strip()
        for value in os.environ.get("FEISHU_ALLOWED_OPEN_IDS", "").split(",")
        if value.strip()
    }
    admin_open_ids = {
        value.strip()
        for value in os.environ.get("FEISHU_ADMIN_OPEN_IDS", "").split(",")
        if value.strip()
    } or legacy_allowed_open_ids
    admin_union_ids = {
        value.strip()
        for value in os.environ.get("FEISHU_ADMIN_UNION_IDS", "").split(",")
        if value.strip()
    }
    if any((feishu_app_id, feishu_app_secret, feishu_redirect_uri)) and not all(
        (
            feishu_app_id,
            feishu_app_secret,
            feishu_redirect_uri,
            session_secret,
            admin_open_ids or admin_union_ids,
        )
    ):
        raise ValueError("Feishu OAuth configuration is incomplete")
    server = RelayServer(
        (host, port),
        api_key,
        db_path,
        read_api_key=read_api_key,
        session_secret=session_secret or api_key,
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        feishu_redirect_uri=feishu_redirect_uri,
        feishu_chat_id=feishu_chat_id,
        admin_open_ids=admin_open_ids,
        admin_union_ids=admin_union_ids,
        public_cookie_path=os.environ.get("SMS_RELAY_COOKIE_PATH", "/sms-relay/"),
    )
    print(
        json.dumps(
            {
                "event": "started",
                "host": host,
                "port": port,
                "db_path": db_path,
                "feishu_oauth": server.oauth_is_configured,
                "feishu_push": server.notifier is not None,
                "configured_admins": len(admin_open_ids) + len(admin_union_ids),
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
