from __future__ import annotations

import unittest
from typing import Any

from app import FeishuClient


class FakeFeishuClient(FeishuClient):
    def __init__(self) -> None:
        super().__init__("cli_test", "secret", request_interval=0)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, path: str, **kwargs: Any) -> dict[str, Any]:
        params = dict(kwargs.get("params") or {})
        self.calls.append((path, params))
        if path.endswith("/departments/0/children"):
            return {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "open_department_id": "od_product",
                            "parent_department_id": "0",
                            "name": "产品部",
                            "member_count": 1,
                            "order": 2,
                        }
                    ],
                    "has_more": False,
                },
            }
        if path.endswith("/users/find_by_department") and params["department_id"] == "0":
            return {
                "code": 0,
                "data": {
                    "items": [{"open_id": "ou_admin", "name": "管理员"}],
                    "has_more": False,
                },
            }
        return {
            "code": 0,
            "data": {
                "items": [
                    {
                        "open_id": "ou_member",
                        "union_id": "on_member",
                        "name": "成员",
                        "department_ids": ["od_product"],
                        "status": {"is_activated": True},
                    }
                ],
                "has_more": False,
            },
        }


class FeishuDirectoryTests(unittest.TestCase):
    def test_fetch_directory_uses_current_department_and_user_endpoints(self) -> None:
        client = FakeFeishuClient()
        departments, users, memberships = client.fetch_directory()

        self.assertEqual([item["department_id"] for item in departments], ["0", "od_product"])
        self.assertEqual({item["open_id"] for item in users}, {"ou_admin", "ou_member"})
        self.assertIn(("ou_admin", "0"), memberships)
        self.assertIn(("ou_member", "od_product"), memberships)
        self.assertEqual(
            [path for path, _ in client.calls],
            [
                "/open-apis/contact/v3/departments/0/children",
                "/open-apis/contact/v3/users/find_by_department",
                "/open-apis/contact/v3/users/find_by_department",
            ],
        )
        for _, params in client.calls:
            self.assertEqual(params["user_id_type"], "open_id")


if __name__ == "__main__":
    unittest.main()
