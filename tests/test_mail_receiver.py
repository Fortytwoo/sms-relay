from __future__ import annotations

import imaplib
import json
import ssl
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from app import FeishuNotifier, RelayServer, enrich_message, fingerprint, init_db, open_db, parse_message
from mail_receiver import MAX_MAIL_BYTES, MailAccount, MailReceiver, load_accounts, parse_email, test_receive, test_send
from test_app import FakeCentralAuth


def raw_mail(content="Your OTP is a7C91d", subject="【测试平台】登录", *, html=False):
    mail = EmailMessage()
    mail["From"] = "Sender <no-reply@example.test>"
    mail["To"] = "hidden-alias@example.test"
    mail["Subject"] = subject
    mail["Date"] = "Tue, 22 Sep 2026 10:00:00 +0800"
    mail.set_content(content, subtype="html" if html else "plain")
    return mail


class FakeIMAP:
    def __init__(self):
        self.messages = {}
        self.validity = 1
        self.fetches = []
        self.fail_login = False
        self.fail_fetch = False
        self.closed = False
        self.login_calls = 0
        self.reported_size_delta = 0
        self.truncate_bytes = 0

    def login(self, username, password):
        self.login_calls += 1
        if self.fail_login:
            raise imaplib.IMAP4.error("secret echoed by a provider")
        return "OK", []

    def select(self, folder, readonly=False):
        assert readonly is True
        assert folder == '"INBOX"'
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        value = self.validity if name == "UIDVALIDITY" else max(self.messages, default=0) + 1
        return name, [str(value).encode()]

    def uid(self, command, *args):
        if command == "search":
            minimum = int(args[-1].split(":")[0])
            uids = [uid for uid in self.messages if uid >= minimum]
            if not uids and self.messages:
                uids = [max(self.messages)]
            return "OK", [b" ".join(str(uid).encode() for uid in uids)]
        assert command == "fetch" and "BODY.PEEK[]" in args[1]
        if self.fail_fetch:
            return "NO", [b"failure"]
        uid = int(args[0])
        self.fetches.append(uid)
        raw = self.messages[uid]
        reported_size = len(raw) + self.reported_size_delta
        fetched = raw[:-self.truncate_bytes] if self.truncate_bytes else raw
        return "OK", [(f"1 (UID {uid} RFC822.SIZE {reported_size} BODY[] {{x}})".encode(),
                       fetched[:MAX_MAIL_BYTES + 1]), b")"]

    def logout(self):
        self.closed = True


class MailConnectionTests(unittest.TestCase):
    def test_receive_checks_latest_header_read_only_without_ingesting(self):
        calls = []
        class Client:
            def login(self, username, password):
                calls.append(("login", username, password))
                return "OK", []
            def select(self, folder, readonly=False):
                calls.append(("select", folder, readonly))
                return "OK", [b"2"]
            def fetch(self, number, query):
                calls.append(("fetch", number, query))
                return "OK", []
            def logout(self):
                calls.append(("logout",))
        account = MailAccount("a", "a@example.test", "imap.example.test", "a@example.test", "secret")
        def factory(host, port, *, ssl_context, timeout):
            self.assertTrue(ssl_context.check_hostname)
            self.assertEqual(ssl_context.verify_mode, ssl.CERT_REQUIRED)
            return Client()
        test_receive(account, client_factory=factory)
        self.assertIn(("select", '"INBOX"', True), calls)
        self.assertIn(("fetch", "2", "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])"), calls)
        self.assertEqual(calls[-1], ("logout",))

    def test_send_uses_tls_and_addresses_mail_to_self(self):
        calls = []
        class Client:
            def login(self, username, password):
                calls.append(("login", username, password))
            def send_message(self, message):
                calls.append(("send", message["From"], message["To"]))
            def quit(self):
                calls.append(("quit",))
        account = MailAccount("a", "a@example.test", "imap.example.test", "a@example.test", "secret",
                              smtp_host="smtp.example.test")
        def factory(host, port, *, context, timeout):
            self.assertEqual((host, port, timeout), ("smtp.example.test", 465, 15))
            self.assertTrue(context.check_hostname)
            return Client()
        test_send(account, ssl_factory=factory)
        self.assertIn(("login", "a@example.test", "secret"), calls)
        self.assertIn(("send", "a@example.test", "a@example.test"), calls)
        self.assertEqual(calls[-1], ("quit",))


class MailReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "relay.db")
        self.server = RelayServer(("127.0.0.1", 0), "a" * 64, self.db,
                                  read_api_key="b" * 64, auth_client=FakeCentralAuth())
        self.account = MailAccount("work-a", "a@example.test", "imap.example.test", "a@example.test", "synthetic-secret")
        self.client = FakeIMAP()
        self.connections = []

    def tearDown(self):
        self.server.server_close()
        self.temp.cleanup()

    def factory(self, host, port, *, ssl_context, timeout):
        self.assertTrue(ssl_context.check_hostname)
        self.assertEqual(ssl_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(timeout, 15)
        self.connections.append(host)
        return self.client

    def receiver(self, accounts=None, ingest=None, factory=None):
        return MailReceiver(self.db, accounts or [self.account], ingest or self.server.ingest_message,
                            client_factory=factory or self.factory)

    def messages(self):
        with open_db(self.db) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM messages ORDER BY id")]

    def test_first_connection_skips_history_then_ingests_new_and_resumes(self):
        self.client.messages[5] = raw_mail().as_bytes()
        receiver = self.receiver()
        receiver.poll_account(self.account)
        self.assertEqual(self.messages(), [])
        self.assertEqual(self.client.fetches, [])
        self.client.messages[6] = raw_mail().as_bytes()
        receiver.poll_account(self.account)
        self.receiver().poll_account(self.account)
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["recipient"], self.account.address)
        self.assertEqual(enrich_message(messages[0])["verification_code"], "a7C91d")
        self.assertEqual(self.client.fetches, [6])
        self.assertTrue(receiver.status()[0]["last_success_at"])
        self.assertTrue(self.client.closed)

    def test_accounts_are_isolated_and_bad_account_does_not_block_next(self):
        bad = self.account
        good = replace(self.account, id="work-b", address="b@example.test", host="good.example.test", start_from="all")
        self.client.messages[1] = raw_mail().as_bytes()
        broken = FakeIMAP()
        broken.fail_login = True
        receiver = self.receiver([bad, good], factory=lambda host, *a, **k: broken if host == bad.host else self.client)
        with patch.object(receiver.stop, "wait", return_value=False):
            receiver.poll_once()
        self.assertEqual(self.messages()[0]["recipient"], good.address)
        statuses = receiver.status()
        self.assertEqual(statuses[0]["last_error"], "mailbox_auth_failed")
        self.assertTrue(statuses[1]["last_success_at"])
        self.assertNotIn("secret", json.dumps(statuses))

    def test_replay_after_commit_before_checkpoint_notifies_once(self):
        self.account = replace(self.account, start_from="all")
        self.client.messages[1] = raw_mail().as_bytes()
        notifications = []
        class Recorder:
            def send(self, message):
                notifications.append(message)
        self.server.notifier = Recorder()
        def crash_after_commit(payload):
            self.server.ingest_message(payload)
            raise RuntimeError("crash")
        receiver = self.receiver(ingest=crash_after_commit)
        receiver.poll_once()
        self.receiver().poll_account(self.account)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(len(notifications), 1)

    def test_network_failure_does_not_advance_cursor(self):
        self.account = replace(self.account, start_from="all")
        self.client.messages[1] = raw_mail().as_bytes()
        self.client.fail_fetch = True
        receiver = self.receiver()
        receiver.poll_once()
        self.assertEqual(self.messages(), [])
        self.client.fail_fetch = False
        with patch("mail_receiver.time.time", return_value=time.time() + 61):
            receiver.poll_once()
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(receiver.status()[0]["last_error"], "")

    def test_login_failure_backs_off_across_restart_and_recovers(self):
        self.client.fail_login = True
        receiver = self.receiver()
        receiver.poll_once()
        status = receiver.status()[0]
        self.assertEqual(status["last_error"], "mailbox_auth_failed")
        self.assertGreaterEqual(status["next_retry_at"], int(time.time()) + 299)
        self.receiver().poll_once()
        self.assertEqual(self.client.login_calls, 1)
        self.client.fail_login = False
        with patch("mail_receiver.time.time", return_value=status["next_retry_at"] + 1):
            receiver.poll_once()
        self.assertEqual(self.client.login_calls, 2)
        self.assertEqual(receiver.status()[0]["last_error"], "")
        self.assertEqual(receiver.status()[0]["next_retry_at"], 0)

    def test_uidvalidity_change_fails_closed_without_replaying(self):
        receiver = self.receiver()
        receiver.poll_account(self.account)
        self.client.validity = 2
        self.client.messages[1] = raw_mail().as_bytes()
        receiver.poll_account(self.account)
        self.assertEqual(self.messages(), [])
        self.assertEqual(receiver.status()[0]["last_error"], "uidvalidity_changed")

    def test_oversized_mail_is_counted_and_does_not_block_codes(self):
        self.account = replace(self.account, start_from="all")
        self.client.messages = {1: b"x" * (MAX_MAIL_BYTES + 5), 2: raw_mail().as_bytes()}
        receiver = self.receiver()
        receiver.poll_account(self.account)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(receiver.status()[0]["skipped_count"], 1)

    def test_provider_size_underestimate_does_not_block_new_mail(self):
        self.account = replace(self.account, start_from="all")
        self.client.messages[1] = raw_mail().as_bytes()
        # Observed Tencent response: RFC822.SIZE=7761, literal length=7763.
        self.client.reported_size_delta = -2
        receiver = self.receiver()
        receiver.poll_once()
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(enrich_message(self.messages()[0])["verification_code"], "a7C91d")
        self.assertEqual(receiver.status()[0]["last_error"], "")
        self.receiver().poll_account(self.account)
        self.assertEqual(len(self.messages()), 1)

    def test_short_fetch_is_retried_without_advancing_cursor(self):
        self.account = replace(self.account, start_from="all")
        self.client.messages[1] = raw_mail().as_bytes()
        self.client.truncate_bytes = 3
        receiver = self.receiver()
        receiver.poll_once()
        self.assertEqual(self.messages(), [])
        self.assertEqual(receiver.status()[0]["last_error"], "mailbox_sync_failed")
        self.client.truncate_bytes = 0
        receiver.poll_account(self.account)
        self.assertEqual(len(self.messages()), 1)

    def test_plain_html_subject_and_attachments_preserve_case(self):
        html = raw_mail("<head><style>OTP 999999</style></head><p>Your OTP is <b>a7C91d</b></p><script>OTP 111111</script>", html=True)
        html.add_attachment(b"Your OTP is 222222", maintype="text", subtype="plain", filename="attachment.txt")
        payload = parse_email(html.as_bytes(), self.account, "imap:test:1")
        self.assertEqual(payload["content"], "Your OTP is a7C91d")
        self.assertEqual(enrich_message(parse_message(payload))["tag"], "测试平台")
        mail = raw_mail("正文无验证码", subject="Your OTP is b8D123")
        self.assertEqual(enrich_message(parse_message(parse_email(mail.as_bytes(), self.account, "imap:test:2")))["verification_code"], "b8D123")
        alternative = raw_mail("Your OTP is A1b2C3")
        alternative.add_alternative("<p>Your OTP is 999999</p>", subtype="html")
        self.assertIn("A1b2C3", parse_email(alternative.as_bytes(), self.account, "imap:test:3")["content"])

    def test_two_mailboxes_same_content_and_concurrent_retries(self):
        payload = parse_email(raw_mail().as_bytes(), self.account, "same-id")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self.server.ingest_message, [payload] * 12))
        self.assertEqual(sum(not result["duplicate"] for result in results), 1)
        other = self.server.ingest_message({**payload, "recipient": "b@example.test"})
        new_mail = self.server.ingest_message({**payload, "source_message_id": "new-id"})
        self.assertFalse(other["duplicate"])
        self.assertFalse(new_mail["duplicate"])
        self.assertEqual(len(self.messages()), 3)

    def test_mail_notification_identifies_receiving_account(self):
        calls = []
        class Client:
            def request(self, *args, **kwargs):
                calls.append(kwargs)
        notifier = FeishuNotifier("", "", "test-chat", client=Client())
        notifier.send({"id": 1, "message_type": "email", "verification_code": "a7C91d",
                       "recipient": self.account.address, "sender": "source@example.test"})
        self.assertEqual(calls[0]["payload"]["msg_type"], "interactive")
        text = json.dumps(json.loads(calls[0]["payload"]["content"]), ensure_ascii=False)
        self.assertIn("接收邮箱：a@example.test", text)
        self.assertIn("a7C91d", text)

    def test_configuration_validation_and_secret_redaction(self):
        path = Path(self.temp.name) / "accounts.json"
        record = {"id": "a", "address": "a@example.test", "host": "imap.exmail.qq.com", "password": "private-test-password"}
        path.write_text(json.dumps([record]), encoding="utf-8")
        account = load_accounts(str(path))[0]
        self.assertEqual(account.username, record["address"])
        self.assertNotIn(record["password"], repr(account))
        for records in ([record, record], [{**record, "port": 143, "tls": False}], [{**record, "folder": "INBOX\r\n"}], [{**record, "address": "bad"}], {}):
            path.write_text(json.dumps(records), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid mailbox configuration"):
                load_accounts(str(path))

    def test_legacy_database_migration_preserves_sms_identity(self):
        path = str(Path(self.temp.name) / "old.db")
        message = parse_message({"from": "10086", "content": "OTP 123456"})
        key = fingerprint(message)
        with open_db(path) as connection:
            connection.execute("""CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL,
                message_type TEXT NOT NULL, sender TEXT NOT NULL, content TEXT NOT NULL,
                source_received_at TEXT NOT NULL, sim_info TEXT NOT NULL,
                device_name TEXT NOT NULL, app_version TEXT NOT NULL,
                message_key TEXT NOT NULL UNIQUE, source_ip TEXT NOT NULL)""")
            connection.execute("INSERT INTO messages VALUES (1, '', 'sms', '10086', 'OTP 123456', '', '', '', '', ?, '')", (key,))
        init_db(path)
        init_db(path)
        with open_db(path) as connection:
            row = connection.execute("SELECT id, content, message_key, recipient, subject, source_message_id FROM messages").fetchone()
        self.assertEqual(tuple(row), (1, "OTP 123456", key, "", "", ""))


if __name__ == "__main__":
    unittest.main()
