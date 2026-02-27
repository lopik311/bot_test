import asyncio
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

DB_PATH = os.getenv("DB_PATH", "bot.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PRIVATE_CHANNEL_ID = int(os.getenv("PRIVATE_CHANNEL_ID", "0"))
DEFAULT_CARD_NUMBER = os.getenv("DEFAULT_CARD_NUMBER", "2204320929425611")
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "Europe/Moscow")
FSM_STORAGE = os.getenv("FSM_STORAGE", "redis").lower()
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PAYMENT_DUPLICATE_WINDOW_MIN = int(os.getenv("PAYMENT_DUPLICATE_WINDOW_MIN", "15"))

PLAN_90 = "90_days"
PLAN_YEAR = "year"

PLANS = {
    PLAN_90: {"title": "90 дней", "price": 5999, "days": 90},
    PLAN_YEAR: {"title": "1 год", "price": 14999, "days": 365},
}

SPB_TEXT = (
    "💳 СПБ перевод\n"
    "Описание:\n"
    "Перевод на карту Озон Банка:\n\n"
)


class UserFlow(StatesGroup):
    waiting_payment_proof = State()


class AdminFlow(StatesGroup):
    waiting_new_card = State()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def user_tz() -> ZoneInfo:
    try:
        return ZoneInfo(USER_TIMEZONE)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def format_date_for_user(dt_or_iso: datetime | str) -> str:
    try:
        value = dt_or_iso if isinstance(dt_or_iso, datetime) else datetime.fromisoformat(dt_or_iso)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(user_tz()).date().isoformat()
    except Exception:  # noqa: BLE001
        return str(dt_or_iso)[:10]


def build_fsm_storage():
    if FSM_STORAGE == "memory":
        logging.info("FSM storage: memory")
        return MemoryStorage()

    try:
        storage = RedisStorage.from_url(REDIS_URL)
        logging.info("FSM storage: redis (%s)", REDIS_URL)
        return storage
    except Exception as error:  # noqa: BLE001
        logging.warning("Не удалось подключить Redis storage (%s). Используется MemoryStorage.", error)
        return MemoryStorage()


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
                username TEXT,
                full_name TEXT,
                plan TEXT NOT NULL,
                status TEXT NOT NULL,
                proof_file_id TEXT NOT NULL,
                proof_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                admin_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                first_seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('card_number', ?)",
            (DEFAULT_CARD_NUMBER,),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_dedup ON payments(user_id, proof_file_id, status, created_at)"
        )


def register_user_if_new(user_id: int, username: str | None, full_name: str | None) -> bool:
    with connect_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO users(user_id, username, full_name, first_seen_at) VALUES(?, ?, ?, ?)",
            (user_id, username, full_name, utcnow().isoformat()),
        )
        if cur.rowcount == 1:
            return True
        conn.execute(
            "UPDATE users SET username=?, full_name=? WHERE user_id=?",
            (username, full_name, user_id),
        )
        return False


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


def save_payment(message: Message, plan: str, proof_file_id: str, proof_type: str) -> str:
    payment_id = str(uuid.uuid4())
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO payments(id, user_id, username, full_name, plan, status, proof_file_id, proof_type, created_at)
            VALUES(?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                payment_id,
                message.from_user.id,
                message.from_user.username,
                message.from_user.full_name,
                plan,
                proof_file_id,
                proof_type,
                utcnow().isoformat(),
            ),
        )
    return payment_id


def get_payment(payment_id: str):
    with connect_db() as conn:
        return conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()


