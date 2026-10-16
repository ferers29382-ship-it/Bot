from __future__ import annotations

import asyncio
import logging
import os
import re
from urllib.parse import quote, urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from storage import MIN_WITHDRAWAL, REFERRAL_REWARD, Store


load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("referral_bot")


def read_admin_ids() -> set[int]:
    raw_ids = os.getenv("ADMIN_IDS", "")
    try:
        return {int(value.strip()) for value in raw_ids.split(",") if value.strip()}
    except ValueError as exc:
        raise RuntimeError("ADMIN_IDS must be a comma-separated list of Telegram numeric IDs") from exc


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it to Railway Variables.")

ADMIN_IDS = read_admin_ids()
if not ADMIN_IDS:
    logger.warning(
        "ADMIN_IDS is not set; the bot will start, but admin commands are disabled."
    )

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@prostofrogi").strip()
REQUIRED_CHANNEL_URL = os.getenv(
    "REQUIRED_CHANNEL_URL", "https://t.me/prostofrogi"
).strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/bot.sqlite3").strip()
store = Store(DATABASE_PATH)

router = Router()


class SupportFlow(StatesGroup):
    waiting_for_message = State()


class WithdrawalFlow(StatesGroup):
    waiting_for_amount = State()


class AddTaskFlow(StatesGroup):
    waiting_for_channel = State()
    waiting_for_reward = State()


class SupportReplyFlow(StatesGroup):
    waiting_for_reply = State()


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def main_menu(user_id: int | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="📋 Задания")],
        [KeyboardButton(text="🤝 Рефералы")],
        [KeyboardButton(text="⭐ Вывести"), KeyboardButton(text="💬 Поддержка")],
    ]
    if is_admin(user_id):
        keyboard.append([KeyboardButton(text="🛠 Админ-панель")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
    )


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подписаться на канал",
                    url=REQUIRED_CHANNEL_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Проверить подписку",
                    callback_data="gate:verify",
                )
            ],
        ]
    )


def welcome_text() -> str:
    return (
        "✈️ Добро пожаловать в «Реферальный бот»!\n"
        "👥 Приглашайте друзей и выполняйте задания, чтобы получать звёзды."
    )


async def send_gate(message: Message) -> None:
    await message.answer(
        "Чтобы пользоваться ботом, подпишитесь на канал, затем нажмите "
        "«Проверить подписку».",
        reply_markup=subscription_keyboard(),
    )


async def ensure_user_record(user_id: int, username: str | None, first_name: str) -> None:
    if await store.get_user(user_id) is None:
        await store.register_user(user_id, username, first_name or "Пользователь", None)


