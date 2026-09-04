"""Telegram-бот: обрабатывает кнопки на карточках вакансий
[✅ Откликнуться] [❌ Пропустить].

По ✅ письмо сначала генерируется и показывается на подтверждение
([✅ Отправить] [✏️ Своим текстом] [❌ Отмена]) — реальный отклик уходит
только после апрува, а не сразу. «Своим текстом» — только на этом этапе:
писать письмо вслепую до черновика смысла нет.

Карточки шлёт search_once.py (позже — планировщик), этот процесс должен быть
запущен параллельно, чтобы ловить нажатия. Отклик — через apply_flow.py,
который не знает про Telegram и просто дёргает нужного JobAgent.
"""
import asyncio
import logging
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

import apply_flow
import llm_cover_letter
from config import TELEGRAM_BOT_TOKEN
from db import Session, Vacancy, init_db
from tg_keyboards import approval_keyboard, vacancy_keyboard

dp = Dispatcher(storage=MemoryStorage())

# vacancy_id -> сгенерированное письмо, ждущее подтверждения ✅/❌/✏️.
# В памяти процесса достаточно — рестарт бота между генерацией и апрувом
# в этом однопользовательском проекте не проблема, просто жмём ✅ заново.
_pending_letters: dict[int, str] = {}


class WaitingCustomText(StatesGroup):
    text = State()


def _vacancy_id(callback_data: str) -> int:
    return int(callback_data.split(":", 1)[1])


async def _get_vacancy(vacancy_id: int) -> Vacancy | None:
    async with Session() as session:
        return await session.get(Vacancy, vacancy_id)


async def _run_apply(status_msg: Message, vacancy: Vacancy, cover_letter: str | None) -> None:
    """Общий хвост apply-флоу: дёрнуть агента и отредактировать статусное сообщение.

    При неудаче возвращаем кнопки [✅/❌/✏️] обратно — иначе карточка
    становится тупиком, откуда больше ничего не сделать."""
    ok, result_message = await apply_flow.apply_to_vacancy(vacancy.id, cover_letter)
    icon = "✅" if ok else "⚠️"
    letter_block = f"\n\n<i>Письмо:</i>\n{escape(cover_letter)}" if cover_letter else ""
    await status_msg.edit_text(
        f"{icon} {escape(result_message)}{letter_block}",
        reply_markup=None if ok else vacancy_keyboard(vacancy.id),
    )


@dp.callback_query(F.data.startswith("skip:"))
async def on_skip(callback: CallbackQuery) -> None:
    """Пропуск — не финал: карточка остаётся живой (можно откликнуться позже),
    прячем только саму кнопку «Пропустить». Полностью кнопки уходят лишь
    после реального отклика (см. _run_apply)."""
    vacancy_id = _vacancy_id(callback.data)
    async with Session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if vacancy is None:
            await callback.answer("Вакансия не найдена в БД", show_alert=True)
            return
        vacancy.status = "skipped"
        await session.commit()

    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n<i>❌ Пропущено</i>",
        reply_markup=vacancy_keyboard(vacancy_id, include_skip=False),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("apply:"))
async def on_apply(callback: CallbackQuery) -> None:
    """✅ на карточке вакансии — генерирует письмо и показывает на подтверждение,
    отклик пока НЕ отправляется."""
    vacancy_id = _vacancy_id(callback.data)
    await callback.answer("Генерирую письмо…")
    await callback.message.edit_reply_markup(reply_markup=None)

    vacancy = await _get_vacancy(vacancy_id)
    if vacancy is None:
        await callback.message.answer("Вакансия не найдена в БД")
        return

    status_msg = await callback.message.answer("⏳ Генерирую сопроводительное письмо…")
    try:
        cover_letter = await llm_cover_letter.generate(
            vacancy.title, vacancy.company, vacancy.raw_description
        )
    except Exception as exc:
        await status_msg.edit_text(
            f"⚠️ Не смог сгенерировать письмо: {escape(str(exc))}",
            reply_markup=vacancy_keyboard(vacancy_id),
        )
        return

    _pending_letters[vacancy_id] = cover_letter
    await status_msg.edit_text(
        f"<i>Черновик письма для «{escape(vacancy.title or '')}»:</i>\n\n{escape(cover_letter)}",
        reply_markup=approval_keyboard(vacancy_id),
    )


@dp.callback_query(F.data.startswith("approve:"))
async def on_approve(callback: CallbackQuery) -> None:
    """✅ Отправить на черновике письма — вот тут отклик реально уходит."""
    vacancy_id = _vacancy_id(callback.data)
    cover_letter = _pending_letters.pop(vacancy_id, None)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    vacancy = await _get_vacancy(vacancy_id)
    if vacancy is None:
        await callback.message.answer("Вакансия не найдена в БД")
        return

    status_msg = await callback.message.answer(f"⏳ Откликаюсь на «{escape(vacancy.title or '')}»…")
    await _run_apply(status_msg, vacancy, cover_letter)


@dp.callback_query(F.data.startswith("cancel:"))
async def on_cancel(callback: CallbackQuery) -> None:
    vacancy_id = _vacancy_id(callback.data)
    _pending_letters.pop(vacancy_id, None)
    await callback.message.edit_text(
        "❌ Отменено, отклик не отправлен. Можно попробовать снова:",
        reply_markup=vacancy_keyboard(vacancy_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("custom:"))
async def on_custom(callback: CallbackQuery, state: FSMContext) -> None:
    vacancy_id = _vacancy_id(callback.data)
    _pending_letters.pop(vacancy_id, None)
    await state.update_data(vacancy_id=vacancy_id)
    await state.set_state(WaitingCustomText.text)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✏️ Пришли текст сопроводительного письма следующим сообщением")
    await callback.answer()


@dp.message(StateFilter(WaitingCustomText.text), F.text)
async def on_custom_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    vacancy_id = data["vacancy_id"]
    await state.clear()

    vacancy = await _get_vacancy(vacancy_id)
    if vacancy is None:
        await message.answer("Вакансия не найдена в БД")
        return

    status_msg = await message.answer(f"⏳ Откликаюсь на «{escape(vacancy.title or '')}»…")
    await _run_apply(status_msg, vacancy, message.text)


@dp.message(F.text)
async def on_text(message: Message) -> None:
    await message.answer("Бот работает. Кнопки на карточках вакансий обрабатываются автоматически.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
