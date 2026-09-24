"""Server-side mailbox configuration with atomic, private file updates."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from mail_receiver import MailAccount, parse_accounts


PUBLIC_FIELDS = ("id", "address", "host", "port", "username", "folder",
                 "start_from", "smtp_host", "smtp_port", "smtp_security", "smtp_username")


class MailboxConfig:
    def __init__(self, path: str, accounts: list[MailAccount]):
        self.path = Path(path)
        self.accounts = list(accounts)
        self.lock = threading.RLock()

    def list_public(self) -> list[dict]:
        with self.lock:
            return [{**{field: getattr(a, field) for field in PUBLIC_FIELDS},
                     "password_set": bool(a.password), "smtp_password_set": bool(a.smtp_password)}
                    for a in self.accounts]

    def get(self, account_id: str) -> MailAccount | None:
        with self.lock:
            return next((a for a in self.accounts if a.id == account_id), None)

    def save(self, payload: object, *, original_id: str | None = None) -> list[MailAccount]:
        if not isinstance(payload, dict):
            raise ValueError("invalid_mailbox")
        if set(payload) - set(MailAccount.__dataclass_fields__):
            raise ValueError("invalid_mailbox")
        with self.lock:
            original = self.get(original_id) if original_id else None
            if original_id and original is None:
                raise KeyError("not_found")
            values = dict(payload)
            if original:
                if values.get("id") != original.id:
                    raise ValueError("mailbox_id_cannot_change")
                for secret in ("password", "smtp_password"):
                    if not values.get(secret):
                        values[secret] = getattr(original, secret)
            values.setdefault("username", values.get("address"))
            records = [asdict(a) for a in self.accounts if a.id != original_id]
            records.append(values)
            try:
                accounts = parse_accounts(records)
            except ValueError:
                raise ValueError("invalid_mailbox") from None
            self._write(accounts)
            self.accounts = accounts
            return list(accounts)

    def delete(self, account_id: str) -> list[MailAccount]:
        with self.lock:
            if not self.get(account_id):
                raise KeyError("not_found")
            accounts = [a for a in self.accounts if a.id != account_id]
            self._write(accounts)
            self.accounts = accounts
            return list(accounts)

    def _write(self, accounts: list[MailAccount]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".mailboxes-", dir=self.path.parent)
            os.chmod(temp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump([asdict(a) for a in accounts], file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)


def default_config_path(db_path: str) -> str:
    return str(Path(db_path).with_name("mailboxes.json"))
