import asyncio
import os
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Message

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

router = Router()


class UserFlow(StatesGroup):
    waiting_payment_proof = State()


class AdminFlow(StatesGroup):
    waiting_new_card = State()


current_card = "0000 0000 0000 0000"


def save_payment(user_id: int, plan: str, file_id: str) -> None:
    """Persist payment proof metadata (stub for your database layer)."""
    # Replace with your DB insert/update logic.
    print(f"saved payment: user={user_id} plan={plan} file_id={file_id}")


@router.message(Command("buy"))
async def buy(message: Message, state: FSMContext) -> None:
    """Start purchase flow and wait for a payment proof."""
    parts = message.text.split(maxsplit=1) if message.text else []
    plan = parts[1].strip() if len(parts) > 1 else "base"

    await state.set_state(UserFlow.waiting_payment_proof)
    await state.update_data(plan=plan)

    await message.answer(
        f"Вы выбрали тариф: {plan}. Отправьте фото или документ с подтверждением оплаты."
    )


@router.message(
    UserFlow.waiting_payment_proof,
    F.photo | F.document,
)
async def receive_payment_proof(message: Message, state: FSMContext) -> None:
    """Handle payment proof only when user is in waiting state."""
    data = await state.get_data()
    plan = data.get("plan", "unknown")

    file_id: Optional[str] = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id

    if not file_id:
        await message.answer("Не удалось прочитать файл. Попробуйте ещё раз.")
        return

    save_payment(message.from_user.id, plan, file_id)
    await state.clear()
    await message.answer("Подтверждение получено. Мы проверим оплату и свяжемся с вами.")


@router.message(Command("admin_change_card"))
async def admin_change_card(message: Message, state: FSMContext) -> None:
    """Switch admin to card update mode."""
    await state.set_state(AdminFlow.waiting_new_card)
    await message.answer("Отправьте новый номер карты текстом.")


@router.message(AdminFlow.waiting_new_card, F.text)
async def set_new_card(message: Message, state: FSMContext) -> None:
    """Accept card number only in admin card-update state."""
    global current_card
    current_card = message.text.strip()

    # Replace with DB/settings persistence in production.
    print(f"new card set: {current_card}")

    await state.clear()
    await message.answer("Номер карты обновлён.")


@router.message(F.photo | F.document)
async def payment_proof_without_state(message: Message) -> None:
    await message.answer("Сначала выберите тариф командой /buy <plan>.")


@router.message(F.text)
async def fallback_text(message: Message) -> None:
    await message.answer(f"Текущая карта для оплаты: {current_card}")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN environment variable")

    storage = RedisStorage.from_url(REDIS_URL)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
