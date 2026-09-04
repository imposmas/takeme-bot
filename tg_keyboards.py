"""Инлайн-клавиатуры Telegram-бота — общие между search_once.py (отправка
карточек) и takemebot.py (обработка нажатий), чтобы callback_data не разъехались.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def vacancy_keyboard(vacancy_id: int, *, include_skip: bool = True) -> InlineKeyboardMarkup:
    """vacancy_id — id строки в нашей таблице vacancies (не id на площадке).

    include_skip=False — вариант для уже пропущенной вакансии: «Пропустить»
    больше не нужен (уже пропущена), но ✅/✏️ остаются — пропуск не финал,
    к вакансии можно вернуться и откликнуться позже. Финал — только реальный
    отклик (там кнопки прячутся полностью, см. takemebot.py)."""
    row = [InlineKeyboardButton(text="✅ Откликнуться", callback_data=f"apply:{vacancy_id}")]
    if include_skip:
        row.append(InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip:{vacancy_id}"))
    row.append(InlineKeyboardButton(text="✏️ Своим текстом", callback_data=f"custom:{vacancy_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def approval_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    """После генерации письма — подтвердить/переписать/отменить, до реального отклика."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data=f"approve:{vacancy_id}"),
                InlineKeyboardButton(text="✏️ Своим текстом", callback_data=f"custom:{vacancy_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel:{vacancy_id}"),
            ]
        ]
    )
