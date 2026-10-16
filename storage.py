from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


def _positive_integer(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


# These defaults can be changed in Railway Variables or in a local .env file.
REFERRAL_REWARD = _positive_integer("REFERRAL_REWARD", 5)
MIN_WITHDRAWAL = _positive_integer("MIN_WITHDRAWAL", 100)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetchone(
    db: aiosqlite.Connection, query: str, parameters: tuple[Any, ...] = ()
) -> aiosqlite.Row | None:
    async with db.execute(query, parameters) as cursor:
        return await cursor.fetchone()


async def _fetchall(
    db: aiosqlite.Connection, query: str, parameters: tuple[Any, ...] = ()
) -> list[aiosqlite.Row]:
    async with db.execute(query, parameters) as cursor:
        return await cursor.fetchall()


class Store:
    """Small async SQLite store for the Telegram bot."""

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path.strip()
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store.initialize() must be called before database access")
        return self._db

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        db = self._connection()
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            await db.rollback()
            raise
        else:
            await db.commit()

    async def initialize(self) -> None:
        async with self._lock:
            if self._db is not None:
                return

            if not self.database_path:
                raise RuntimeError("DATABASE_PATH must not be empty")
            if self.database_path != ":memory:":
                Path(self.database_path).expanduser().parent.mkdir(
                    parents=True, exist_ok=True
                )

            db = await aiosqlite.connect(self.database_path)
            db.row_factory = aiosqlite.Row
            try:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute("PRAGMA journal_mode = WAL")
                await db.execute("PRAGMA busy_timeout = 5000")
                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        first_name TEXT NOT NULL,
                        referrer_id INTEGER,
                        balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
                        referral_count INTEGER NOT NULL DEFAULT 0 CHECK (referral_count >= 0),
                        is_verified INTEGER NOT NULL DEFAULT 0 CHECK (is_verified IN (0, 1)),
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (referrer_id) REFERENCES users(user_id)
                            ON DELETE SET NULL
                    );

                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id TEXT NOT NULL,
                        channel_url TEXT NOT NULL,
                        reward INTEGER NOT NULL CHECK (reward > 0),
                        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS task_completions (
                        user_id INTEGER NOT NULL,
                        task_id INTEGER NOT NULL,
                        completed_at TEXT NOT NULL,
                        PRIMARY KEY (user_id, task_id),
                        FOREIGN KEY (user_id) REFERENCES users(user_id)
                            ON DELETE CASCADE,
                        FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                            ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS support_messages (
                        message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        username TEXT,
                        message TEXT NOT NULL,
                        reply TEXT,
                        status TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'replied')),
                        created_at TEXT NOT NULL,
                        replied_at TEXT,
                        FOREIGN KEY (user_id) REFERENCES users(user_id)
                            ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS withdrawals (
                        withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        amount INTEGER NOT NULL CHECK (amount > 0),
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'paid', 'rejected')),
                        created_at TEXT NOT NULL,
                        resolved_at TEXT,
                        FOREIGN KEY (user_id) REFERENCES users(user_id)
                            ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_tasks_active
                        ON tasks(is_active, task_id);
                    CREATE INDEX IF NOT EXISTS idx_support_status
                        ON support_messages(status, message_id);
                    CREATE INDEX IF NOT EXISTS idx_withdrawals_status
                        ON withdrawals(status, withdrawal_id);
                    """
                )
                await db.commit()
            except BaseException:
                await db.close()
                raise
            self._db = db

    async def close(self) -> None:
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    async def register_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str,
        referrer_id: int | None,
    ) -> None:
        async with self._lock:
            async with self._transaction() as db:
                valid_referrer_id = None
                if referrer_id is not None and referrer_id != user_id:
                    referrer = await _fetchone(
                        db, "SELECT user_id FROM users WHERE user_id = ?", (referrer_id,)
                    )
                    if referrer is not None:
                        valid_referrer_id = referrer_id

                await db.execute(
                    """
                    INSERT INTO users (
                        user_id, username, first_name, referrer_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username = excluded.username,
                        first_name = excluded.first_name
                    """,
                    (
                        user_id,
                        username,
                        first_name or "Пользователь",
                        valid_referrer_id,
                        _now(),
                    ),
                )

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        async with self._lock:
            row = await _fetchone(
                self._connection(), "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            return dict(row) if row is not None else None

    async def verify_user(self, user_id: int) -> tuple[bool, int | None, bool]:
        async with self._lock:
            async with self._transaction() as db:
                user = await _fetchone(
                    db,
                    "SELECT referrer_id, is_verified FROM users WHERE user_id = ?",
                    (user_id,),
                )
                if user is None or user["is_verified"]:
                    return False, None, False

                await db.execute(
                    "UPDATE users SET is_verified = 1 WHERE user_id = ?", (user_id,)
                )
                awarded_referrer: int | None = None
                referrer_id = user["referrer_id"]
                if referrer_id is not None and referrer_id != user_id:
                    cursor = await db.execute(
                        """
                        UPDATE users
                        SET balance = balance + ?, referral_count = referral_count + 1
                        WHERE user_id = ?
                        """,
                        (REFERRAL_REWARD, referrer_id),
                    )
                    if cursor.rowcount:
                        awarded_referrer = int(referrer_id)

                return True, awarded_referrer, True

    async def list_available_tasks(self, user_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            rows = await _fetchall(
                self._connection(),
                """
                SELECT t.task_id, t.channel_id, t.channel_url, t.reward
                FROM tasks AS t
                WHERE t.is_active = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM task_completions AS c
                      WHERE c.task_id = t.task_id AND c.user_id = ?
                  )
                ORDER BY t.task_id DESC
                """,
                (user_id,),
            )
            return [dict(row) for row in rows]

    async def get_task(self, task_id: int) -> dict[str, Any] | None:
        async with self._lock:
            row = await _fetchone(
                self._connection(),
                """
                SELECT task_id, channel_id, channel_url, reward
                FROM tasks
                WHERE task_id = ? AND is_active = 1
                """,
                (task_id,),
            )
            return dict(row) if row is not None else None

    async def complete_task(self, user_id: int, task_id: int) -> tuple[str, int]:
        async with self._lock:
            async with self._transaction() as db:
                task = await _fetchone(
                    db,
                    "SELECT reward FROM tasks WHERE task_id = ? AND is_active = 1",
                    (task_id,),
                )
                if task is None:
                    return "unavailable", 0

                cursor = await db.execute(
                    """
                    INSERT OR IGNORE INTO task_completions (
                        user_id, task_id, completed_at
                    )
                    SELECT user_id, ?, ? FROM users WHERE user_id = ?
                    """,
                    (task_id, _now(), user_id),
                )
                if cursor.rowcount == 0:
                    existing_user = await _fetchone(
                        db, "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
                    )
                    if existing_user is None:
                        return "unavailable", 0
                    existing_completion = await _fetchone(
                        db,
                        """
                        SELECT 1 FROM task_completions
                        WHERE user_id = ? AND task_id = ?
                        """,
                        (user_id, task_id),
                    )
                    if existing_completion is not None:
                        return "already_completed", 0
                    return "unavailable", 0

                reward = int(task["reward"])
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (reward, user_id),
                )
                return "completed", reward

    async def create_support_message(
        self, user_id: int, username: str | None, message: str
    ) -> int:
        async with self._lock:
            async with self._transaction() as db:
                cursor = await db.execute(
                    """
                    INSERT INTO support_messages (user_id, username, message, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, username, message, _now()),
                )
                return int(cursor.lastrowid)

    async def list_open_support(self) -> list[dict[str, Any]]:
        async with self._lock:
            rows = await _fetchall(
                self._connection(),
                """
                SELECT message_id, user_id, username, message, status, created_at
                FROM support_messages
                WHERE status = 'open'
                ORDER BY message_id DESC
                LIMIT 50
                """,
            )
            return [dict(row) for row in rows]

    async def get_support_message(self, message_id: int) -> dict[str, Any] | None:
        async with self._lock:
            row = await _fetchone(
                self._connection(),
                "SELECT * FROM support_messages WHERE message_id = ?",
                (message_id,),
            )
            return dict(row) if row is not None else None

    async def mark_support_replied(self, message_id: int, reply: str) -> bool:
        async with self._lock:
            async with self._transaction() as db:
                cursor = await db.execute(
                    """
                    UPDATE support_messages
                    SET reply = ?, status = 'replied', replied_at = ?
                    WHERE message_id = ? AND status = 'open'
                    """,
                    (reply, _now(), message_id),
                )
                return cursor.rowcount > 0

    async def request_withdrawal(
        self, user_id: int, amount: int
    ) -> tuple[str, int | None]:
        if amount < 1:
            return "invalid_amount", None

        async with self._lock:
            async with self._transaction() as db:
                user = await _fetchone(
                    db, "SELECT balance FROM users WHERE user_id = ?", (user_id,)
                )
                if user is None or int(user["balance"]) < amount:
                    return "insufficient_balance", None

                await db.execute(
                    "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                    (amount, user_id),
                )
                cursor = await db.execute(
                    """
                    INSERT INTO withdrawals (user_id, amount, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, amount, _now()),
                )
                return "created", int(cursor.lastrowid)

    async def list_pending_withdrawals(self) -> list[dict[str, Any]]:
        async with self._lock:
            rows = await _fetchall(
                self._connection(),
                """
                SELECT w.withdrawal_id, w.user_id, w.amount, w.created_at,
                       u.username, u.first_name
                FROM withdrawals AS w
                JOIN users AS u ON u.user_id = w.user_id
                WHERE w.status = 'pending'
                ORDER BY w.withdrawal_id ASC
                """,
            )
            return [dict(row) for row in rows]

    async def resolve_withdrawal(
        self, withdrawal_id: int, approved: bool
    ) -> dict[str, Any] | None:
        async with self._lock:
            async with self._transaction() as db:
                request = await _fetchone(
                    db,
                    "SELECT * FROM withdrawals WHERE withdrawal_id = ? AND status = 'pending'",
                    (withdrawal_id,),
                )
                if request is None:
                    return None

                result = dict(request)
                status = "paid" if approved else "rejected"
                await db.execute(
                    """
                    UPDATE withdrawals
                    SET status = ?, resolved_at = ?
                    WHERE withdrawal_id = ? AND status = 'pending'
                    """,
                    (status, _now(), withdrawal_id),
                )
                if not approved:
                    await db.execute(
                        "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                        (request["amount"], request["user_id"]),
                    )
                result["status"] = status
                return result

    async def create_task(
        self, channel_id: str, channel_url: str, reward: int
    ) -> int:
        async with self._lock:
            async with self._transaction() as db:
                cursor = await db.execute(
                    """
                    INSERT INTO tasks (channel_id, channel_url, reward, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (channel_id, channel_url, reward, _now()),
                )
                return int(cursor.lastrowid)
