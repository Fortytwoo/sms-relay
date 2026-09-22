"""Read-only IMAP ingestion; credentials stay in a server-side configuration file."""
from __future__ import annotations

import hashlib
import imaplib
import json
import re
import sqlite3
import ssl
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable


MAX_MAIL_BYTES = 2 * 1024 * 1024
BATCH_SIZE = 50


class MailboxLoginError(Exception):
    """Provider rejected credentials or disabled IMAP; do not retain its response."""


def normalize_address(value: str) -> str:
    if len(value) > 320 or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", value):
        raise ValueError("recipient must be a single email address")
    local, domain = value.rsplit("@", 1)
    return local + "@" + domain.lower()


@dataclass(frozen=True)
class MailAccount:
    id: str
    address: str
    host: str
    username: str
    password: str = field(repr=False)
    port: int = 993
    folder: str = "INBOX"
    start_from: str = "latest"

    @property
    def state_key(self) -> str:
        # A changed server/folder/login is a new stream, even when its label is reused.
        identity = [self.id, self.address, self.host, self.port, self.username, self.folder]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def load_accounts(path: str) -> list[MailAccount]:
    if not path:
        return []
    try:
        records = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(records, list) or len(records) > 100:
            raise ValueError
        accounts = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError
            if set(record) - set(MailAccount.__dataclass_fields__):
                raise ValueError
            values = dict(record)
            values.setdefault("username", values.get("address"))
            account = MailAccount(**values)
            if any(not isinstance(getattr(account, name), str) or not getattr(account, name)
                   for name in ("id", "address", "host", "username", "password", "folder")):
                raise ValueError
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account.id):
                raise ValueError
            if type(account.port) is not int or not 1 <= account.port <= 65535:
                raise ValueError
            if account.start_from not in {"latest", "all"}:
                raise ValueError
            # Use ASCII IMAP folder names (modified UTF-7 for non-ASCII folders).
            if not account.folder.isascii() or any(c in account.folder for c in '\r\n"\\'):
                raise ValueError
            if any(c in account.host + account.username for c in "\r\n"):
                raise ValueError
            values["address"] = normalize_address(account.address)
            accounts.append(MailAccount(**values))
        if len({a.id for a in accounts}) != len(accounts):
            raise ValueError
        if len({a.address for a in accounts}) != len(accounts):
            raise ValueError
        return accounts
    except (ValueError, TypeError, OSError):
        # Do not include JSON decoder excerpts or paths containing credentials.
        raise ValueError("invalid mailbox configuration; see docs/EMAIL.md") from None


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "tr", "li", "h1", "h2"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "div", "tr", "li", "h1", "h2"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_email(raw: bytes, account: MailAccount, source_id: str) -> dict[str, str]:
    if len(raw) > MAX_MAIL_BYTES:
        raise ValueError("mail_too_large")
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    part = mail.get_body(preferencelist=("plain", "html"))
    content = ""
    if part is not None:
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            content = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            parser = _HTMLText()
            parser.feed(content)
            content = "".join(parser.parts)
    subject = str(mail.get("Subject", ""))
    timestamp = ""
    try:
        timestamp = parsedate_to_datetime(str(mail.get("Date", ""))).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    # Keep original code case. Attachments and HTML are never persisted/rendered.
    return {"type": "email", "from": str(mail.get("From", ""))[:512],
            "recipient": account.address, "subject": subject[:2048],
            "content": content.strip()[:32768] or ("[无文本正文]" if not subject else ""),
            "received_at": timestamp, "source_message_id": source_id}


