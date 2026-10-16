from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage import MIN_WITHDRAWAL, REFERRAL_REWARD, Store


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.store = Store(str(database_path))
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp_dir.cleanup()

    async def test_referral_is_awarded_once_after_verification(self) -> None:
        await self.store.register_user(100, "referrer", "Referrer", None)
        await self.store.register_user(200, "new_user", "New user", 100)

        self.assertEqual(await self.store.verify_user(200), (True, 100, True))
        self.assertEqual(await self.store.verify_user(200), (False, None, False))

        referrer = await self.store.get_user(100)
        self.assertIsNotNone(referrer)
        self.assertEqual(referrer["balance"], REFERRAL_REWARD)
        self.assertEqual(referrer["referral_count"], 1)

    async def test_task_reward_is_not_paid_twice(self) -> None:
        await self.store.register_user(300, None, "Task user", None)
        task_id = await self.store.create_task("@samplechannel", "https://t.me/samplechannel", 12)

        self.assertEqual(await self.store.complete_task(300, task_id), ("completed", 12))
        self.assertEqual(
            await self.store.complete_task(300, task_id), ("already_completed", 0)
        )
        user = await self.store.get_user(300)
        self.assertEqual(user["balance"], 12)

    async def test_withdrawal_rejection_refunds_and_paid_request_does_not(self) -> None:
        await self.store.register_user(400, None, "Withdrawal user", None)
        task_id = await self.store.create_task(
            "@samplechannel",
            "https://t.me/samplechannel",
            MIN_WITHDRAWAL + 10,
        )
        await self.store.complete_task(400, task_id)

        status, rejected_id = await self.store.request_withdrawal(400, MIN_WITHDRAWAL)
        self.assertEqual(status, "created")
        rejected = await self.store.resolve_withdrawal(rejected_id, approved=False)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual((await self.store.get_user(400))["balance"], MIN_WITHDRAWAL + 10)

        status, paid_id = await self.store.request_withdrawal(400, MIN_WITHDRAWAL)
        self.assertEqual(status, "created")
        paid = await self.store.resolve_withdrawal(paid_id, approved=True)
        self.assertEqual(paid["status"], "paid")
        self.assertEqual((await self.store.get_user(400))["balance"], 10)

    async def test_support_message_can_be_replied_to_once(self) -> None:
        await self.store.register_user(500, "support_user", "Support user", None)
        message_id = await self.store.create_support_message(
            500, "support_user", "Помогите, пожалуйста"
        )
        self.assertTrue(await self.store.mark_support_replied(message_id, "Ответ"))
        self.assertFalse(await self.store.mark_support_replied(message_id, "Другой ответ"))

        ticket = await self.store.get_support_message(message_id)
        self.assertEqual(ticket["status"], "replied")
        self.assertEqual(ticket["reply"], "Ответ")


if __name__ == "__main__":
    unittest.main()
