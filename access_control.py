from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def open_db(db_path: str):
    connection = connect_db(db_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class AccessConflictError(RuntimeError):
    pass


class InvalidAccessSubjectError(ValueError):
    pass


class AccessControl:
    def __init__(
        self,
        db_path: str,
        *,
        admin_open_ids: set[str] | None = None,
        admin_union_ids: set[str] | None = None,
    ) -> None:
        self.db_path = db_path
        self.admin_open_ids = set(admin_open_ids or set())
        self.admin_union_ids = set(admin_union_ids or set())
        self.init_db()

    def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with open_db(self.db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS directory_departments (
                    department_id TEXT PRIMARY KEY,
                    parent_department_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    member_count INTEGER NOT NULL DEFAULT 0,
                    synced_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS directory_users (
                    open_id TEXT PRIMARY KEY,
                    union_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    avatar_url TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    synced_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS directory_user_departments (
                    open_id TEXT NOT NULL,
                    department_id TEXT NOT NULL,
                    PRIMARY KEY (open_id, department_id),
                    FOREIGN KEY (open_id) REFERENCES directory_users(open_id) ON DELETE CASCADE,
                    FOREIGN KEY (department_id) REFERENCES directory_departments(department_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS access_grants (
                    subject_type TEXT NOT NULL CHECK (subject_type IN ('department', 'user')),
                    subject_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (subject_type, subject_id)
                );

                CREATE TABLE IF NOT EXISTS access_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    access_revision INTEGER NOT NULL DEFAULT 0,
                    directory_revision INTEGER NOT NULL DEFAULT 0,
                    sync_status TEXT NOT NULL DEFAULT 'idle',
                    sync_started_at TEXT NOT NULL DEFAULT '',
                    sync_finished_at TEXT NOT NULL DEFAULT '',
                    sync_error TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS login_identities (
                    open_id TEXT PRIMARY KEY,
                    union_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    last_login_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS access_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_open_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_directory_departments_parent
                    ON directory_departments(parent_department_id, sort_order, name);
                CREATE INDEX IF NOT EXISTS idx_directory_users_name
                    ON directory_users(name);
                CREATE INDEX IF NOT EXISTS idx_directory_user_departments_department
                    ON directory_user_departments(department_id, open_id);
                CREATE INDEX IF NOT EXISTS idx_access_audit_created_at
                    ON access_audit(created_at DESC);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO access_meta(singleton) VALUES (1)"
            )
            connection.execute(
                """
                UPDATE access_meta
                SET sync_status = 'failed',
                    sync_finished_at = ?,
                    sync_error = '同步进程在服务重启前未完成'
                WHERE singleton = 1 AND sync_status = 'running'
                """,
                (utc_now(),),
            )

    def record_login(self, open_id: str, union_id: str, name: str) -> None:
        if not open_id:
            return
        with open_db(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO login_identities(open_id, union_id, name, last_login_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(open_id) DO UPDATE SET
                    union_id = excluded.union_id,
                    name = excluded.name,
                    last_login_at = excluded.last_login_at
                """,
                (open_id, union_id, name, utc_now()),
            )

    def _is_admin(self, open_id: str, union_id: str = "") -> bool:
        return open_id in self.admin_open_ids or bool(
            union_id and union_id in self.admin_union_ids
        )

    def resolve_user(
        self, open_id: str, *, union_id: str = "", fallback_name: str = ""
    ) -> dict[str, str] | None:
        if not open_id:
            return None
        if self._is_admin(open_id, union_id):
            return {
                "open_id": open_id,
                "union_id": union_id,
                "name": self._display_name(open_id, fallback_name),
                "role": "admin",
            }

        with open_db(self.db_path) as connection:
            row = connection.execute(
                """
                WITH RECURSIVE granted_departments(department_id) AS (
                    SELECT subject_id FROM access_grants WHERE subject_type = 'department'
                    UNION
                    SELECT d.department_id
                    FROM directory_departments d
                    JOIN granted_departments g
                      ON d.parent_department_id = g.department_id
                )
                SELECT u.open_id, u.union_id, u.name
                FROM directory_users u
                WHERE u.open_id = ? AND u.active = 1 AND (
                    EXISTS (
                        SELECT 1 FROM access_grants g
                        WHERE g.subject_type = 'user' AND g.subject_id = u.open_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM directory_user_departments ud
                        WHERE ud.open_id = u.open_id
                          AND ud.department_id IN (SELECT department_id FROM granted_departments)
                    )
                )
                """,
                (open_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "open_id": str(row["open_id"]),
            "union_id": str(row["union_id"] or union_id),
            "name": str(row["name"] or fallback_name or "飞书用户"),
            "role": "user",
        }

    def _display_name(self, open_id: str, fallback: str) -> str:
        with open_db(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT name FROM directory_users WHERE open_id = ?
                UNION ALL
                SELECT name FROM login_identities WHERE open_id = ?
                LIMIT 1
                """,
                (open_id, open_id),
            ).fetchone()
        return str(row[0]) if row and row[0] else fallback or "飞书用户"

    def begin_sync(self) -> bool:
        with open_db(self.db_path) as connection:
            row = connection.execute(
                "SELECT sync_status FROM access_meta WHERE singleton = 1"
            ).fetchone()
            if row and row[0] == "running":
                return False
            connection.execute(
                """
                UPDATE access_meta
                SET sync_status = 'running', sync_started_at = ?,
                    sync_finished_at = '', sync_error = ''
                WHERE singleton = 1
                """,
                (utc_now(),),
            )
        return True

    def fail_sync(self, error: str) -> None:
        with open_db(self.db_path) as connection:
            connection.execute(
                """
                UPDATE access_meta
                SET sync_status = 'failed', sync_finished_at = ?, sync_error = ?
                WHERE singleton = 1
                """,
                (utc_now(), error[:512]),
            )

    def complete_sync(
        self,
        departments: list[dict[str, Any]],
        users: list[dict[str, Any]],
        memberships: set[tuple[str, str]],
    ) -> None:
        synced_at = utc_now()
        department_ids = {str(item["department_id"]) for item in departments}
        user_ids = {str(item["open_id"]) for item in users}
        valid_memberships = [
            (open_id, department_id)
            for open_id, department_id in memberships
            if open_id in user_ids and department_id in department_ids
        ]
        with open_db(self.db_path) as connection:
            connection.execute("DELETE FROM directory_user_departments")
            connection.execute("DELETE FROM directory_users")
            connection.execute("DELETE FROM directory_departments")
            connection.executemany(
                """
                INSERT INTO directory_departments(
                    department_id, parent_department_id, name, sort_order,
                    member_count, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(item["department_id"]),
                        str(item.get("parent_department_id", "")),
                        str(item.get("name") or "未命名部门")[:256],
                        int(item.get("order") or 0),
                        int(item.get("member_count") or 0),
                        synced_at,
                    )
                    for item in departments
                ],
            )
            connection.executemany(
                """
                INSERT INTO directory_users(
                    open_id, union_id, name, avatar_url, active, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(item["open_id"]),
                        str(item.get("union_id") or ""),
                        str(item.get("name") or "飞书用户")[:256],
                        str(item.get("avatar_url") or "")[:1024],
                        1 if item.get("active", True) else 0,
                        synced_at,
                    )
                    for item in users
                ],
            )
            connection.executemany(
                """
                INSERT INTO directory_user_departments(open_id, department_id)
                VALUES (?, ?)
                """,
                valid_memberships,
            )
            connection.execute(
                """
                UPDATE access_meta
                SET directory_revision = directory_revision + 1,
                    sync_status = 'success', sync_finished_at = ?, sync_error = ''
                WHERE singleton = 1
                """,
                (synced_at,),
            )

    def directory_snapshot(self) -> dict[str, Any]:
        with open_db(self.db_path) as connection:
            meta = connection.execute(
                "SELECT * FROM access_meta WHERE singleton = 1"
            ).fetchone()
            departments = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT department_id, parent_department_id, name, sort_order, member_count
                    FROM directory_departments
                    ORDER BY parent_department_id, sort_order, name
                    """
                )
            ]
        return {
            "departments": departments,
            "directory_revision": int(meta["directory_revision"]),
            "sync": {
                "status": str(meta["sync_status"]),
                "started_at": str(meta["sync_started_at"]),
                "finished_at": str(meta["sync_finished_at"]),
                "error": str(meta["sync_error"]),
            },
        }

    def access_summary(self) -> dict[str, Any]:
        with open_db(self.db_path) as connection:
            meta = connection.execute(
                "SELECT access_revision FROM access_meta WHERE singleton = 1"
            ).fetchone()
            department_ids = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT subject_id FROM access_grants
                    WHERE subject_type = 'department' ORDER BY subject_id
                    """
                )
            ]
            user_open_ids = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT subject_id FROM access_grants
                    WHERE subject_type = 'user' ORDER BY subject_id
                    """
                )
            ]
            effective_rows = connection.execute(
                """
                WITH RECURSIVE granted_departments(department_id) AS (
                    SELECT subject_id FROM access_grants WHERE subject_type = 'department'
                    UNION
                    SELECT d.department_id FROM directory_departments d
                    JOIN granted_departments g ON d.parent_department_id = g.department_id
                ), authorized(open_id) AS (
                    SELECT u.open_id FROM directory_users u
                    JOIN access_grants g ON g.subject_type = 'user' AND g.subject_id = u.open_id
                    WHERE u.active = 1
                    UNION
                    SELECT u.open_id FROM directory_users u
                    JOIN directory_user_departments ud ON ud.open_id = u.open_id
                    WHERE u.active = 1
                      AND ud.department_id IN (SELECT department_id FROM granted_departments)
                )
                SELECT u.open_id, u.union_id
                FROM directory_users u
                JOIN authorized a ON a.open_id = u.open_id
                """
            ).fetchall()
            effective_user_count = sum(
                1
                for row in effective_rows
                if not self._is_admin(str(row["open_id"]), str(row["union_id"] or ""))
            )
        return {
            "revision": int(meta[0]),
            "department_ids": department_ids,
            "user_open_ids": user_open_ids,
            "effective_user_count": effective_user_count,
            "admin_count": len(self.admin_open_ids) + len(self.admin_union_ids),
        }

    def _granted_department_ids(self, connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                """
                WITH RECURSIVE granted_departments(department_id) AS (
                    SELECT subject_id FROM access_grants WHERE subject_type = 'department'
                    UNION
                    SELECT d.department_id FROM directory_departments d
                    JOIN granted_departments g ON d.parent_department_id = g.department_id
                )
                SELECT department_id FROM granted_departments
                """
            )
        }

    def list_users(
        self,
        *,
        department_id: str = "",
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        query = query.strip()
        where = ["u.active = 1"]
        parameters: list[Any] = []
        if department_id:
            where.append(
                "EXISTS (SELECT 1 FROM directory_user_departments x "
                "WHERE x.open_id = u.open_id AND x.department_id = ?)"
            )
            parameters.append(department_id)
        if query:
            where.append("u.name LIKE ? ESCAPE '\\'")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"%{escaped}%")
        where_sql = " AND ".join(where)
        with open_db(self.db_path) as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM directory_users u WHERE {where_sql}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT u.open_id, u.union_id, u.name, u.avatar_url
                FROM directory_users u
                WHERE {where_sql}
                ORDER BY u.name, u.open_id
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()
            direct_grants = {
                str(row[0])
                for row in connection.execute(
                    "SELECT subject_id FROM access_grants WHERE subject_type = 'user'"
                )
            }
            granted_departments = self._granted_department_ids(connection)
            users: list[dict[str, Any]] = []
            for row in rows:
                open_id = str(row["open_id"])
                department_ids = [
                    str(item[0])
                    for item in connection.execute(
                        """
                        SELECT department_id FROM directory_user_departments
                        WHERE open_id = ? ORDER BY department_id
                        """,
                        (open_id,),
                    )
                ]
                union_id = str(row["union_id"] or "")
                users.append(
                    {
                        "open_id": open_id,
                        "union_id": union_id,
                        "name": str(row["name"]),
                        "avatar_url": str(row["avatar_url"]),
                        "department_ids": department_ids,
                        "direct_granted": open_id in direct_grants,
                        "department_granted": any(
                            item in granted_departments for item in department_ids
                        ),
                        "is_admin": self._is_admin(open_id, union_id),
                    }
                )
        return {
            "users": users,
            "total": total,
            "offset": offset,
            "has_more": offset + len(users) < total,
        }

    def replace_grants(
        self,
        *,
        department_ids: list[str],
        user_open_ids: list[str],
        expected_revision: int,
        actor_open_id: str,
    ) -> dict[str, Any]:
        department_ids = sorted({str(value).strip() for value in department_ids if str(value).strip()})
        user_open_ids = sorted({str(value).strip() for value in user_open_ids if str(value).strip()})
        if len(department_ids) > 5000 or len(user_open_ids) > 5000:
            raise InvalidAccessSubjectError("too_many_access_subjects")

        now = utc_now()
        with open_db(self.db_path) as connection:
            revision = int(
                connection.execute(
                    "SELECT access_revision FROM access_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            if revision != expected_revision:
                raise AccessConflictError("access_revision_conflict")
            known_departments = {
                str(row[0])
                for row in connection.execute("SELECT department_id FROM directory_departments")
            }
            known_users = {
                str(row[0]) for row in connection.execute("SELECT open_id FROM directory_users")
            }
            if not set(department_ids).issubset(known_departments):
                raise InvalidAccessSubjectError("unknown_department")
            if not set(user_open_ids).issubset(known_users):
                raise InvalidAccessSubjectError("unknown_user")
            user_open_ids = [
                value for value in user_open_ids if value not in self.admin_open_ids
            ]

            connection.execute("DELETE FROM access_grants")
            connection.executemany(
                """
                INSERT INTO access_grants(subject_type, subject_id, created_by, created_at)
                VALUES ('department', ?, ?, ?)
                """,
                [(value, actor_open_id, now) for value in department_ids],
            )
            connection.executemany(
                """
                INSERT INTO access_grants(subject_type, subject_id, created_by, created_at)
                VALUES ('user', ?, ?, ?)
                """,
                [(value, actor_open_id, now) for value in user_open_ids],
            )
            new_revision = revision + 1
            connection.execute(
                "UPDATE access_meta SET access_revision = ? WHERE singleton = 1",
                (new_revision,),
            )
            connection.execute(
                """
                INSERT INTO access_audit(actor_open_id, action, detail, created_at)
                VALUES (?, 'replace_access_grants', ?, ?)
                """,
                (
                    actor_open_id,
                    json.dumps(
                        {
                            "revision": new_revision,
                            "department_count": len(department_ids),
                            "user_count": len(user_open_ids),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
        return self.access_summary()
