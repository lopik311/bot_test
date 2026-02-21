import asyncio
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)

DB_PATH = os.getenv("DB_PATH", "bot.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PRIVATE_CHANNEL_ID = int(os.getenv("PRIVATE_CHANNEL_ID", "0"))
DEFAULT_CARD_NUMBER = os.getenv("DEFAULT_CARD_NUMBER", "2204320929425611")

SPB_TEXT = (
    "💳 СПБ перевод\n"
    "Описание:\n"
    "Перевод на карту Озон Банка:\n\n"
)

PLAN_90 = "90_days"
PLAN_YEAR = "year"

PLANS = {
    PLAN_90: {"title": "90 дней", "price": 9999, "days": 90},
    PLAN_YEAR: {"title": "1 год", "price": 14999, "days": 365},
}


@dataclass
class PendingPayment:
    user_id: int
    plan: str


pending_by_admin_msg: dict[int, PendingPayment] = {}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                plan TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                reminded_7 INTEGER NOT NULL DEFAULT 0,
                reminded_1 INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                status TEXT NOT NULL,
                proof_file_id TEXT,
                proof_type TEXT,
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                admin_id INTEGER
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('card_number', ?)",
            (DEFAULT_CARD_NUMBER,),
        )


def get_setting(key: str) -> str:
    with connect_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""


