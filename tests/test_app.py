from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from app import (
    FeishuNotifier,
    RelayServer,
    extract_message_tag,
    extract_verification_code,
    extract_email_verification_code,
    build_verification_card,
    identify_platform,
    parse_sim_info,
)
from central_auth import CentralAuthUnavailable


WRITE_API_KEY = "a" * 64
READ_API_KEY = "b" * 64


class FakeCentralAuth:
    issuer = "https://auth.example.test"
    client_id = "sms-relay-web"
    audience = "sms-relay-api"
    scopes = ("sms-relay:access",)
    redirect_uri = "https://relay.example.test/sms-relay/auth/callback"
    post_logout_redirect_uri = (
        "https://relay.example.test/sms-relay/?auto_sso=off"
    )

    def __init__(self) -> None:
        self.exchange_calls: list[tuple[str, str]] = []
        self.introspect_calls: list[str] = []
        self.refresh_calls: list[str] = []
        self.revoke_calls: list[str] = []
        self.revoke_error: Exception | None = None
        self.introspection = {
            "active": True,
            "principal": {
                "id": "union-test",
                "unionId": "union-test",
                "openId": "ou_test",
                "clientId": self.client_id,
                "scopes": ["sms-relay:access"],
                "name": "测试用户",
            },
        }

    def authorization_url(self, state: str, code_challenge: str) -> str:
        return self.issuer + "/oauth/authorize?" + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(self.scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )

    def exchange_code(self, code: str, verifier: str) -> dict:
        self.exchange_calls.append((code, verifier))
        return {
            "access_token": "central-access-token",
            "refresh_token": "central-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "sms-relay:access",
        }

    def introspect(self, access_token: str) -> dict:
        self.introspect_calls.append(access_token)
        return self.introspection

    def refresh(self, refresh_token: str) -> dict:
        self.refresh_calls.append(refresh_token)
        return {
            "access_token": "rotated-access-token",
            "refresh_token": "rotated-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "sms-relay:access",
        }

    def revoke(self, refresh_token: str) -> None:
        self.revoke_calls.append(refresh_token)
        if self.revoke_error is not None:
            raise self.revoke_error


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RelayApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "relay.db")
        self.auth = FakeCentralAuth()
        self.server = RelayServer(
            ("127.0.0.1", 0),
            WRITE_API_KEY,
            self.db_path,
            read_api_key=READ_API_KEY,
            auth_client=self.auth,
            introspection_cache_seconds=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, str] | None = None,
        api_key: str | None = None,
        cookie: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        headers: dict[str, str] = dict(extra_headers or {})
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if api_key is not None:
            headers["X-API-Key"] = api_key
        if cookie is not None:
            headers["Cookie"] = cookie
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def request_text(self, path: str) -> tuple[int, str, dict[str, str]]:
        with urllib.request.urlopen(self.base_url + path, timeout=3) as response:
            headers = {name.lower(): value for name, value in response.headers.items()}
            return response.status, response.read().decode("utf-8"), headers

    def request_raw(
        self,
        method: str,
        path: str,
        *,
        cookie: str | None = None,
    ) -> tuple[int, bytes, object]:
        headers = {"Cookie": cookie} if cookie else {}
        request = urllib.request.Request(
            self.base_url + path, headers=headers, method=method
        )
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(request, timeout=3) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def login(self) -> tuple[str, str]:
        login_status, _, login_headers = self.request_raw("GET", "/auth/login")
        self.assertEqual(login_status, 302)
        location = login_headers["Location"]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        transaction_cookie = login_headers["Set-Cookie"].split(";", 1)[0]
        callback = (
            "/auth/callback?"
            + urllib.parse.urlencode(
                {
                    "code": "test-code",
                    "state": query["state"][0],
                    "iss": self.auth.issuer,
                }
            )
        )
        callback_status, _, callback_headers = self.request_raw(
            "GET", callback, cookie=transaction_cookie
        )
        self.assertEqual(callback_status, 302)
        cookies = callback_headers.get_all("Set-Cookie")
        session_cookie = next(
            value.split(";", 1)[0]
            for value in cookies
            if value.startswith("sms_relay_session=") and "Max-Age=0" not in value
        )
        return session_cookie, transaction_cookie

    def test_health_is_public(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {"ok": True, "status": "healthy"},
        )

    def test_mailbox_configuration_requires_session_and_origin_and_redacts_secrets(self) -> None:
        path = "/v1/mailboxes/config"
        origin = {"Origin": self.base_url}
        payload = {"id": "work-a", "address": "work@example.test", "host": "imap.example.test",
                   "username": "", "password": "synthetic-secret", "smtp_host": "smtp.example.test"}
        self.assertEqual(self.request("GET", path, api_key=READ_API_KEY)[0], 401)
        self.assertEqual(self.request("POST", path, payload, api_key=WRITE_API_KEY,
                                      extra_headers=origin)[0], 401)
        cookie, _ = self.login()
        self.assertEqual(self.request("POST", path, payload, cookie=cookie)[0], 403)
        self.assertEqual(self.request("POST", path, payload, cookie=cookie,
                                      extra_headers={"Origin": "https://evil.example.test"})[0], 403)
        with patch("mail_receiver.MailReceiver.start"):
            status, body = self.request("POST", path, payload, cookie=cookie, extra_headers=origin)
        self.assertEqual(status, 200)
        self.assertNotIn("synthetic-secret", json.dumps(body))
        self.assertTrue(body["mailboxes"][0]["password_set"])
        config_path = Path(self.db_path).with_name("mailboxes.json")
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))[0]["password"], "synthetic-secret")
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))[0]["username"], "work@example.test")
        self.assertEqual(len(self.server.mail_receiver.accounts), 1)
        edit = {**payload, "password": "", "smtp_password": "", "smtp_port": 587,
                "smtp_security": "starttls"}
        status, _ = self.request("PUT", path + "/work-a", edit, cookie=cookie, extra_headers=origin)
        self.assertEqual(status, 200)
        saved = json.loads(config_path.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["password"], "synthetic-secret")
        self.assertEqual(saved["smtp_port"], 587)
        self.assertEqual(self.request("POST", path, {**payload, "id": "work-b", "address": payload["address"]},
                                      cookie=cookie, extra_headers=origin)[0], 400)
        self.assertEqual(self.request("DELETE", path + "/work-a", cookie=cookie,
                                      extra_headers=origin)[0], 200)
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), [])
        self.assertEqual(self.server.mail_receiver.accounts, [])

    def test_mailbox_connection_tests_use_saved_account_and_sanitize_failures(self) -> None:
        cookie, _ = self.login()
        origin = {"Origin": self.base_url}
        payload = {"id": "work-a", "address": "work@example.test", "host": "imap.example.test",
                   "password": "synthetic-secret", "smtp_host": "smtp.example.test"}
        with patch("mail_receiver.MailReceiver.start"):
            self.assertEqual(self.request("POST", "/v1/mailboxes/config", payload,
                                          cookie=cookie, extra_headers=origin)[0], 200)
        with patch("mail_receiver.test_receive") as receive, patch("mail_receiver.test_send") as send:
            for mode in ("receive", "send"):
                status, _ = self.request("POST", f"/v1/mailboxes/config/work-a/test/{mode}",
                                         cookie=cookie, extra_headers=origin)
                self.assertEqual(status, 200)
            self.assertEqual(receive.call_count, 1)
            self.assertEqual(send.call_count, 1)
            send.side_effect = ValueError("synthetic-secret provider detail")
            status, body = self.request("POST", "/v1/mailboxes/config/work-a/test/send",
                                        cookie=cookie, extra_headers=origin)
            self.assertEqual(status, 502)
            self.assertEqual(body["error"], "smtp_test_failed")
            self.assertNotIn("synthetic-secret", json.dumps(body))

    def test_web_ui_and_assets_are_public_with_security_headers(self) -> None:
        status, page, headers = self.request_text("/")
        css_status, css, _ = self.request_text("/assets/app.css")
        js_status, js, _ = self.request_text("/assets/app.js")
        visual_status, visual, visual_headers = self.request_text("/assets/login-visual.svg")

        self.assertEqual(status, 200)
        self.assertIn("短信中转", page)
        self.assertIn('id="feishu-login"', page)
        self.assertIn('class="auth-shell"', page)
        self.assertIn('class="auth-copy"', page)
        self.assertIn('class="auth-visual"', page)
        self.assertIn("统一身份服务", page)
        self.assertIn("安全访问工作空间", page)
        self.assertIn("SMS Relay 短信中继", page)
        self.assertIn("使用飞书安全登录", page)
        self.assertIn("仅用于身份验证，不会读取或修改业务数据。", page)
        self.assertIn("一次验证，全局通行", page)
        self.assertIn('src="assets/login-visual.svg?v=1.3.0"', page)
        self.assertIn('id="detail-tag-row"', page)
        self.assertIn('id="detail-phone"', page)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertEqual(css_status, 200)
        self.assertIn("--color-primary", css)
        self.assertIn("grid-template-columns: 56.46% 43.54%", css)
        self.assertIn(".is-auth-view .topbar", css)
        self.assertEqual(js_status, 200)
        self.assertIn('document.body.classList.add("is-auth-view")', js)
        self.assertIn('document.body.classList.remove("is-auth-view")', js)
        self.assertNotIn("sessionStorage", js)
        self.assertNotIn("localStorage", js)
        self.assertNotIn("innerHTML", js)
        self.assertIn("message.tag", js)
        self.assertEqual(visual_status, 200)
        self.assertIn("<svg", visual)
        self.assertTrue(visual_headers["content-type"].startswith("image/svg+xml"))

    def test_messages_require_api_key(self) -> None:
        status, body = self.request("GET", "/v1/messages")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_email_ingestion_validation_filters_and_incremental_cursor(self) -> None:
        payload = {"type": "email", "from": "sender@example.test", "recipient": "a@example.test",
                   "subject": "【测试】Your OTP is a7C91d", "content": "邮件正文",
                   "source_message_id": "message-1"}
        for changes in ({"recipient": ""}, {"source_message_id": ""}):
            status, _ = self.request("POST", "/v1/messages", {**payload, **changes}, WRITE_API_KEY)
            self.assertEqual(status, 400)
        _, first = self.request("POST", "/v1/messages", payload, WRITE_API_KEY)
        _, duplicate = self.request("POST", "/v1/messages", payload, WRITE_API_KEY)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["id"], duplicate["id"])
        self.request("POST", "/v1/messages", {**payload, "recipient": "b@example.test"}, WRITE_API_KEY)
        self.request("POST", "/v1/messages", {"content": "短信"}, WRITE_API_KEY)
        status, page = self.request("GET", "/v1/messages?message_type=email&after_id=0&limit=1", api_key=READ_API_KEY)
        self.assertEqual(status, 200)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["messages"][0]["verification_code"], "a7C91d")
        self.assertEqual(page["messages"][0]["tag"], "测试")
        status, page2 = self.request("GET", f"/v1/messages?message_type=email&after_id={page['next_after_id']}&limit=1", api_key=READ_API_KEY)
        self.assertEqual(page2["messages"][0]["recipient"], "b@example.test")
        self.assertFalse(page2["has_more"])
        _, filtered = self.request("GET", "/v1/messages?recipient=a%40example.test", api_key=READ_API_KEY)
        self.assertEqual(filtered["count"], 1)
        _, sms = self.request("GET", "/v1/messages?message_type=sms", api_key=READ_API_KEY)
        self.assertEqual(sms["count"], 1)
        self.assertEqual(self.request("GET", "/v1/messages?message_type=bogus", api_key=READ_API_KEY)[0], 400)

    def test_mailbox_status_uses_read_auth_without_credentials(self) -> None:
        from mail_receiver import MailAccount, MailReceiver
        account = MailAccount("a", "a@example.test", "imap.example.test", "test-login", "synthetic-secret")
        self.server.mail_receiver = MailReceiver(self.db_path, [account], self.server.ingest_message)
        for key in (None, WRITE_API_KEY):
            self.assertEqual(self.request("GET", "/v1/mailboxes", api_key=key)[0], 401)
        status, payload = self.request("GET", "/v1/mailboxes", api_key=READ_API_KEY)
        self.assertEqual(status, 200)
        self.assertEqual(payload["mailboxes"][0]["address"], account.address)
        self.assertNotIn("synthetic-secret", json.dumps(payload))
        self.assertNotIn("test-login", json.dumps(payload))

    def test_email_action_code_notifies_once_with_card(self) -> None:
        calls = []
        class Client:
            def request(self, *args, **kwargs):
                calls.append(kwargs["payload"])
                return {"code": 0}
        self.server.notifier = FeishuNotifier("", "", "test-chat", client=Client())
        payload = {"type": "email", "recipient": "a@example.test", "source_message_id": "new-code",
                   "subject": "邮箱验证", "content": "请在48小时内输入以下代码完成验证：\n a7C91d"}
        _, first = self.request("POST", "/v1/messages", payload, WRITE_API_KEY)
        _, duplicate = self.request("POST", "/v1/messages", payload, WRITE_API_KEY)
        self.assertEqual(first["lark_push_status"], "sent")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["msg_type"], "interactive")
        card = json.loads(calls[0]["content"])
        button = next(x for x in card["body"]["elements"] if x["tag"] == "button")
        self.assertTrue(button["behaviors"][0]["default_url"].endswith(f"/copy?message_id={first['id']}"))

    def test_copy_page_and_code_endpoint_enforce_read_authorization(self) -> None:
        _, stored = self.request("POST", "/v1/messages", {"content": "OTP a7C91d"}, WRITE_API_KEY)
        path = f"/v1/messages/{stored['id']}/code"
        for key in (None, WRITE_API_KEY):
            self.assertEqual(self.request("GET", path, api_key=key)[0], 401)
        status, payload = self.request("GET", path, api_key=READ_API_KEY)
        self.assertEqual(status, 200)
        self.assertEqual(payload["message"]["verification_code"], "a7C91d")
        self.assertNotIn("content", payload["message"])
        self.assertNotIn("source_ip", payload["message"])
        self.assertEqual(self.request("GET", "/v1/messages/9999999/code", api_key=READ_API_KEY)[0], 404)
        cookie, _ = self.login()
        self.assertEqual(self.request("GET", path, cookie=cookie)[0], 200)
        self.auth.introspection["active"] = False
        self.assertEqual(self.request("GET", path, cookie=cookie)[0], 401)
        status, html, headers = self.request_text(f"/copy?message_id={stored['id']}")
        self.assertEqual(status, 200)
        self.assertNotIn("a7C91d", html)
        self.assertEqual(headers["cache-control"], "no-store")

    def test_insert_deduplicates_and_lists_utf8_message(self) -> None:
        payload = {
            "type": "sms",
            "from": "10086",
            "content": "您的验证码为 483921，5 分钟内有效",
            "received_at": "2026-08-07 10:00:00",
            "sim_info": "SIM2_13800000000",
            "device_name": "sunstone",
            "app_version": "3.3.3.250214",
        }
        first_status, first = self.request("POST", "/v1/messages", payload, WRITE_API_KEY)
        second_status, second = self.request("POST", "/v1/messages", payload, WRITE_API_KEY)
        list_status, listed = self.request(
            "GET", "/v1/messages?limit=10", api_key=READ_API_KEY
        )

        self.assertEqual(first_status, 200)
        self.assertFalse(first["duplicate"])
        self.assertEqual(second_status, 200)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["count"], 1)
        message = listed["messages"][0]
        self.assertEqual(message["content"], "您的验证码为 483921，5 分钟内有效")
        self.assertEqual(message["verification_code"], "483921")
        self.assertEqual(message["sim_slot"], "SIM2")
        self.assertEqual(message["sim_phone"], "13800000000")

    def test_duplicate_detection_ignores_delivery_client_metadata(self) -> None:
        original = {
            "type": "sms",
            "from": "10690000",
            "content": "【平台】验证码 a7C91d，请勿泄露",
            "received_at": "2026-08-25 11:45:00",
            "sim_info": "SIM2_中国电信_13900000000",
            "device_name": "Xiaomi 22101317C",
            "app_version": "3.5.0.260224",
        }
        compensating_client = dict(original)
        compensating_client.update(
            {
                "sim_info": "SIM2_中国电信_",
                "device_name": "Xiaomi 22101317C reliable outbox",
                "app_version": "reliable-outbox/1.1.0",
            }
        )

        first_status, first = self.request("POST", "/v1/messages", original, WRITE_API_KEY)
        second_status, second = self.request(
            "POST", "/v1/messages", compensating_client, WRITE_API_KEY
        )
        list_status, listed = self.request(
            "GET", "/v1/messages?limit=10", api_key=READ_API_KEY
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["count"], 1)

    def test_duplicate_detection_tolerates_source_timestamp_drift(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.messages: list[dict] = []

            def send(self, message: dict) -> None:
                self.messages.append(message)

        recorder = Recorder()
        self.server.notifier = recorder
        sms_forwarder = {
            "type": "sms",
            "from": "10690000",
            "content": "【抖音商城】订单导出文件已加密，解压密码为gVtFmd，请妥善保管",
            "received_at": "2026-08-26 12:55:04",
            "sim_info": "SIM2_中国电信_13900000000",
            "device_name": "Xiaomi 22101317C",
            "app_version": "3.5.0.260224",
        }
        reliable_outbox = dict(sms_forwarder)
        reliable_outbox.update(
            {
                "received_at": "2026-08-26 12:55:02",
                "sim_info": "SIM2_中国电信_",
                "device_name": "Xiaomi 22101317C reliable outbox",
                "app_version": "reliable-outbox/1.1.0",
            }
        )

        first_status, first = self.request(
            "POST", "/v1/messages", sms_forwarder, WRITE_API_KEY
        )
        second_status, second = self.request(
            "POST", "/v1/messages", reliable_outbox, WRITE_API_KEY
        )
        list_status, listed = self.request(
            "GET", "/v1/messages?limit=10", api_key=READ_API_KEY
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["message_key"], first["message_key"])
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(len(recorder.messages), 1)
        self.assertEqual(recorder.messages[0]["verification_code"], "gVtFmd")

    def test_same_message_outside_timestamp_drift_window_is_not_duplicate(self) -> None:
        original = {
            "type": "sms",
            "from": "10690000",
            "content": "【平台】验证码 482701，请勿泄露",
            "received_at": "2026-08-26 12:55:04",
        }
        later_message = dict(original)
        later_message["received_at"] = "2026-08-26 12:56:05"

        first_status, first = self.request(
            "POST", "/v1/messages", original, WRITE_API_KEY
        )
        second_status, second = self.request(
            "POST", "/v1/messages", later_message, WRITE_API_KEY
        )
        list_status, listed = self.request(
            "GET", "/v1/messages?limit=10", api_key=READ_API_KEY
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertFalse(first["duplicate"])
        self.assertFalse(second["duplicate"])
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["count"], 2)

    def test_concurrent_timestamp_drift_deliveries_only_notify_once(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.messages: list[dict] = []

            def send(self, message: dict) -> None:
                self.messages.append(message)

        recorder = Recorder()
        self.server.notifier = recorder
        first_payload = {
            "type": "sms",
            "from": "10690000",
            "content": "【抖音商城】解压密码为AbCdEf，请妥善保管",
            "received_at": "2026-08-26 13:06:54",
            "app_version": "3.5.0.260224",
        }
        second_payload = dict(first_payload)
        second_payload.update(
            {
                "received_at": "2026-08-26 13:06:55",
                "app_version": "reliable-outbox/1.1.0",
            }
        )
        barrier = threading.Barrier(3)
        results: list[tuple[int, dict]] = []
        errors: list[BaseException] = []

        def submit(payload: dict[str, str]) -> None:
            try:
                barrier.wait()
                results.append(
                    self.request("POST", "/v1/messages", payload, WRITE_API_KEY)
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=submit, args=(payload,))
            for payload in (first_payload, second_payload)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual([status for status, _body in results], [200, 200])
        bodies = [body for _status, body in results]
        self.assertEqual(sorted(body["duplicate"] for body in bodies), [False, True])
        self.assertEqual(len({body["id"] for body in bodies}), 1)
        self.assertEqual(len({body["message_key"] for body in bodies}), 1)
        self.assertEqual(len(recorder.messages), 1)

    def test_message_list_includes_tag_extracted_from_sms_signature(self) -> None:
        status, inserted = self.request(
            "POST",
            "/v1/messages",
            {
                "from": "10690000",
                "content": "【小红书】验证码 682143，请勿泄露",
                "sim_info": "SIM1_13900000000",
            },
            WRITE_API_KEY,
        )
        list_status, listed = self.request(
            "GET", "/v1/messages?limit=10", api_key=READ_API_KEY
        )

        self.assertEqual(status, 200)
        self.assertFalse(inserted["duplicate"])
        self.assertEqual(inserted["tag"], "小红书")
        self.assertEqual(inserted["sim_slot"], "SIM1")
        self.assertEqual(inserted["sim_phone"], "13900000000")
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["messages"][0]["tag"], "小红书")

    def test_identify_platform_endpoint_uses_authenticated_exact_host_matching(self) -> None:
        cases = {
            "https://ark.xiaohongshu.com/app-order/order/query": "小红书",
            "https://s.kwaixiaodian.com/zone/order/list": "快手",
            "https://zhaoshang.dxycare.com/system/download/index?pageSize=20&pageNo=1": "丁香",
            "https://portal.maiscrm.com/navigator#/taskCenter": "私域商城",
            "https://store.weixin.qq.com/shop/order/list": "微信小店",
            "https://fxg.jinritemai.com/ffa/morder/order/list": "抖音商城",
            "https://doudian.douyinec.com/login/common": "抖音商城",
        }

        unauthenticated_status, _ = self.request(
            "GET",
            "/v1/platforms/identify?url=https%3A%2F%2Fark.xiaohongshu.com%2F",
        )
        self.assertEqual(unauthenticated_status, 401)

        for url, expected in cases.items():
            with self.subTest(url=url):
                encoded_url = urllib.parse.quote(url, safe="")
                status, body = self.request(
                    "GET",
                    f"/v1/platforms/identify?url={encoded_url}",
                    api_key=READ_API_KEY,
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["tag"], expected)
                self.assertTrue(body["recognized"])

        encoded_lookalike = urllib.parse.quote(
            "https://ark.xiaohongshu.com.example.com/app-order/order/query", safe=""
        )
        status, body = self.request(
            "GET",
            f"/v1/platforms/identify?url={encoded_lookalike}",
            api_key=READ_API_KEY,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["tag"], "")
        self.assertFalse(body["recognized"])

    def test_central_oauth_uses_pkce_and_opaque_server_session(self) -> None:
        payload = {
            "type": "sms",
            "from": "10690000",
            "content": "动态码：5729",
            "sim_info": "SIM1_13900000000",
        }
        self.request("POST", "/v1/messages", payload, WRITE_API_KEY)
        login_status, _, login_headers = self.request_raw("GET", "/auth/login")
        self.assertEqual(login_status, 302)
        location = login_headers["Location"]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("code_verifier", query)
        self.assertNotIn("central-", login_headers["Set-Cookie"])

        transaction_cookie = login_headers["Set-Cookie"].split(";", 1)[0]
        callback_path = "/auth/callback?" + urllib.parse.urlencode(
            {
                "code": "test-code",
                "state": query["state"][0],
                "iss": self.auth.issuer,
            }
        )
        callback_status, _, callback_headers = self.request_raw(
            "GET", callback_path, cookie=transaction_cookie
        )
        self.assertEqual(callback_status, 302)
        callback_cookies = callback_headers.get_all("Set-Cookie")
        cookie = next(
            value.split(";", 1)[0]
            for value in callback_cookies
            if value.startswith("sms_relay_session=") and "Max-Age=0" not in value
        )
        self.assertNotIn("central-access-token", cookie)
        self.assertNotIn("central-refresh-token", cookie)
        self.assertNotIn("ou_test", cookie)

        session_status, session = self.request("GET", "/auth/session", cookie=cookie)
        list_status, listed = self.request("GET", "/v1/messages", cookie=cookie)

        self.assertEqual(session_status, 200)
        self.assertEqual(session["user"]["name"], "测试用户")
        self.assertEqual(session["authentication"], "central_oauth")
        self.assertNotIn("csrf_token", session)
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["messages"][0]["verification_code"], "5729")

    def test_callback_rejects_duplicate_issuer_mismatch_binding_and_replay(self) -> None:
        status, _, headers = self.request_raw("GET", "/auth/login")
        self.assertEqual(status, 302)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)
        state = query["state"][0]
        transaction_cookie = headers["Set-Cookie"].split(";", 1)[0]

        duplicate_status, _, _ = self.request_raw(
            "GET",
            "/auth/callback?code=a&state=" + state + "&state=second&iss="
            + urllib.parse.quote(self.auth.issuer, safe=""),
            cookie=transaction_cookie,
        )
        self.assertEqual(duplicate_status, 400)

        mismatch_status, _, _ = self.request_raw(
            "GET",
            "/auth/callback?"
            + urllib.parse.urlencode(
                {"code": "a", "state": state, "iss": "https://wrong.example"}
            ),
            cookie=transaction_cookie,
        )
        self.assertEqual(mismatch_status, 400)

        status, _, headers = self.request_raw("GET", "/auth/login")
        state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["state"][0]
        binding_status, _, _ = self.request_raw(
            "GET",
            "/auth/callback?"
            + urllib.parse.urlencode(
                {"code": "a", "state": state, "iss": self.auth.issuer}
            ),
            cookie="sms_relay_oauth_tx=wrong-browser",
        )
        self.assertEqual(binding_status, 400)

        transaction_cookie = headers["Set-Cookie"].split(";", 1)[0]
        callback = "/auth/callback?" + urllib.parse.urlencode(
            {"code": "a", "state": state, "iss": self.auth.issuer}
        )
        success_status, _, _ = self.request_raw(
            "GET", callback, cookie=transaction_cookie
        )
        replay_status, _, _ = self.request_raw(
            "GET", callback, cookie=transaction_cookie
        )
        self.assertEqual(success_status, 302)
        self.assertEqual(replay_status, 400)

    def test_oauth_transaction_is_consumed_once_under_concurrency(self) -> None:
        state, verifier, browser_binding = self.server.create_oauth_transaction()
        barrier = threading.Barrier(3)
        results: list[str | None] = []
        errors: list[Exception] = []

        def consume() -> None:
            try:
                barrier.wait()
                results.append(
                    self.server.consume_oauth_transaction(state, browser_binding)
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(errors, [])
        self.assertEqual(results.count(verifier), 1)
        self.assertEqual(results.count(None), 1)

    def test_introspection_enforces_client_scope_and_active_state(self) -> None:
        cases = [
            ({"active": False}, 403),
            (
                {
                    "active": True,
                    "principal": {
                        "id": "union-test",
                        "clientId": "other-client",
                        "scopes": ["sms-relay:access"],
                    },
                },
                403,
            ),
            (
                {
                    "active": True,
                    "principal": {
                        "id": "union-test",
                        "clientId": self.auth.client_id,
                        "scopes": [],
                    },
                },
                403,
            ),
        ]
        for introspection, expected_status in cases:
            with self.subTest(introspection=introspection):
                self.auth.introspection = introspection
                status, _, headers = self.request_raw("GET", "/auth/login")
                state = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(headers["Location"]).query
                )["state"][0]
                transaction_cookie = headers["Set-Cookie"].split(";", 1)[0]
                callback_status, _, _ = self.request_raw(
                    "GET",
                    "/auth/callback?"
                    + urllib.parse.urlencode(
                        {"code": "a", "state": state, "iss": self.auth.issuer}
                    ),
                    cookie=transaction_cookie,
                )
                self.assertEqual(callback_status, expected_status)
        self.assertEqual(
            self.auth.revoke_calls,
            ["central-refresh-token", "central-refresh-token", "central-refresh-token"],
        )

    def test_refresh_rotates_server_side_token_atomically(self) -> None:
        cookie, _ = self.login()
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("UPDATE oauth_sessions SET access_expires_at = 0")
            connection.commit()
        finally:
            connection.close()

        status, _ = self.request("GET", "/auth/session", cookie=cookie)

        self.assertEqual(status, 200)
        self.assertEqual(self.auth.refresh_calls, ["central-refresh-token"])
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT access_token, refresh_token FROM oauth_sessions"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, ("rotated-access-token", "rotated-refresh-token"))

    def test_logout_revokes_before_deleting_and_fails_closed(self) -> None:
        cookie, _ = self.login()
        self.auth.revoke_error = CentralAuthUnavailable("unavailable")
        failed_status, _, failed_headers = self.request_raw(
            "POST", "/auth/logout", cookie=cookie
        )
        still_valid_status, _ = self.request("GET", "/auth/session", cookie=cookie)
        self.assertEqual(failed_status, 503)
        self.assertEqual(failed_headers.get_all("Set-Cookie"), None)
        self.assertEqual(still_valid_status, 200)

        self.auth.revoke_error = None
        logout_status, _, logout_headers = self.request_raw(
            "POST", "/auth/logout", cookie=cookie
        )
        revoked_status, _ = self.request("GET", "/auth/session", cookie=cookie)
        self.assertEqual(logout_status, 200)
        self.assertEqual(
            self.auth.revoke_calls,
            ["central-refresh-token", "central-refresh-token"],
        )
        self.assertIn("Max-Age=0", logout_headers["Set-Cookie"])
        self.assertEqual(revoked_status, 401)

    def test_new_verification_message_is_sent_to_feishu_notifier(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.messages: list[dict] = []

            def send(self, message: dict) -> None:
                self.messages.append(message)

        recorder = Recorder()
        self.server.notifier = recorder
        status, body = self.request(
            "POST",
            "/v1/messages",
            {
                "from": "10690000",
                "content": "验证码：682143",
                "sim_info": "SIM1_13900000000",
            },
            WRITE_API_KEY,
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["lark_push_status"], "sent")
        self.assertEqual(len(recorder.messages), 1)
        self.assertEqual(recorder.messages[0]["verification_code"], "682143")
        self.assertEqual(recorder.messages[0]["sim_phone"], "13900000000")

    def test_rejects_non_json(self) -> None:
        request = urllib.request.Request(
            self.base_url + "/v1/messages",
            data=b"hello",
            headers={"X-API-Key": WRITE_API_KEY, "Content-Type": "text/plain"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 415)

    def test_api_key_must_be_64_characters(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 64"):
            RelayServer(
                ("127.0.0.1", 0),
                "too-short",
                ":memory:",
                read_api_key=READ_API_KEY,
            )

    def test_read_api_key_must_be_64_characters_and_distinct(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 64"):
            RelayServer(
                ("127.0.0.1", 0),
                WRITE_API_KEY,
                ":memory:",
                read_api_key="too-short",
            )
        with self.assertRaisesRegex(ValueError, "must differ"):
            RelayServer(
                ("127.0.0.1", 0),
                WRITE_API_KEY,
                ":memory:",
                read_api_key=WRITE_API_KEY,
            )

    def test_api_keys_have_separate_read_and_write_permissions(self) -> None:
        read_with_write_status, _ = self.request(
            "GET", "/v1/messages", api_key=WRITE_API_KEY
        )
        write_with_read_status, _ = self.request(
            "POST",
            "/v1/messages",
            {"from": "10086", "content": "权限测试"},
            READ_API_KEY,
        )
        read_status, _ = self.request("GET", "/v1/messages", api_key=READ_API_KEY)

        self.assertEqual(read_with_write_status, 401)
        self.assertEqual(write_with_read_status, 401)
        self.assertEqual(read_status, 200)

    def test_incremental_message_cursor_is_ordered_and_resumable(self) -> None:
        inserted_ids = []
        for index in range(3):
            status, body = self.request(
                "POST",
                "/v1/messages",
                {"from": "10086", "content": f"增量消息 {index}"},
                WRITE_API_KEY,
            )
            self.assertEqual(status, 200)
            inserted_ids.append(body["id"])

        bootstrap_status, bootstrap = self.request(
            "GET", "/v1/messages?after_id=0&limit=10", api_key=READ_API_KEY
        )
        first_status, first = self.request(
            "GET",
            f"/v1/messages?after_id={inserted_ids[0]}&limit=1",
            api_key=READ_API_KEY,
        )
        second_status, second = self.request(
            "GET",
            f"/v1/messages?after_id={first['next_after_id']}&limit=10",
            api_key=READ_API_KEY,
        )
        empty_status, empty = self.request(
            "GET",
            f"/v1/messages?after_id={second['next_after_id']}&limit=10",
            api_key=READ_API_KEY,
        )

        self.assertEqual(bootstrap_status, 200)
        self.assertEqual(
            [message["id"] for message in bootstrap["messages"]], inserted_ids
        )
        self.assertEqual(bootstrap["next_after_id"], inserted_ids[-1])
        self.assertFalse(bootstrap["has_more"])
        self.assertEqual(first_status, 200)
        self.assertEqual(
            [message["id"] for message in first["messages"]], [inserted_ids[1]]
        )
        self.assertEqual(first["next_after_id"], inserted_ids[1])
        self.assertTrue(first["has_more"])
        self.assertEqual(second_status, 200)
        self.assertEqual(
            [message["id"] for message in second["messages"]], [inserted_ids[2]]
        )
        self.assertEqual(second["next_after_id"], inserted_ids[2])
        self.assertFalse(second["has_more"])
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty["messages"], [])
        self.assertEqual(empty["next_after_id"], inserted_ids[2])
        self.assertFalse(empty["has_more"])

    def test_incremental_cursor_rejects_invalid_combinations(self) -> None:
        both_status, both = self.request(
            "GET", "/v1/messages?before_id=10&after_id=5", api_key=READ_API_KEY
        )
        negative_status, negative = self.request(
            "GET", "/v1/messages?after_id=-1", api_key=READ_API_KEY
        )
        blank_status, blank = self.request(
            "GET", "/v1/messages?after_id=", api_key=READ_API_KEY
        )

        self.assertEqual(both_status, 400)
        self.assertEqual(both["error"], "invalid_query")
        self.assertEqual(negative_status, 400)
        self.assertEqual(negative["error"], "invalid_query")
        self.assertEqual(blank_status, 400)
        self.assertEqual(blank["error"], "invalid_query")


class MessageEnrichmentTests(unittest.TestCase):
    def test_email_verification_action_with_multiline_code(self) -> None:
        cases = [
            ("邮箱验证", "请在48小时内输入以下代码完成验证：\r\n \n a7C91d\n客服电话400-601-4321", "a7C91d"),
            ("邮箱验证", "请使用以下代码完成验证：\nAbCdEf", "AbCdEf"),
            ("邮箱验证", "输入以下代码完成验证：aB12cD", "aB12cD"),
            ("帐号登录提醒", "帐号成功登录。登录时间：2026-09-22 17:14:58\n客服电话400-601-4321", ""),
            ("邮箱验证", "输入以下代码完成验证：\n400-601-4321", ""),
            ("邮箱验证", "输入以下代码完成验证：\n2026-09-22", ""),
            ("邮箱验证", "输入以下代码完成验证：\n48小时", ""),
            ("订单通知", "订单编号123456", ""),
            ("邮箱验证", "输入以下代码完成验证：\n请联系客服\n123456", ""),
            ("邮箱验证", "输入以下代码完成验证：\nAb12Cd\n输入以下代码完成验证：\nEf34Gh", ""),
            ("邮箱验证", "Your OTP is a7C91d", "a7C91d"),
            ("邮箱验证", "OTP 123456\nOTP 654321", ""),
            ("邮箱验证", "OTP 123456\nOTP 123456", "123456"),
        ]
        for subject, body, expected in cases:
            with self.subTest(subject=subject, body=body):
                self.assertEqual(extract_email_verification_code(subject, body), expected)
        self.assertEqual(extract_verification_code("输入以下代码完成验证：\nAbCdEf"), "")

    def test_extracts_first_non_empty_bracket_tag(self) -> None:
        self.assertEqual(
            extract_message_tag("【  小红书  】验证码 123456【登录提醒】"),
            "小红书",
        )
        self.assertEqual(extract_message_tag("【】验证码 123456【快手】"), "快手")
        self.assertEqual(extract_message_tag("没有短信签名"), "")

    def test_identifies_supported_platform_urls_without_lookalike_hosts(self) -> None:
        self.assertEqual(
            identify_platform("https://store.weixin.qq.com/shop/order/list"),
            "微信小店",
        )
        self.assertEqual(
            identify_platform("https://store.weixin.qq.com.evil.example/shop/order/list"),
            "",
        )
        self.assertEqual(identify_platform("javascript:alert(1)"), "")

    def test_extracts_common_verification_code_formats(self) -> None:
        cases = {
            "验证码是123456，请勿泄露": "123456",
            "【丁香园】您的丁香园账号登录验证码 482701，请勿泄露。": "482701",
            "【快手科技】739205快手验证码，15分钟内有效，仅用于登录。": "739205",
            "【抖音商城】订单导出文件已加密，解压密码为618204，请妥善保管": "618204",
            "【抖音商城】订单导出文件已加密，解压密码为gVtFmd，请妥善保管": "gVtFmd",
            "【快手小店】提取码：11116f。你的账号正申请文件提取码，请核实导出原因，避免因不明导出行为导致信息泄露，非必要勿导出！": "11116f",
            "动态码：4827，10分钟内有效": "4827",
            "Your OTP is A7C91D": "A7C91D",
            "Your OTP is a7C91d": "a7C91d",
            "839204 是您的校验码": "839204",
            "security code = 771920": "771920",
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                self.assertEqual(extract_verification_code(content), expected)

    def test_does_not_treat_unrelated_numbers_as_verification_codes(self) -> None:
        self.assertEqual(extract_verification_code("订单 202608071234 已发货"), "")
        self.assertEqual(
            extract_verification_code("您的抖店账号于2026-08-10 12:10:00成功登录"),
            "",
        )
        self.assertEqual(extract_verification_code("解锁最高1000流量包"), "")
        self.assertEqual(extract_verification_code("您的登录密码已修改"), "")

    def test_parses_sim_slot_and_phone_number(self) -> None:
        self.assertEqual(parse_sim_info("SIM2_13800000000"), ("SIM2", "13800000000"))
        self.assertEqual(parse_sim_info("卡1 中国联通 13900000000"), ("SIM1", "13900000000"))
        self.assertEqual(parse_sim_info("SIM2_"), ("SIM2", ""))


class FeishuNotifierTests(unittest.TestCase):
    def test_notification_includes_tag_only_when_present(self) -> None:
        class RecorderClient:
            def __init__(self) -> None:
                self.payloads: list[dict] = []

            def request(self, _path: str, **kwargs) -> dict:
                self.payloads.append(kwargs["payload"])
                return {"code": 0}

        client = RecorderClient()
        notifier = FeishuNotifier("", "", "oc_test", client=client)
        base_message = {
            "id": 1,
            "verification_code": "482701",
            "sender": "10690000",
            "sim_phone": "13800000000",
            "source_received_at": "2026-08-12 10:00:00",
        }

        notifier.send({**base_message, "tag": "小红书"})
        notifier.send({**base_message, "id": 2, "tag": ""})

        self.assertEqual(client.payloads[0]["msg_type"], "interactive")
        self.assertEqual(client.payloads[0]["uuid"], "sms-relay-1")
        tagged_text = json.dumps(json.loads(client.payloads[0]["content"]), ensure_ascii=False)
        untagged_text = json.dumps(json.loads(client.payloads[1]["content"]), ensure_ascii=False)
        self.assertIn("平台：小红书", tagged_text)
        self.assertNotIn("平台：", untagged_text)

    def test_card_has_copyable_code_and_plain_text_email_metadata(self) -> None:
        card = build_verification_card({"id": 3, "message_type": "email", "verification_code": "a7C91d",
            "recipient": "account@example.test", "sender": "<at id=all>sender</at>",
            "subject": "[untrusted](https://example.test)", "content": "private body omitted",
            "source_received_at": "2026-09-22T10:00:00+08:00"})
        self.assertEqual(card["schema"], "2.0")
        elements = card["body"]["elements"]
        code_elements = [element for element in elements if element["tag"] == "markdown"]
        self.assertEqual(code_elements, [{"tag": "markdown", "content": "```\na7C91d\n```"}])
        texts = [element["text"]["content"] for element in elements if element["tag"] == "div"]
        self.assertIn("接收邮箱：account@example.test", texts)
        self.assertIn("邮件主题：[untrusted](https://example.test)", texts)
        self.assertTrue(all(element["text"]["tag"] == "plain_text" for element in elements if element["tag"] == "div"))
        self.assertNotIn("private body omitted", json.dumps(card))
        button = next(element for element in elements if element["tag"] == "button")
        url = button["behaviors"][0]["default_url"]
        self.assertTrue(url.endswith("/copy?message_id=3"))
        self.assertNotIn("a7C91d", url)
        self.assertNotIn("account@example.test", url)
        sms = build_verification_card({"id": 4, "verification_code": "AbCdEf", "sim_phone": "13800000000"})
        self.assertIn("接收号码：13800000000", json.dumps(sms, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
