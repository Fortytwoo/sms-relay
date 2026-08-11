from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from access_control import AccessConflictError, AccessControl


class AccessControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "relay.db")
        self.access = AccessControl(self.db_path, admin_open_ids={"ou_admin"})
        self.departments = [
            {"department_id": "0", "parent_department_id": "", "name": "企业"},
            {"department_id": "od_a", "parent_department_id": "0", "name": "甲部门"},
            {"department_id": "od_a1", "parent_department_id": "od_a", "name": "甲子部门"},
            {"department_id": "od_b", "parent_department_id": "0", "name": "乙部门"},
        ]
        self.users = [
            {"open_id": "ou_a", "name": "甲成员"},
            {"open_id": "ou_a1", "name": "甲子成员"},
            {"open_id": "ou_b", "name": "乙成员"},
        ]
        self.memberships = {
            ("ou_a", "od_a"),
            ("ou_a1", "od_a1"),
            ("ou_b", "od_b"),
        }
        self.access.complete_sync(self.departments, self.users, self.memberships)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_bootstrap_admin_is_always_allowed(self) -> None:
        user = self.access.resolve_user("ou_admin", fallback_name="管理员")
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "admin")
        self.assertIsNone(self.access.resolve_user("ou_a"))

    def test_department_grant_recursively_allows_descendants(self) -> None:
        result = self.access.replace_grants(
            department_ids=["od_a"],
            user_open_ids=[],
            expected_revision=0,
            actor_open_id="ou_admin",
        )
        self.assertEqual(result["effective_user_count"], 2)
        self.assertIsNotNone(self.access.resolve_user("ou_a"))
        self.assertIsNotNone(self.access.resolve_user("ou_a1"))
        self.assertIsNone(self.access.resolve_user("ou_b"))

    def test_direct_grant_and_optimistic_revision(self) -> None:
        self.access.replace_grants(
            department_ids=[],
            user_open_ids=["ou_b"],
            expected_revision=0,
            actor_open_id="ou_admin",
        )
        self.assertEqual(self.access.resolve_user("ou_b")["role"], "user")
        with self.assertRaises(AccessConflictError):
            self.access.replace_grants(
                department_ids=[],
                user_open_ids=[],
                expected_revision=0,
                actor_open_id="ou_admin",
            )

    def test_successful_sync_recomputes_dynamic_access(self) -> None:
        self.access.replace_grants(
            department_ids=["od_a"],
            user_open_ids=[],
            expected_revision=0,
            actor_open_id="ou_admin",
        )
        self.access.complete_sync(
            self.departments,
            [self.users[0], self.users[2]],
            {("ou_a", "od_a"), ("ou_b", "od_b")},
        )
        self.assertIsNone(self.access.resolve_user("ou_a1"))
        self.assertIsNotNone(self.access.resolve_user("ou_a"))

    def test_failed_sync_preserves_previous_snapshot(self) -> None:
        before = self.access.directory_snapshot()
        self.assertTrue(self.access.begin_sync())
        self.access.fail_sync("temporary upstream failure")
        after = self.access.directory_snapshot()
        self.assertEqual(after["sync"]["status"], "failed")
        self.assertEqual(after["departments"], before["departments"])

    def test_effective_user_count_excludes_bootstrap_admin(self) -> None:
        self.access.complete_sync(
            self.departments,
            [*self.users, {"open_id": "ou_admin", "name": "管理员"}],
            {*self.memberships, ("ou_admin", "od_a")},
        )
        result = self.access.replace_grants(
            department_ids=["od_a"],
            user_open_ids=[],
            expected_revision=0,
            actor_open_id="ou_admin",
        )
        self.assertEqual(result["effective_user_count"], 2)


if __name__ == "__main__":
    unittest.main()
