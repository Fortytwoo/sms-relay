from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app import (
    RelayServer,
    extract_message_tag,
    extract_verification_code,
    identify_platform,
    parse_sim_info,
)


WRITE_API_KEY = "a" * 64
READ_API_KEY = "b" * 64


class RelayApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = str(Path(self.temp_dir.name) / "relay.db")
        self.server = RelayServer(
            ("127.0.0.1", 0),
            WRITE_API_KEY,
            db_path,
            read_api_key=READ_API_KEY,
            allowed_open_ids={"ou_test"},
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
        self.assertIn('id="detail-tag-row"', page)
        self.assertIn('id="detail-phone"', page)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertEqual(css_status, 200)
        self.assertIn("--color-primary", css)
        self.assertEqual(js_status, 200)
        self.assertNotIn("sessionStorage", js)
        self.assertNotIn("localStorage", js)
        self.assertNotIn("innerHTML", js)
        self.assertIn("message.tag", js)

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

    def test_feishu_session_can_list_messages_without_api_key(self) -> None:
        payload = {
            "type": "sms",
            "from": "10690000",
            "content": "动态码：5729",
            "sim_info": "SIM1_13900000000",
        }
        self.request("POST", "/v1/messages", payload, WRITE_API_KEY)
        cookie = self.server.make_session_cookie("ou_test", "测试用户")

        session_status, session = self.request("GET", "/auth/session", cookie=cookie)
        list_status, listed = self.request("GET", "/v1/messages", cookie=cookie)

        self.assertEqual(session_status, 200)
        self.assertEqual(session["user"]["name"], "测试用户")
        self.assertEqual(session["user"]["role"], "admin")
        self.assertTrue(session["csrf_token"])
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["messages"][0]["verification_code"], "5729")

    def test_admin_access_api_requires_role_csrf_and_revision(self) -> None:
        self.server.access.complete_sync(
            [
                {"department_id": "0", "parent_department_id": "", "name": "企业"},
                {"department_id": "od_team", "parent_department_id": "0", "name": "测试组"},
            ],
            [
                {"open_id": "ou_test", "name": "管理员"},
                {"open_id": "ou_member", "name": "普通用户"},
            ],
            {("ou_test", "od_team"), ("ou_member", "od_team")},
        )
        admin_cookie = self.server.make_session_cookie("ou_test", "管理员")
        session_status, session = self.request("GET", "/auth/session", cookie=admin_cookie)
        self.assertEqual(session_status, 200)
        csrf = session["csrf_token"]

        missing_csrf_status, _ = self.request(
            "PUT",
            "/v1/admin/access",
            {"revision": 0, "department_ids": [], "user_open_ids": ["ou_member"]},
            cookie=admin_cookie,
        )
        save_status, saved = self.request(
            "PUT",
            "/v1/admin/access",
            {"revision": 0, "department_ids": [], "user_open_ids": ["ou_member"]},
            cookie=admin_cookie,
            extra_headers={"X-CSRF-Token": csrf},
        )
        conflict_status, conflict = self.request(
            "PUT",
            "/v1/admin/access",
            {"revision": 0, "department_ids": [], "user_open_ids": []},
            cookie=admin_cookie,
            extra_headers={"X-CSRF-Token": csrf},
        )
        member_cookie = self.server.make_session_cookie("ou_member", "普通用户")
        forbidden_status, forbidden = self.request(
            "GET", "/v1/admin/access", cookie=member_cookie
        )

        self.assertEqual(missing_csrf_status, 403)
        self.assertEqual(save_status, 200)
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"], "access_revision_conflict")
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(forbidden["error"], "forbidden")

    def test_revoked_user_session_stops_working_immediately(self) -> None:
        self.server.access.complete_sync(
            [{"department_id": "0", "parent_department_id": "", "name": "企业"}],
            [{"open_id": "ou_member", "name": "普通用户"}],
            {("ou_member", "0")},
        )
        self.server.access.replace_grants(
            department_ids=[],
            user_open_ids=["ou_member"],
            expected_revision=0,
            actor_open_id="ou_test",
        )
        cookie = self.server.make_session_cookie("ou_member", "普通用户")
        allowed_status, _ = self.request("GET", "/v1/messages", cookie=cookie)
        self.server.access.replace_grants(
            department_ids=[],
            user_open_ids=[],
            expected_revision=1,
            actor_open_id="ou_test",
        )
        revoked_status, revoked = self.request("GET", "/v1/messages", cookie=cookie)

        self.assertEqual(allowed_status, 200)
        self.assertEqual(revoked_status, 401)
        self.assertEqual(revoked["error"], "unauthorized")

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