def get_pending_payments(limit: int = 20):
    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM payments WHERE status='pending' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def find_recent_duplicate_payment(user_id: int, proof_file_id: str, window_min: int = PAYMENT_DUPLICATE_WINDOW_MIN):
    cutoff = (utcnow() - timedelta(minutes=window_min)).isoformat()
    with connect_db() as conn:
        return conn.execute(
            """
            SELECT id, created_at
            FROM payments
            WHERE user_id=? AND proof_file_id=? AND status='pending' AND created_at>=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, proof_file_id, cutoff),
        ).fetchone()


def set_payment_status(payment_id: str, status: str, admin_id: int) -> None:
    with connect_db() as conn:
        conn.execute(
            "UPDATE payments SET status=?, reviewed_at=?, admin_id=? WHERE id=? AND status='pending'",
            (status, utcnow().isoformat(), admin_id, payment_id),
        )


def set_subscription(user_id: int, username: str | None, full_name: str | None, plan: str) -> datetime:
    duration = timedelta(days=PLANS[plan]["days"])
    with connect_db() as conn:
        current = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=? AND status='active'",
            (user_id,),
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
            (user_id, username, full_name, plan, new_exp.isoformat()),
        )
        return new_exp


def subscription_stats() -> tuple[int, int, int]:
    with connect_db() as conn:
        active = conn.execute("SELECT COUNT(*) c FROM subscriptions WHERE status='active'").fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) c FROM payments WHERE status='pending'").fetchone()["c"]
        expired = conn.execute("SELECT COUNT(*) c FROM subscriptions WHERE status='expired'").fetchone()["c"]
    return active, pending, expired


def mark_expired(user_id: int):
    with connect_db() as conn:
        conn.execute("UPDATE subscriptions SET status='expired' WHERE user_id=?", (user_id,))


def mark_reminder(user_id: int, days: int):
    col = "reminded_7" if days == 7 else "reminded_1"
    with connect_db() as conn:
        conn.execute(f"UPDATE subscriptions SET {col}=1 WHERE user_id=?", (user_id,))


def get_active_subscriptions():
    with connect_db() as conn:
        return conn.execute("SELECT * FROM subscriptions WHERE status='active'").fetchall()


def get_user_subscription(user_id: int):
    with connect_db() as conn:
        return conn.execute("SELECT * FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()


def user_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Купить 90 дней — 5999 ₽", callback_data=f"buy:{PLAN_90}")],
        [InlineKeyboardButton(text="Купить 1 год — 14999 ₽", callback_data=f"buy:{PLAN_YEAR}")],
        [InlineKeyboardButton(text="Мой статус", callback_data="my_status")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="⚙️ Админ. меню", callback_data="open_admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📥 Ожидающие оплаты", callback_data="admin_pending")],
            [InlineKeyboardButton(text="💳 Сменить номер карты", callback_data="admin_change_card")],
        ]
    )


def payment_review_kb(payment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve:{payment_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{payment_id}"),
            ]
        ]
    )


async def send_payment_instructions(message: Message, plan: str):
    card = get_setting("card_number")
    await message.answer(
        f"Вы выбрали тариф: <b>{PLANS[plan]['title']}</b> за <b>{PLANS[plan]['price']} ₽</b>.\n\n"
        f"{SPB_TEXT}<code>{card}</code>\n\n"
        "После оплаты пришлите скриншот/чек в этот чат."
        "\nВажно: отправьте одним сообщением (фото или файл).",
        parse_mode=ParseMode.HTML,
    )


async def create_single_use_link(bot: Bot) -> str:
    invite = await bot.create_chat_invite_link(
        chat_id=PRIVATE_CHANNEL_ID,
        member_limit=1,
        expire_date=int((utcnow() + timedelta(days=1)).timestamp()),
        name="Оплаченная подписка",
    )
    return invite.invite_link


async def process_scheduler(bot: Bot):
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
            mark_reminder(user_id, 7)

        if 0 <= days_left <= 1 and not sub["reminded_1"]:
            await bot.send_message(
                user_id,
                "⚠️ Подписка заканчивается менее чем через сутки. Чтобы не потерять доступ — продлите.",
                reply_markup=user_menu(),
            )
            mark_reminder(user_id, 1)

        if utcnow() >= exp:
            try:
                await bot.ban_chat_member(PRIVATE_CHANNEL_ID, user_id)
                await bot.unban_chat_member(PRIVATE_CHANNEL_ID, user_id, only_if_banned=True)
            except Exception as error:  # noqa: BLE001
                logging.warning("Не удалось удалить пользователя %s из канала: %s", user_id, error)
            mark_expired(user_id)
            await bot.send_message(
                user_id,
                "❌ Подписка истекла. Доступ в канал отключен. Оплатите заново для получения новой ссылки.",
                reply_markup=user_menu(),
            )


async def main():
    if not BOT_TOKEN or not ADMIN_IDS or not PRIVATE_CHANNEL_ID:
        raise RuntimeError("Set BOT_TOKEN, ADMIN_IDS, PRIVATE_CHANNEL_ID in environment")

    init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=build_fsm_storage())

    @dp.message(Command("start"))
    async def start(message: Message):
        await message.answer(
            "Тут собран весь мой эксклюзивный секретный контент за все время моей работы ❤️",
            reply_markup=user_menu(is_admin=message.from_user.id in ADMIN_IDS),
        )
        is_new_user = register_user_if_new(
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )
        if is_new_user:
            username = f"@{message.from_user.username}" if message.from_user.username else "(без username)"
            admin_text = (
                "🆕 Новый пользователь зарегистрировался!\n\n"
                f"👤 Пользователь: {username}\n"
                f"🆔 ID: {message.from_user.id}"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, admin_text)
                except Exception as error:  # noqa: BLE001
                    logging.warning("Не удалось отправить уведомление админу %s: %s", admin_id, error)

    @dp.message(Command("admin"))
    async def admin(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return await message.answer("Нет доступа")
        await message.answer("Админ-меню", reply_markup=admin_menu())

    @dp.callback_query(F.data == "open_admin_menu")
    async def open_admin_menu(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return await call.answer("Нет доступа", show_alert=True)
        await call.message.answer("Админ-меню", reply_markup=admin_menu())
        await call.answer()

    @dp.callback_query(F.data.startswith("buy:"))
    async def buy(call: CallbackQuery, state: FSMContext):
        plan = call.data.split(":", 1)[1]
        await state.set_state(UserFlow.waiting_payment_proof)
        await state.update_data(plan=plan)
        await send_payment_instructions(call.message, plan)
        await call.answer()

    @dp.callback_query(F.data == "my_status")
    async def my_status(call: CallbackQuery):
        row = get_user_subscription(call.from_user.id)
        if not row:
            await call.message.answer("У вас пока нет активной подписки.", reply_markup=user_menu())
        else:
            await call.message.answer(
                f"Тариф: {PLANS[row['plan']]['title']}\n"
                f"Статус: {row['status']}\n"
                f"Действует до: {format_date_for_user(row['expires_at'])}",
                reply_markup=user_menu(is_admin=call.from_user.id in ADMIN_IDS),
            )
        await call.answer()

    @dp.message(UserFlow.waiting_payment_proof, F.photo | F.document)
    async def payment_proof(message: Message, state: FSMContext):
        data = await state.get_data()
        plan = data.get("plan")
        if not plan:
            await state.clear()
            return await message.answer("Сначала выберите тариф кнопками.", reply_markup=user_menu())

        if message.photo:
            proof_file_id = message.photo[-1].file_id
            proof_type = "photo"
        else:
            proof_file_id = message.document.file_id
            proof_type = "document"

        duplicate = find_recent_duplicate_payment(
            user_id=message.from_user.id,
            proof_file_id=proof_file_id,
        )
        if duplicate:
            await state.clear()
            return await message.answer(
                "Этот чек уже отправлен на проверку. Пожалуйста, дождитесь решения администратора.",
                reply_markup=user_menu(is_admin=message.from_user.id in ADMIN_IDS),
            )

        payment_id = save_payment(message, plan, proof_file_id, proof_type)
        await state.clear()

        admin_text = (
            f"Новый платеж #{payment_id}\n"
            f"Пользователь: {message.from_user.full_name} (@{message.from_user.username})\n"
            f"ID: {message.from_user.id}\n"
            f"Тариф: {PLANS[plan]['title']} ({PLANS[plan]['price']} ₽)"
        )

        for admin_id in ADMIN_IDS:
            if proof_type == "photo":
                await bot.send_photo(admin_id, proof_file_id, caption=admin_text, reply_markup=payment_review_kb(payment_id))
            else:
                await bot.send_document(admin_id, proof_file_id, caption=admin_text, reply_markup=payment_review_kb(payment_id))

        await message.answer("Чек отправлен администратору. Ожидайте решения ✅")

    @dp.message(F.photo | F.document)
    async def payment_proof_no_state(message: Message):
        await message.answer(
            "Сначала выберите тариф кнопками, затем отправьте чек.",
            reply_markup=user_menu(is_admin=message.from_user.id in ADMIN_IDS),
        )

    @dp.callback_query(F.data.startswith("approve:"))
    async def approve(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return await call.answer("Нет доступа", show_alert=True)

        payment_id = call.data.split(":", 1)[1]
        payment = get_payment(payment_id)
        if not payment or payment["status"] != "pending":
            return await call.answer("Уже обработано")

        set_payment_status(payment_id, "approved", call.from_user.id)
        exp = set_subscription(
            payment["user_id"],
            payment["username"],
            payment["full_name"],
            payment["plan"],
        )
        invite_link = await create_single_use_link(bot)

        await bot.send_message(
            payment["user_id"],
            f"✅ Оплата подтверждена!\n"
            f"Подписка активна до: {format_date_for_user(exp)}\n"
            f"Ваша одноразовая ссылка:\n{invite_link}",
            reply_markup=user_menu(),
        )
        await call.message.edit_reply_markup(reply_markup=None)
        await call.answer("Оплата подтверждена")

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
        await call.answer("Платеж отклонен")

    @dp.callback_query(F.data == "admin_stats")
    async def admin_stats(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return await call.answer("Нет доступа", show_alert=True)
        active, pending, expired = subscription_stats()
        await call.message.answer(
            f"📊 Статистика\nАктивные подписки: {active}\n"
            f"Ожидают проверки: {pending}\nИстекшие: {expired}",
            reply_markup=admin_menu(),
        )
        await call.answer()

    @dp.callback_query(F.data == "admin_pending")
    async def admin_pending(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return await call.answer("Нет доступа", show_alert=True)

        pending = get_pending_payments(limit=20)
        if not pending:
            await call.message.answer("Сейчас нет ожидающих оплат.", reply_markup=admin_menu())
            return await call.answer()

        lines = ["📥 Ожидающие оплаты (последние 20):"]
        keyboard_rows = []
        for row in pending:
            lines.append(
                f"• {row['id'][:8]} | ID:{row['user_id']} | {PLANS[row['plan']]['title']} | {format_date_for_user(row['created_at'])}"
            )
            keyboard_rows.append(
                [InlineKeyboardButton(text=f"Открыть {row['id'][:8]}", callback_data=f"pending_open:{row['id']}")]
            )

        await call.message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )
        await call.answer()

    @dp.callback_query(F.data.startswith("pending_open:"))
    async def pending_open(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return await call.answer("Нет доступа", show_alert=True)

        payment_id = call.data.split(":", 1)[1]
        payment = get_payment(payment_id)
        if not payment:
            return await call.answer("Платеж не найден", show_alert=True)

        caption = (
            f"Платеж #{payment['id']}\n"
            f"Статус: {payment['status']}\n"
            f"Пользователь: {payment['full_name']} (@{payment['username']})\n"
            f"ID: {payment['user_id']}\n"
            f"Тариф: {PLANS[payment['plan']]['title']} ({PLANS[payment['plan']]['price']} ₽)\n"
            f"Создан: {format_date_for_user(payment['created_at'])}"
        )
        kb = payment_review_kb(payment_id) if payment["status"] == "pending" else None

        if payment["proof_type"] == "photo":
            await bot.send_photo(call.from_user.id, payment["proof_file_id"], caption=caption, reply_markup=kb)
        else:
            await bot.send_document(call.from_user.id, payment["proof_file_id"], caption=caption, reply_markup=kb)
        await call.answer()

    @dp.callback_query(F.data == "admin_change_card")
    async def admin_change_card(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return await call.answer("Нет доступа", show_alert=True)
        await state.set_state(AdminFlow.waiting_new_card)
        await call.message.answer("Введите новый номер карты одним сообщением.")
        await call.answer()

    @dp.message(AdminFlow.waiting_new_card, F.text)
    async def handle_admin_card_text(message: Message, state: FSMContext):
        if message.from_user.id not in ADMIN_IDS:
            await state.clear()
            return await message.answer("Нет доступа")

        card_number = message.text.strip().replace(" ", "")
        if not card_number.isdigit() or len(card_number) < 16:
            return await message.answer("Неверный формат. Введите только цифры номера карты.")

        set_setting("card_number", card_number)
        await state.clear()
        await message.answer("Номер карты обновлен ✅", reply_markup=admin_menu())

    @dp.message(F.text)
    async def handle_text(message: Message):
        await message.answer(
            "Используйте кнопки ниже.",
            reply_markup=user_menu(is_admin=message.from_user.id in ADMIN_IDS),
        )

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(process_scheduler, "interval", minutes=30, args=[bot])
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
