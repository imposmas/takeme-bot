"""Telegram-бот: обрабатывает кнопки на карточках вакансий
[✅ Откликнуться] [❌ Пропустить].

По ✅ письмо сначала генерируется и показывается на подтверждение
([✅ Отправить] [✏️ Своим текстом] [❌ Отмена]) — реальный отклик уходит
только после апрува, а не сразу. «Своим текстом» — только на этом этапе:
писать письмо вслепую до черновика смысла нет.

Важно: весь этот флоу происходит В ОДНОМ И ТОМ ЖЕ сообщении (карточка
вакансии редактируется на месте — генерация/черновик/результат), а не
плодит отдельные сообщения. Раньше письмо приходило отдельным сообщением,
и было непонятно, к какой вакансии оно относится, приходилось листать
вверх в поисках карточки.

Карточки шлёт search_once.py (позже — планировщик), этот процесс должен быть
запущен параллельно, чтобы ловить нажатия. Отклик — через apply_flow.py,
который не знает про Telegram и просто дёргает нужного JobAgent.

Плюс фоновый цикл (_monitor_loop): раз в RESPONSE_CHECK_INTERVAL_SECONDS
проверяет статусы уже отправленных откликов на HH (просмотрено/отказ,
monitor_responses.py) и шлёт уведомление при новом отказе.
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
import monitor_responses
from config import RESPONSE_CHECK_INTERVAL_SECONDS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from db import Session, Vacancy, init_db
from tg_cards import render_card
from tg_keyboards import approval_keyboard, vacancy_keyboard

log = logging.getLogger(__name__)
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


def _with_status(vacancy: Vacancy, suffix: str) -> str:
    """Карточка вакансии + текущий статус — одно сообщение вместо простыни
    отдельным сообщением, которую потом приходится искать."""
    return f"{render_card(vacancy)}\n\n{suffix}"


async def _finish_apply(
    bot: Bot, chat_id: int, message_id: int, vacancy: Vacancy, cover_letter: str | None
) -> bool:
    """Общий хвост apply-флоу: дёрнуть агента и отредактировать ту же карточку.

    При неудаче возвращаем кнопки [✅/❌] обратно — иначе карточка становится
    тупиком, откуда больше ничего не сделать. Возвращает ok — вызывающая
    сторона (on_custom_text) шлёт отдельный маячок в чат и не должна врать
    об успехе, если тут на самом деле ошибка."""
    ok, result_message = await apply_flow.apply_to_vacancy(vacancy.id, cover_letter)
    icon = "✅" if ok else "⚠️"
    letter_block = f"\n\n<i>Письмо:</i>\n{escape(cover_letter)}" if cover_letter else ""
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=_with_status(vacancy, f"{icon} {escape(result_message)}{letter_block}"),
        reply_markup=None if ok else vacancy_keyboard(vacancy.id),
    )
    return ok


@dp.callback_query(F.data.startswith("skip:"))
async def on_skip(callback: CallbackQuery) -> None:
    """Пропуск — не финал: карточка остаётся живой (можно откликнуться позже),
    прячем только саму кнопку «Пропустить». Полностью кнопки уходят лишь
    после реального отклика (см. _finish_apply)."""
    vacancy_id = _vacancy_id(callback.data)
    async with Session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if vacancy is None:
            await callback.answer("Вакансия не найдена в БД", show_alert=True)
            return
        vacancy.status = "skipped"
        await session.commit()

    await callback.message.edit_text(
        _with_status(vacancy, "❌ Пропущено"),
        reply_markup=vacancy_keyboard(vacancy_id, include_skip=False),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("apply:"))
async def on_apply(callback: CallbackQuery) -> None:
    """✅ на карточке вакансии — генерирует письмо и показывает на подтверждение
    прямо в той же карточке, отклик пока НЕ отправляется."""
    vacancy_id = _vacancy_id(callback.data)

    vacancy = await _get_vacancy(vacancy_id)
    if vacancy is None:
        await callback.answer()
        await callback.message.answer("Вакансия не найдена в БД")
        return

    if vacancy.status == "applied":
        # Карточка могла остаться с кнопками, хотя отклик уже реально
        # состоялся (например, обнаружился как «уже откликались» на HH) —
        # не генерируем письмо впустую, просто приводим карточку в порядок.
        await callback.answer("Уже откликались")
        await callback.message.edit_text(
            _with_status(vacancy, "✅ Отклик уже был отправлен ранее"),
            reply_markup=None,
        )
        return

    await callback.answer("Генерирую письмо…")
    await callback.message.edit_text(
        _with_status(vacancy, "⏳ Генерирую сопроводительное письмо…"),
        reply_markup=None,
    )
    try:
        cover_letter = await llm_cover_letter.generate(
            vacancy.title, vacancy.company, vacancy.raw_description
        )
    except Exception as exc:
        await callback.message.edit_text(
            _with_status(vacancy, f"⚠️ Не смог сгенерировать письмо: {escape(str(exc))}"),
            reply_markup=vacancy_keyboard(vacancy_id),
        )
        return

    _pending_letters[vacancy_id] = cover_letter
    await callback.message.edit_text(
        _with_status(vacancy, f"<i>Черновик письма:</i>\n{escape(cover_letter)}"),
        reply_markup=approval_keyboard(vacancy_id),
    )


@dp.callback_query(F.data.startswith("approve:"))
async def on_approve(callback: CallbackQuery) -> None:
    """✅ Отправить на черновике письма — вот тут отклик реально уходит."""
    vacancy_id = _vacancy_id(callback.data)
    cover_letter = _pending_letters.pop(vacancy_id, None)
    await callback.answer()

    vacancy = await _get_vacancy(vacancy_id)
    if vacancy is None:
        await callback.message.answer("Вакансия не найдена в БД")
        return

    await callback.message.edit_text(_with_status(vacancy, "⏳ Откликаюсь…"), reply_markup=None)
    await _finish_apply(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        vacancy, cover_letter,
    )


@dp.callback_query(F.data.startswith("cancel:"))
async def on_cancel(callback: CallbackQuery) -> None:
    vacancy_id = _vacancy_id(callback.data)
    _pending_letters.pop(vacancy_id, None)

    vacancy = await _get_vacancy(vacancy_id)
    if vacancy is None:
        await callback.message.answer("Вакансия не найдена в БД")
        return

    await callback.message.edit_text(
        _with_status(vacancy, "❌ Отменено, отклик не отправлен. Можно попробовать снова:"),
        reply_markup=vacancy_keyboard(vacancy_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("custom:"))
async def on_custom(callback: CallbackQuery, state: FSMContext) -> None:
    vacancy_id = _vacancy_id(callback.data)
    _pending_letters.pop(vacancy_id, None)
    # Текст письма придёт отдельным сообщением от пользователя — запоминаем,
    # какую карточку тогда редактировать, вместо того чтобы плодить новую.
    await state.update_data(
        vacancy_id=vacancy_id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
    )
    await state.set_state(WaitingCustomText.text)

    vacancy = await _get_vacancy(vacancy_id)
    if vacancy is None:
        await callback.message.answer("Вакансия не найдена в БД")
        return

    await callback.message.edit_text(
        _with_status(vacancy, "✏️ Пришли текст сопроводительного письма следующим сообщением"),
        reply_markup=None,
    )
    await callback.answer()


@dp.message(StateFilter(WaitingCustomText.text), F.text)
async def on_custom_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    vacancy_id, chat_id, message_id = data["vacancy_id"], data["chat_id"], data["message_id"]
    await state.clear()

    vacancy = await _get_vacancy(vacancy_id)
    if vacancy is None:
        await message.answer("Вакансия не найдена в БД")
        return

    await message.bot.edit_message_text(
        chat_id=chat_id, message_id=message_id,
        text=_with_status(vacancy, "⏳ Откликаюсь…"),
        reply_markup=None,
    )
    ok = await _finish_apply(message.bot, chat_id, message_id, vacancy, message.text)
    # Карточка обновилась выше по чату — маячок в текущем месте, чтобы не
    # пришлось её искать. Текст зависит от реального результата — не врём об
    # успехе, если там на самом деле ошибка.
    icon = "✅" if ok else "⚠️"
    await message.answer(f"{icon} Смотри карточку выше ⬆️")


@dp.message(F.text)
async def on_text(message: Message) -> None:
    await message.answer("Бот работает. Кнопки на карточках вакансий обрабатываются автоматически.")


async def _monitor_loop(bot: Bot) -> None:
    """Раз в RESPONSE_CHECK_INTERVAL_SECONDS — проверка статусов откликов на
    HH. Ошибка одного прохода не должна убивать цикл, просто пробуем снова
    на следующем витке."""
    if not TELEGRAM_CHAT_ID:
        return
    while True:
        await asyncio.sleep(RESPONSE_CHECK_INTERVAL_SECONDS)
        try:
            rejections = await monitor_responses.check_and_notify(bot, int(TELEGRAM_CHAT_ID))
            log.info("monitor: проверка откликов, новых отказов: %s", rejections)
        except Exception:
            log.exception("monitor: ошибка проверки откликов")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    asyncio.create_task(_monitor_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