@contextmanager
def _db(path: str):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class MailReceiver:
    def __init__(self, db_path: str, accounts: list[MailAccount], ingest: Callable,
                 *, client_factory=imaplib.IMAP4_SSL):
        self.db_path, self.accounts, self.ingest = db_path, accounts, ingest
        self.client_factory = client_factory
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        with _db(db_path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS mailbox_state (
                state_key TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL DEFAULT '',
                last_uid INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '', skipped_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0
            )""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(mailbox_state)")}
            for name in ("next_retry_at", "consecutive_failures"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE mailbox_state ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            for account in accounts:
                connection.execute("INSERT OR IGNORE INTO mailbox_state(state_key) VALUES (?)",
                                   (account.state_key,))

    def status(self) -> list[dict]:
        with _db(self.db_path) as connection:
            return [{"id": a.id, "address": a.address,
                     **dict(connection.execute(
                         "SELECT last_success_at, last_error, skipped_count, next_retry_at FROM mailbox_state WHERE state_key = ?",
                         (a.state_key,)).fetchone())} for a in self.accounts]

    def _update(self, account: MailAccount, **values):
        with _db(self.db_path) as connection:
            connection.execute("UPDATE mailbox_state SET " +
                               ", ".join(name + " = ?" for name in values) + " WHERE state_key = ?",
                               [*values.values(), account.state_key])

    @staticmethod
    def _ok(response):
        kind, data = response
        if kind != "OK":
            raise imaplib.IMAP4.error("imap_request_failed")
        return data

    @staticmethod
    def _number(client, name):
        _, values = client.response(name)
        if not values or not values[0] or not values[0].isdigit():
            raise imaplib.IMAP4.error("missing_mailbox_identity")
        return int(values[0])

    def poll_account(self, account: MailAccount):
        client = self.client_factory(account.host, account.port,
                                     ssl_context=ssl.create_default_context(), timeout=15)
        try:
            try:
                self._ok(client.login(account.username, account.password))
            except imaplib.IMAP4.error:
                raise MailboxLoginError("mailbox_auth_failed") from None
            self._ok(client.select('"' + account.folder + '"', readonly=True))
            validity = str(self._number(client, "UIDVALIDITY"))
            next_uid = self._number(client, "UIDNEXT")
            with _db(self.db_path) as connection:
                state = dict(connection.execute("SELECT * FROM mailbox_state WHERE state_key = ?",
                                                (account.state_key,)).fetchone())
            if state["uidvalidity"] and state["uidvalidity"] != validity:
                # Do not silently replay historical codes after a mailbox rebuild.
                self._update(account, last_error="uidvalidity_changed")
                return
            last_uid = state["last_uid"]
            if not state["uidvalidity"]:
                last_uid = next_uid - 1 if account.start_from == "latest" else 0
                self._update(account, uidvalidity=validity, last_uid=last_uid)
            result = self._ok(client.uid("search", None, "UID", f"{last_uid + 1}:*"))
            # IMAP n:* can return the current maximum even when it is below n.
            uids = sorted({int(uid) for uid in (result[0] or b"").split() if int(uid) > last_uid})
            skipped = state["skipped_count"]
            for uid in uids[:BATCH_SIZE]:
                if self.stop.is_set():
                    return
                response = self._ok(client.uid("fetch", str(uid),
                    f"(UID RFC822.SIZE BODY.PEEK[]<0.{MAX_MAIL_BYTES + 1}>)"))
                parts = [part for part in response if isinstance(part, tuple)]
                if not parts:
                    # It may have been expunged between SEARCH and FETCH. Retry next poll.
                    raise imaplib.IMAP4.error("mail_missing")
                metadata, raw = parts[0]
                size = re.search(rb"RFC822.SIZE (\d+)", metadata, re.IGNORECASE)
                if size is None:
                    raise imaplib.IMAP4.error("mail_size_missing")
                if int(size[1]) > MAX_MAIL_BYTES or len(raw) > MAX_MAIL_BYTES:
                    skipped += 1
                    self._update(account, last_uid=uid, skipped_count=skipped)
                    continue
                if len(raw) != int(size[1]):
                    raise imaplib.IMAP4.error("mail_truncated")
                source_id = f"imap:{account.state_key}:{validity}:{uid}"
                self.ingest(parse_email(raw, account, source_id))
                # Crash between commit and cursor update is safe: ingestion deduplicates.
                self._update(account, last_uid=uid)
            self._update(account, last_success_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         last_error="", next_retry_at=0, consecutive_failures=0)
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def poll_once(self):
        for index, account in enumerate(self.accounts):
            if self.stop.is_set():
                return
            with _db(self.db_path) as connection:
                retry = connection.execute(
                    "SELECT next_retry_at, consecutive_failures FROM mailbox_state WHERE state_key = ?",
                    (account.state_key,),
                ).fetchone()
            if retry["next_retry_at"] > time.time():
                continue
            try:
                self.poll_account(account)
            except Exception as exc:
                # Never expose provider exceptions: these may echo credentials or mail.
                auth_failure = isinstance(exc, MailboxLoginError)
                delay = min((300 if auth_failure else 30) * 2 ** min(retry["consecutive_failures"], 7), 3600)
                self._update(account, last_error="mailbox_auth_failed" if auth_failure else "mailbox_sync_failed",
                             next_retry_at=int(time.time()) + delay,
                             consecutive_failures=retry["consecutive_failures"] + 1)
            if index + 1 < len(self.accounts):
                self.stop.wait(5)

    def _loop(self):
        while not self.stop.is_set():
            self.poll_once()
            self.stop.wait(30)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="mailbox-receiver", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=20)