async def is_member_of(bot: Bot, user_id: int, channel_id: str) -> bool | None:
    """Return None when Telegram cannot verify membership (usually bot permissions)."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
    except TelegramAPIError:
        logger.exception("Could not check membership for user %s in %s", user_id, channel_id)
        return None

    status = getattr(member.status, "value", member.status)
    if status in {
        ChatMemberStatus.MEMBER.value,
        ChatMemberStatus.ADMINISTRATOR.value,
        ChatMemberStatus.CREATOR.value,
    }:
        return True
    if status == ChatMemberStatus.RESTRICTED.value:
        return bool(getattr(member, "is_member", False))
    return False


async def access_message(message: Message, bot: Bot) -> bool:
    user = message.from_user
    if not user:
        return False
    if is_admin(user.id):
        return True

    subscribed = await is_member_of(bot, user.id, REQUIRED_CHANNEL)
    if subscribed is True:
        await ensure_user_record(user.id, user.username, user.first_name)
        await store.verify_user(user.id)
        return True
    if subscribed is False:
        await send_gate(message)
    else:
        await message.answer(
            "Не удалось проверить подписку. Попросите администратора добавить бота "
            "администратором канала и попробуйте ещё раз."
        )
    return False


async def access_callback(callback: CallbackQuery, bot: Bot) -> bool:
    user = callback.from_user
    if is_admin(user.id):
        return True

    subscribed = await is_member_of(bot, user.id, REQUIRED_CHANNEL)
    if subscribed is True:
        await ensure_user_record(user.id, user.username, user.first_name)
        await store.verify_user(user.id)
        return True
    if subscribed is False:
        await callback.answer("Вы не подписаны на канал.", show_alert=True)
        if callback.message:
            await callback.message.answer(
                "Сначала подпишитесь на канал и нажмите «Проверить подписку».",
                reply_markup=subscription_keyboard(),
            )
    else:
        await callback.answer(
            "Не удалось проверить подписку. Сообщите администратору бота.",
            show_alert=True,
        )
    return False


def parse_public_channel(value: str) -> tuple[str, str] | None:
    text = value.strip()
    if text.startswith("@"):
        username = text[1:]
    else:
        candidate = text if "://" in text else f"https://{text}"
        parsed = urlparse(candidate)
        if parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or parts[0] in {"joinchat", "c", "addlist"} or parts[0].startswith("+"):
            return None
        username = parts[0]

    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    return f"@{username}", f"https://t.me/{username}"


async def notify_referrer(bot: Bot, referrer_id: int, referred_name: str) -> None:
    try:
        await bot.send_message(
            referrer_id,
            f"✅ Ваш реферал {referred_name} подписался на канал. "
            f"Начислено {REFERRAL_REWARD} ⭐.",
        )
    except TelegramAPIError:
        logger.info("Could not notify referrer %s", referrer_id)


@router.message(Command("start"))
async def start_handler(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return

    referrer_id: int | None = None
    payload = (message.text or "").partition(" ")[2].strip()
    if payload.startswith("ref_"):
        try:
            referrer_id = int(payload.removeprefix("ref_"))
        except ValueError:
            referrer_id = None

    await store.register_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name or "Пользователь",
        referrer_id=referrer_id,
    )

    if is_admin(user.id):
        await message.answer(welcome_text(), reply_markup=main_menu(user.id))
        return

    subscribed = await is_member_of(bot, user.id, REQUIRED_CHANNEL)
    if subscribed is True:
        _, awarded_referrer, _ = await store.verify_user(user.id)
        await message.answer(welcome_text(), reply_markup=main_menu(user.id))
        if awarded_referrer:
            referred_name = f"@{user.username}" if user.username else user.first_name
            await notify_referrer(bot, awarded_referrer, referred_name)
    elif subscribed is False:
        await send_gate(message)
    else:
        await message.answer(
            "Не удалось проверить подписку. Администратор должен добавить бота "
            "в канал как администратора."
        )


@router.message(Command("myid"))
async def my_id_handler(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Действие отменено.",
        reply_markup=main_menu(message.from_user.id if message.from_user else None),
    )


@router.callback_query(F.data == "gate:verify")
async def verify_subscription_handler(callback: CallbackQuery, bot: Bot) -> None:
    subscribed = await is_member_of(bot, callback.from_user.id, REQUIRED_CHANNEL)
    if subscribed is False:
        await callback.answer("Вы не подписаны на канал.", show_alert=True)
        if callback.message:
            await callback.message.answer(
                "Вы не подписаны на канал. Подпишитесь и нажмите кнопку проверки.",
                reply_markup=subscription_keyboard(),
            )
        return
    if subscribed is None:
        await callback.answer(
            "Не удалось проверить подписку. Сообщите администратору бота.",
            show_alert=True,
        )
        return

    await ensure_user_record(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.first_name,
    )
    _, awarded_referrer, first_verification = await store.verify_user(callback.from_user.id)
    await callback.answer("Подписка подтверждена.")
    if callback.message:
        await callback.message.answer(
            "✅ Подписка подтверждена!",
            reply_markup=main_menu(callback.from_user.id),
        )
        await callback.message.answer(welcome_text())

    if awarded_referrer:
        name = (
            f"@{callback.from_user.username}"
            if callback.from_user.username
            else callback.from_user.first_name
        )
        await notify_referrer(bot, awarded_referrer, name)
    elif not first_verification:
        logger.info("User %s re-verified channel membership", callback.from_user.id)


@router.message(Command("admin"))
async def admin_menu_handler(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("У вас нет доступа к панели администратора.")
        return
    await message.answer(
        "🛠 Панель администратора",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="➕ Добавить задание",
                        callback_data="admin:add_task",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💬 Сообщения поддержки",
                        callback_data="admin:support",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⭐ Заявки на вывод",
                        callback_data="admin:withdrawals",
                    )
                ],
            ]
        ),
    )


@router.message(F.text == "🛠 Админ-панель")
async def admin_menu_button_handler(message: Message) -> None:
    await admin_menu_handler(message)


@router.message(F.text == "👤 Мой профиль")
async def profile_handler(message: Message, bot: Bot) -> None:
    if not await access_message(message, bot):
        return
    user = message.from_user
    if not user:
        return
    profile = await store.get_user(user.id)
    if not profile:
        await message.answer("Нажмите /start, чтобы создать профиль.")
        return

    username = f"@{profile['username']}" if profile["username"] else "не указан"
    registered = profile["created_at"].replace("T", " ").replace("+00:00", " UTC")
    await message.answer(
        "👤 Ваш профиль\n\n"
        f"🆔 ID: {profile['user_id']}\n"
        f"Имя: {profile['first_name']}\n"
        f"Username: {username}\n"
        f"⭐ Баланс: {profile['balance']} ⭐\n"
        f"🤝 Рефералов: {profile['referral_count']}\n"
        "📣 Подписка подтверждена: ✅ Да\n"
        f"📅 Регистрация: {registered}"
    )


@router.message(F.text == "🤝 Рефералы")
async def referrals_handler(message: Message, bot: Bot) -> None:
    if not await access_message(message, bot):
        return
    user = message.from_user
    if not user:
        return

    me = await bot.get_me()
    if not me.username:
        await message.answer("Не удалось получить ссылку бота. Попробуйте позже.")
        return
    referral_link = f"https://t.me/{me.username}?start=ref_{user.id}"
    share_link = (
        "https://t.me/share/url"
        f"?url={quote(referral_link, safe='')}"
        f"&text={quote('Присоединяйся к боту и выполняй задания!')}"
    )
    profile = await store.get_user(user.id)
    referral_count = profile["referral_count"] if profile else 0
    await message.answer(
        "🤝 Реферальная программа\n\n"
        f"За каждого нового пользователя, который запустит бота и подтвердит "
        f"подписку, вы получите {REFERRAL_REWARD} ⭐.\n\n"
        f"Приглашено: {referral_count}\n"
        f"Ваша ссылка:\n{referral_link}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📩 Поделиться", url=share_link)]
            ]
        ),
    )


@router.message(F.text == "📋 Задания")
async def tasks_handler(message: Message, bot: Bot) -> None:
    if not await access_message(message, bot):
        return
    user = message.from_user
    if not user:
        return

    tasks = await store.list_available_tasks(user.id)
    if not tasks:
        await message.answer("📋 Сейчас нет доступных заданий.")
        return
    buttons = [
        [
            InlineKeyboardButton(
                text=f"Задание #{task['task_id']} · {task['reward']} ⭐",
                callback_data=f"task:open:{task['task_id']}",
            )
        ]
        for task in tasks
    ]
    await message.answer(
        "📋 Доступные задания\nВыберите задание:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("task:open:"))
async def open_task_handler(callback: CallbackQuery, bot: Bot) -> None:
    if not await access_callback(callback, bot):
        return
    try:
        task_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Не удалось открыть задание.", show_alert=True)
        return

    task = await store.get_task(task_id)
    if not task:
        await callback.answer("Это задание больше недоступно.", show_alert=True)
        return

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"Подпишитесь на канал задания. После проверки будет начислено "
            f"{task['reward']} ⭐.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📣 Открыть канал", url=task["channel_url"])],
                    [
                        InlineKeyboardButton(
                            text="✅ Проверить подписку",
                            callback_data=f"task:check:{task_id}",
                        )
                    ],
                ]
            ),
        )


@router.callback_query(F.data.startswith("task:check:"))
async def check_task_handler(callback: CallbackQuery, bot: Bot) -> None:
    if not await access_callback(callback, bot):
        return
    try:
        task_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Не удалось проверить задание.", show_alert=True)
        return

    task = await store.get_task(task_id)
    if not task:
        await callback.answer("Это задание больше недоступно.", show_alert=True)
        return

    subscribed = await is_member_of(bot, callback.from_user.id, task["channel_id"])
    if subscribed is False:
        await callback.answer("Подписка не найдена. Сначала подпишитесь.", show_alert=True)
        return
    if subscribed is None:
        await callback.answer(
            "Не удалось проверить канал. Попросите администратора проверить права бота.",
            show_alert=True,
        )
        return

    status, reward = await store.complete_task(callback.from_user.id, task_id)
    if status == "completed":
        await callback.answer("Подписка подтверждена!")
        if callback.message:
            await callback.message.answer(f"✅ Готово! На баланс начислено {reward} ⭐.")
    elif status == "already_completed":
        await callback.answer("За это задание награда уже получена.", show_alert=True)
    else:
        await callback.answer("Задание недоступно.", show_alert=True)


@router.message(F.text == "💬 Поддержка")
async def support_start_handler(
    message: Message, bot: Bot, state: FSMContext
) -> None:
    if not await access_message(message, bot):
        return
    await state.set_state(SupportFlow.waiting_for_message)
    await message.answer("Напишите сообщение для поддержки одним сообщением.")


@router.message(SupportFlow.waiting_for_message, F.text)
async def support_message_handler(
    message: Message, state: FSMContext
) -> None:
    user = message.from_user
    text = (message.text or "").strip()
    if not user or not text:
        await message.answer("Отправьте текстовое сообщение.")
        return
    if len(text) > 4000:
        await message.answer("Сообщение слишком длинное. Максимум — 4000 символов.")
        return

    ticket_id = await store.create_support_message(user.id, user.username, text)
    await state.clear()
    await message.answer(f"✅ Сообщение #{ticket_id} отправлено в поддержку.")


@router.message(F.text == "⭐ Вывести")
async def withdrawal_start_handler(
    message: Message, bot: Bot, state: FSMContext
) -> None:
    if not await access_message(message, bot):
        return
    user = message.from_user
    if not user:
        return
    profile = await store.get_user(user.id)
    if not profile or profile["balance"] < MIN_WITHDRAWAL:
        balance = profile["balance"] if profile else 0
        await message.answer(
            f"Минимальная сумма вывода — {MIN_WITHDRAWAL} ⭐. "
            f"Ваш баланс: {balance} ⭐."
        )
        return

    await state.set_state(WithdrawalFlow.waiting_for_amount)
    await message.answer(
        f"Введите количество звёзд для вывода (от {MIN_WITHDRAWAL} до "
        f"{profile['balance']} ⭐)."
    )


@router.message(WithdrawalFlow.waiting_for_amount, F.text)
async def withdrawal_amount_handler(
    message: Message, state: FSMContext
) -> None:
    text = (message.text or "").strip()
    if not text.isdecimal():
        await message.answer("Введите целое число звёзд.")
        return
    amount = int(text)
    if amount < MIN_WITHDRAWAL:
        await message.answer(f"Минимальная сумма вывода — {MIN_WITHDRAWAL} ⭐.")
        return

    user = message.from_user
    if not user:
        return
    status, request_id = await store.request_withdrawal(user.id, amount)
    if status == "insufficient_balance":
        profile = await store.get_user(user.id)
        balance = profile["balance"] if profile else 0
        await message.answer(f"Недостаточно звёзд. Ваш баланс: {balance} ⭐.")
        return
    if status != "created":
        await message.answer("Не удалось создать заявку. Нажмите /start и попробуйте снова.")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        f"✅ Заявка #{request_id} на {amount} ⭐ отправлена администратору. "
        "Сумма временно списана с баланса."
    )


@router.callback_query(F.data.startswith("admin:"))
async def admin_callback_handler(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    data = callback.data or ""
    if data == "admin:add_task":
        await state.set_state(AddTaskFlow.waiting_for_channel)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Отправьте публичную ссылку на канал задания, например "
                "https://t.me/channelname."
            )
        return

    if data == "admin:support":
        tickets = await store.list_open_support()
        await callback.answer()
        if callback.message:
            if not tickets:
                await callback.message.answer("Открытых сообщений поддержки нет.")
                return
            buttons = []
            for ticket in tickets:
                name = f"@{ticket['username']}" if ticket["username"] else str(ticket["user_id"])
                preview = ticket["message"].replace("\n", " ")[:28]
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=f"#{ticket['message_id']} {name}: {preview}",
                            callback_data=f"admin:support:{ticket['message_id']}",
                        )
                    ]
                )
            await callback.message.answer(
                "Открытые сообщения поддержки:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
        return

    if data.startswith("admin:support:"):
        try:
            ticket_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Неверный номер обращения.", show_alert=True)
            return
        ticket = await store.get_support_message(ticket_id)
        if not ticket or ticket["status"] != "open":
            await callback.answer("Обращение уже закрыто или не найдено.", show_alert=True)
            return
        username = f"@{ticket['username']}" if ticket["username"] else "не указан"
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"Обращение #{ticket_id}\n"
                f"Пользователь: {username} (ID {ticket['user_id']})\n\n"
                f"{ticket['message']}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✍️ Ответить",
                                callback_data=f"admin:reply:{ticket_id}",
                            )
                        ]
                    ]
                ),
            )
        return

    if data.startswith("admin:reply:"):
        try:
            ticket_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Неверный номер обращения.", show_alert=True)
            return
        ticket = await store.get_support_message(ticket_id)
        if not ticket or ticket["status"] != "open":
            await callback.answer("Обращение уже закрыто или не найдено.", show_alert=True)
            return
        await state.set_state(SupportReplyFlow.waiting_for_reply)
        await state.update_data(ticket_id=ticket_id)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"Введите ответ пользователю по обращению #{ticket_id} одним сообщением."
            )
        return

    if data == "admin:withdrawals":
        requests = await store.list_pending_withdrawals()
        await callback.answer()
        if callback.message:
            if not requests:
                await callback.message.answer("Ожидающих заявок на вывод нет.")
                return
            for request in requests:
                username = (
                    f"@{request['username']}"
                    if request["username"]
                    else request["first_name"]
                )
                await callback.message.answer(
                    f"Заявка #{request['withdrawal_id']}\n"
                    f"Пользователь: {username} (ID {request['user_id']})\n"
                    f"Сумма: {request['amount']} ⭐",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✅ Отметить выплаченной",
                                    callback_data=(
                                        f"admin:withdraw:paid:{request['withdrawal_id']}"
                                    ),
                                ),
                                InlineKeyboardButton(
                                    text="❌ Отклонить и вернуть",
                                    callback_data=(
                                        f"admin:withdraw:reject:{request['withdrawal_id']}"
                                    ),
                                ),
                            ]
                        ]
                    ),
                )
        return

    if data.startswith("admin:withdraw:"):
        parts = data.split(":")
        if len(parts) != 4 or parts[2] not in {"paid", "reject"}:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        try:
            withdrawal_id = int(parts[3])
        except ValueError:
            await callback.answer("Неверный номер заявки.", show_alert=True)
            return

        approved = parts[2] == "paid"
        request = await store.resolve_withdrawal(withdrawal_id, approved)
        if not request:
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return
        await callback.answer("Заявка обработана.")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                f"Заявка #{withdrawal_id}: "
                f"{'отмечена выплаченной' if approved else 'отклонена, баланс возвращён'}."
            )
        try:
            if approved:
                await bot.send_message(
                    request["user_id"],
                    f"✅ Ваша заявка #{withdrawal_id} на {request['amount']} ⭐ "
                    "отмечена администратором как выплаченная.",
                )
            else:
                await bot.send_message(
                    request["user_id"],
                    f"Заявка #{withdrawal_id} отклонена. "
                    f"{request['amount']} ⭐ возвращены на баланс.",
                )
        except TelegramAPIError:
            logger.info("Could not notify user %s about withdrawal", request["user_id"])
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


@router.message(AddTaskFlow.waiting_for_channel, F.text)
async def add_task_channel_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await state.clear()
        await message.answer("Нет доступа.")
        return
    parsed = parse_public_channel(message.text or "")
    if not parsed:
        await message.answer(
            "Ссылка не распознана. Отправьте ссылку на публичный канал, "
            "например https://t.me/channelname."
        )
        return
    channel_id, channel_url = parsed
    await state.update_data(channel_id=channel_id, channel_url=channel_url)
    await state.set_state(AddTaskFlow.waiting_for_reward)
    await message.answer("Введите награду за задание в целых звёздах.")


@router.message(AddTaskFlow.waiting_for_reward, F.text)
async def add_task_reward_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await state.clear()
        await message.answer("Нет доступа.")
        return
    text = (message.text or "").strip()
    if not text.isdecimal() or int(text) < 1:
        await message.answer("Введите положительное целое число звёзд.")
        return
    reward = int(text)
    if reward > 1_000_000:
        await message.answer("Награда не должна превышать 1 000 000 ⭐.")
        return

    data = await state.get_data()
    task_id = await store.create_task(data["channel_id"], data["channel_url"], reward)
    await state.clear()
    await message.answer(
        f"✅ Задание #{task_id} добавлено.\n"
        f"Канал: {data['channel_url']}\n"
        f"Награда: {reward} ⭐."
    )


@router.message(SupportReplyFlow.waiting_for_reply, F.text)
async def support_reply_handler(
    message: Message, bot: Bot, state: FSMContext
) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await state.clear()
        await message.answer("Нет доступа.")
        return
    reply = (message.text or "").strip()
    if not reply:
        await message.answer("Введите текстовый ответ.")
        return
    if len(reply) > 4000:
        await message.answer("Ответ слишком длинный. Максимум — 4000 символов.")
        return

    data = await state.get_data()
    ticket = await store.get_support_message(int(data["ticket_id"]))
    if not ticket or ticket["status"] != "open":
        await state.clear()
        await message.answer("Обращение уже закрыто или не найдено.")
        return
    try:
        await bot.send_message(
            ticket["user_id"],
            f"💬 Ответ поддержки по обращению #{ticket['message_id']}:\n\n{reply}",
        )
    except TelegramAPIError:
        await message.answer(
            "Не удалось отправить ответ пользователю. Обращение оставлено открытым."
        )
        await state.clear()
        return

    await store.mark_support_replied(ticket["message_id"], reply)
    await state.clear()
    await message.answer(f"✅ Ответ отправлен по обращению #{ticket['message_id']}.")


@router.message()
async def fallback_handler(message: Message, bot: Bot) -> None:
    if not await access_message(message, bot):
        return
    await message.answer(
        "Выберите действие в меню ниже или отправьте /start.",
        reply_markup=main_menu(message.from_user.id if message.from_user else None),
    )


async def main() -> None:
    await store.initialize()
    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    logger.info("Starting Telegram bot in long-polling mode")
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