def set_setting(key: str, value: str) -> None:
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def save_payment(user_id: int, plan: str, proof_file_id: str, proof_type: str) -> str:
    payment_id = str(uuid.uuid4())
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO payments(id, user_id, plan, status, proof_file_id, proof_type, created_at)
            VALUES(?, ?, ?, 'pending', ?, ?, ?)
            """,
            (payment_id, user_id, plan, proof_file_id, proof_type, utcnow().isoformat()),
        )
    return payment_id


def get_payment(payment_id: str):
    with connect_db() as conn:
        return conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()


def set_payment_status(payment_id: str, status: str, admin_id: int) -> None:
    with connect_db() as conn:
        conn.execute(
            "UPDATE payments SET status=?, reviewed_at=?, admin_id=? WHERE id=?",
            (status, utcnow().isoformat(), admin_id, payment_id),
        )


def set_subscription(user: Message, plan: str) -> datetime:
    duration = timedelta(days=PLANS[plan]["days"])
    with connect_db() as conn:
        current = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=? AND status='active'",
            (user.from_user.id,),
        ).fetchone()
        base = utcnow()
        if current:
            current_exp = datetime.fromisoformat(current["expires_at"])
            if current_exp > base:
                base = current_exp
        new_exp = base + duration
        conn.execute(
            """
            INSERT INTO subscriptions(user_id, username, full_name, plan, expires_at, status, reminded_7, reminded_1)
            VALUES(?, ?, ?, ?, ?, 'active', 0, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                plan=excluded.plan,
                expires_at=excluded.expires_at,
                status='active',
                reminded_7=0,
                reminded_1=0
            """,
            (
                user.from_user.id,
                user.from_user.username,
                user.from_user.full_name,
                plan,
                new_exp.isoformat(),
            ),
        )
        return new_exp


def subscription_stats() -> tuple[int, int, int]:
    with connect_db() as conn:
        active = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE status='active'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) c FROM payments WHERE status='pending'"
        ).fetchone()["c"]
        expired = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE status='expired'"
        ).fetchone()["c"]
    return active, pending, expired


def mark_expired(user_id: int):
    with connect_db() as conn:
        conn.execute(
            "UPDATE subscriptions SET status='expired' WHERE user_id=?", (user_id,)
        )


def reset_reminder(user_id: int, day: int):
    col = "reminded_7" if day == 7 else "reminded_1"
    with connect_db() as conn:
        conn.execute(f"UPDATE subscriptions SET {col}=1 WHERE user_id=?", (user_id,))


def get_active_subscriptions():
    with connect_db() as conn:
        return conn.execute("SELECT * FROM subscriptions WHERE status='active'").fetchall()


def user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Купить 90 дней — 9999 ₽", callback_data=f"buy:{PLAN_90}")],
            [InlineKeyboardButton(text="Купить 1 год — 14999 ₽", callback_data=f"buy:{PLAN_YEAR}")],
            [InlineKeyboardButton(text="Мой статус", callback_data="my_status")],
        ]
    )


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="💳 Сменить номер карты", callback_data="admin_change_card")],
        ]
    )


async def send_payment_instructions(msg: Message, plan: str):
    card = get_setting("card_number")
    text = (
        f"Вы выбрали: <b>{PLANS[plan]['title']}</b> за <b>{PLANS[plan]['price']} ₽</b>.\n\n"
        f"{SPB_TEXT}<code>{card}</code>\n\n"
        "После оплаты пришлите в чат скриншот/чек одним сообщением."
    )
    await msg.answer(text, parse_mode=ParseMode.HTML)


async def create_single_use_link(bot: Bot) -> str:
    invite = await bot.create_chat_invite_link(
        chat_id=PRIVATE_CHANNEL_ID,
        member_limit=1,
        expire_date=int((utcnow() + timedelta(days=1)).timestamp()),
        name="Оплаченная подписка",
    )
    return invite.invite_link


async def scheduled_jobs(bot: Bot):
    for sub in get_active_subscriptions():
        user_id = sub["user_id"]
        exp = datetime.fromisoformat(sub["expires_at"])
        left = exp - utcnow()
        days_left = left.days

        if 6 <= days_left <= 7 and not sub["reminded_7"]:
            await bot.send_message(
                user_id,
                "⏰ Напоминание: до конца подписки осталось около 7 дней. Продлите заранее.",
                reply_markup=user_menu(),
            )
            reset_reminder(user_id, 7)

        if 0 <= days_left <= 1 and not sub["reminded_1"]:
            await bot.send_message(
                user_id,
                "⚠️ Подписка заканчивается менее чем через сутки. Чтобы не потерять доступ — продлите.",
                reply_markup=user_menu(),
            )
            reset_reminder(user_id, 1)

        if utcnow() >= exp:
            try:
                await bot.ban_chat_member(PRIVATE_CHANNEL_ID, user_id)
                await bot.unban_chat_member(PRIVATE_CHANNEL_ID, user_id, only_if_banned=True)
            except Exception as e:
                logging.warning("Failed to remove user %s from channel: %s", user_id, e)
            mark_expired(user_id)
            await bot.send_message(
                user_id,
                "❌ Подписка истекла, доступ к каналу отключен. Оплатите, чтобы получить новую ссылку.",
                reply_markup=user_menu(),
            )


async def main():
    if not BOT_TOKEN or not ADMIN_IDS or not PRIVATE_CHANNEL_ID:
        raise RuntimeError("Set BOT_TOKEN, ADMIN_IDS, PRIVATE_CHANNEL_ID in environment")

    init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def start(message: Message):
        await message.answer(
            "Привет! Я бот подписок. Все управление через кнопки ниже.",
            reply_markup=user_menu(),
        )

    @dp.message(Command("admin"))
    async def admin(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return await message.answer("Нет доступа")
        await message.answer("Админ-меню", reply_markup=admin_menu())

    @dp.callback_query(F.data.startswith("buy:"))
    async def buy(call: CallbackQuery):
        plan = call.data.split(":", 1)[1]
        await send_payment_instructions(call.message, plan)
        await call.answer()

    @dp.callback_query(F.data == "my_status")
    async def my_status(call: CallbackQuery):
        with connect_db() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE user_id=?", (call.from_user.id,)
            ).fetchone()
        if not row:
            await call.message.answer("Подписка не найдена.")
        else:
            await call.message.answer(
                f"Статус: {row['status']}\nДо: {row['expires_at']}\nТариф: {PLANS[row['plan']]['title']}"
            )
        await call.answer()

    @dp.message(F.photo | F.document)
    async def payment_proof(message: Message):
        caption = message.caption or ""
        plan = PLAN_90 if "90" in caption else PLAN_YEAR if "год" in caption.lower() else None

        if plan is None:
            # Берем последний незакрытый платеж, если есть
            with connect_db() as conn:
                pending = conn.execute(
                    "SELECT plan FROM payments WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
                    (message.from_user.id,),
                ).fetchone()
            plan = pending["plan"] if pending else PLAN_90

        if message.photo:
            file_id = message.photo[-1].file_id
            proof_type = "photo"
        else:
            file_id = message.document.file_id
            proof_type = "document"

        payment_id = save_payment(message.from_user.id, plan, file_id, proof_type)

        buttons = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        callback_data=f"approve:{payment_id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=f"reject:{payment_id}",
                    ),
                ]
            ]
        )

        admin_text = (
            f"Новая оплата #{payment_id}\n"
            f"Пользователь: {message.from_user.full_name} (@{message.from_user.username})\n"
            f"user_id: {message.from_user.id}\n"
            f"Тариф: {PLANS[plan]['title']} ({PLANS[plan]['price']} ₽)"
        )

        for admin_id in ADMIN_IDS:
            if proof_type == "photo":
                sent = await message.bot.send_photo(admin_id, file_id, caption=admin_text, reply_markup=buttons)
            else:
                sent = await message.bot.send_document(admin_id, file_id, caption=admin_text, reply_markup=buttons)
            pending_by_admin_msg[sent.message_id] = PendingPayment(
                user_id=message.from_user.id,
                plan=plan,
            )

        await message.answer("Чек отправлен администратору на проверку. Ожидайте подтверждения.")

    @dp.callback_query(F.data.startswith("approve:"))
    async def approve(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return await call.answer("Нет доступа", show_alert=True)

        payment_id = call.data.split(":", 1)[1]
        payment = get_payment(payment_id)
        if not payment or payment["status"] != "pending":
            return await call.answer("Уже обработано")

        set_payment_status(payment_id, "approved", call.from_user.id)

        user_chat = await bot.get_chat(payment["user_id"])
        fake_msg = Message.model_validate(
            {
                "message_id": 0,
                "date": int(datetime.now().timestamp()),
                "chat": {"id": payment["user_id"], "type": "private"},
                "from": {
                    "id": payment["user_id"],
                    "is_bot": False,
                    "first_name": user_chat.first_name or "User",
                },
            }
        )
        exp = set_subscription(fake_msg, payment["plan"])
        link = await create_single_use_link(bot)

        await bot.send_message(
            payment["user_id"],
            f"✅ Оплата подтверждена!\nВаша подписка активна до: {exp.isoformat()}\n"
            f"Одноразовая ссылка в канал:\n{link}",
        )
        await call.message.edit_reply_markup(reply_markup=None)
        await call.answer("Подтверждено")

    @dp.callback_query(F.data.startswith("reject:"))
    async def reject(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return await call.answer("Нет доступа", show_alert=True)

        payment_id = call.data.split(":", 1)[1]
        payment = get_payment(payment_id)
        if not payment or payment["status"] != "pending":
            return await call.answer("Уже обработано")

        set_payment_status(payment_id, "rejected", call.from_user.id)
        await bot.send_message(
            payment["user_id"],
            "❌ Платеж отклонен. Проверьте перевод и отправьте новый скриншот.",
            reply_markup=user_menu(),
        )
        await call.message.edit_reply_markup(reply_markup=None)
        await call.answer("Отклонено")

    @dp.callback_query(F.data == "admin_stats")
    async def admin_stats(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return await call.answer("Нет доступа", show_alert=True)
        active, pending, expired = subscription_stats()
        await call.message.answer(
            f"📊 Статистика:\nАктивных: {active}\nОжидают проверки: {pending}\nИстекших: {expired}"
        )
        await call.answer()

    @dp.callback_query(F.data == "admin_change_card")
    async def admin_change_card(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return await call.answer("Нет доступа", show_alert=True)
        await call.message.answer(
            "Отправьте новый номер карты сообщением в формате:\n/card 0000000000000000"
        )
        await call.answer()

    @dp.message(Command("card"))
    async def update_card(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return await message.answer("Нет доступа")
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            return await message.answer("Использование: /card 0000000000000000")
        set_setting("card_number", parts[1].strip())
        await message.answer("Номер карты обновлен ✅")

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(scheduled_jobs, "interval", minutes=30, args=[bot])
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
