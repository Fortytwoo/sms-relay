from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import RelayServer, extract_verification_code, parse_sim_info


API_KEY = "a" * 64


class RelayApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = str(Path(self.temp_dir.name) / "relay.db")
        self.server = RelayServer(("127.0.0.1", 0), API_KEY, db_path)
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
    ) -> tuple[int, dict]:
        headers: dict[str, str] = {}
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

    def test_health_is_public(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {"ok": True, "status": "healthy"},
        )

    def test_web_ui_and_assets_are_public_with_security_headers(self) -> None:
        status, page, headers = self.request_text("/")
        css_status, css, _ = self.request_text("/assets/app.css")
        js_status, js, _ = self.request_text("/assets/app.js")

        self.assertEqual(status, 200)
        self.assertIn("短信中转", page)
        self.assertIn('id="feishu-login"', page)
        self.assertIn("飞书账号登录", page)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertEqual(css_status, 200)
        self.assertIn("--color-primary", css)
        self.assertEqual(js_status, 200)
        self.assertNotIn("sessionStorage", js)
        self.assertNotIn("localStorage", js)
        self.assertNotIn("innerHTML", js)

    def test_messages_require_api_key(self) -> None:
        status, body = self.request("GET", "/v1/messages")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

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
        first_status, first = self.request("POST", "/v1/messages", payload, API_KEY)
        second_status, second = self.request("POST", "/v1/messages", payload, API_KEY)
        list_status, listed = self.request("GET", "/v1/messages?limit=10", api_key=API_KEY)

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

    def test_feishu_session_can_list_messages_without_api_key(self) -> None:
        payload = {
            "type": "sms",
            "from": "10690000",
            "content": "动态码：5729",
            "sim_info": "SIM1_13900000000",
        }
        self.request("POST", "/v1/messages", payload, API_KEY)
        cookie = self.server.make_session_cookie("ou_test", "测试用户")

        session_status, session = self.request("GET", "/auth/session", cookie=cookie)
        list_status, listed = self.request("GET", "/v1/messages", cookie=cookie)

        self.assertEqual(session_status, 200)
        self.assertEqual(session["user"]["name"], "测试用户")
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["messages"][0]["verification_code"], "5729")

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
            API_KEY,
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
            headers={"X-API-Key": API_KEY, "Content-Type": "text/plain"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 415)

    def test_api_key_must_be_64_characters(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 64"):
            RelayServer(("127.0.0.1", 0), "too-short", ":memory:")


class MessageEnrichmentTests(unittest.TestCase):
    def test_extracts_common_verification_code_formats(self) -> None:
        cases = {
            "验证码是123456，请勿泄露": "123456",
            "动态码：4827，10分钟内有效": "4827",
            "Your OTP is A7C91D": "A7C91D",
            "839204 是您的校验码": "839204",
            "security code = 771920": "771920",
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                self.assertEqual(extract_verification_code(content), expected)

    def test_does_not_treat_unrelated_numbers_as_verification_codes(self) -> None:
        self.assertEqual(extract_verification_code("订单 202608071234 已发货"), "")

    def test_parses_sim_slot_and_phone_number(self) -> None:
        self.assertEqual(parse_sim_info("SIM2_13800000000"), ("SIM2", "13800000000"))
        self.assertEqual(parse_sim_info("卡1 中国联通 13900000000"), ("SIM1", "13900000000"))
        self.assertEqual(parse_sim_info("SIM2_"), ("SIM2", ""))


if __name__ == "__main__":
    unittest.main()
