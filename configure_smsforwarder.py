from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path


SENDER_NAME = "SMS Relay"
WEBHOOK_TYPE = 3


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def configure(db_path: Path, api_key: str, webhook_url: str, device_name: str) -> dict[str, object]:
    if len(api_key) != 64:
        raise ValueError("API key must contain exactly 64 characters")
    if not webhook_url.startswith("https://"):
        raise ValueError("Webhook URL must use HTTPS")

    body_template = compact_json(
        {
            "type": "sms",
            "from": "{{FROM}}",
            "content": "{{SMS}}",
            "received_at": "{{RECEIVE_TIME}}",
            "sim_info": "{{CARD_SLOT}}",
            "device_name": device_name,
            "app_version": "{{APP_VERSION}}",
        }
    )
    sender_setting = compact_json(
        {
            "method": "POST",
            "webServer": webhook_url,
            "secret": "",
            "response": '"ok":true',
            "webParams": body_template,
            "headers": {
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
            "proxyType": "DIRECT",
            "proxyHost": "",
            "proxyPort": "",
            "proxyAuthenticator": False,
            "proxyUsername": "",
            "proxyPassword": "",
        }
    )

    connection = sqlite3.connect(str(db_path), timeout=10)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        required_tables = {"Sender", "Rule"}
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not required_tables.issubset(tables):
            raise RuntimeError("SmsForwarder Sender/Rule tables were not found")

        now_ms = int(time.time() * 1000)
        matching_senders = connection.execute(
            """
            SELECT id FROM Sender
            WHERE type = ? AND (name = ? OR json_setting LIKE ?)
            ORDER BY id
            """,
            (WEBHOOK_TYPE, SENDER_NAME, f'%"webServer":"{webhook_url}"%'),
        ).fetchall()

        if matching_senders:
            sender_id = int(matching_senders[0][0])
            connection.execute(
                """
                UPDATE Sender
                SET type = ?, name = ?, json_setting = ?, status = 1, time = ?
                WHERE id = ?
                """,
                (WEBHOOK_TYPE, SENDER_NAME, sender_setting, now_ms, sender_id),
            )
            sender_action = "updated"
        else:
            cursor = connection.execute(
                """
                INSERT INTO Sender (type, name, json_setting, status, time)
                VALUES (?, ?, ?, 1, ?)
                """,
                (WEBHOOK_TYPE, SENDER_NAME, sender_setting, now_ms),
            )
            sender_id = int(cursor.lastrowid)
            sender_action = "inserted"

        matching_rules = connection.execute(
            """
            SELECT id FROM Rule
            WHERE type = 'sms' AND filed = 'transpond_all' AND sender_id = ?
            ORDER BY id
            """,
            (sender_id,),
        ).fetchall()
        if matching_rules:
            rule_id = int(matching_rules[0][0])
            connection.execute(
                """
                UPDATE Rule
                SET `check` = 'is', value = '', sms_template = '', regex_replace = '',
                    sim_slot = 'ALL', status = 1, time = ?, sender_list = ?,
                    sender_logic = 'ALL', silent_period_start = 0,
                    silent_period_end = 0, silent_day_of_week = ''
                WHERE id = ?
                """,
                (now_ms, str(sender_id), rule_id),
            )
            rule_action = "updated"
        else:
            cursor = connection.execute(
                """
                INSERT INTO Rule (
                    type, filed, `check`, value, sender_id, sms_template,
                    regex_replace, sim_slot, status, time, sender_list,
                    sender_logic, silent_period_start, silent_period_end,
                    silent_day_of_week
                ) VALUES (
                    'sms', 'transpond_all', 'is', '', ?, '', '', 'ALL', 1, ?, ?,
                    'ALL', 0, 0, ''
                )
                """,
                (sender_id, now_ms, str(sender_id)),
            )
            rule_id = int(cursor.lastrowid)
            rule_action = "inserted"

        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        journal_mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    finally:
        connection.close()

    return {
        "sender_id": sender_id,
        "sender_action": sender_action,
        "rule_id": rule_id,
        "rule_action": rule_action,
        "journal_mode": journal_mode,
        "webhook_url": webhook_url,
        "api_key_length": len(api_key),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure SmsForwarder through its Room database")
    parser.add_argument("db", type=Path)
    parser.add_argument("--webhook-url", required=True)
    parser.add_argument("--device-name", required=True)
    parser.add_argument("--api-key-env", default="SMS_RELAY_API_KEY")
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env, "")
    summary = configure(args.db, api_key, args.webhook_url, args.device_name)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
