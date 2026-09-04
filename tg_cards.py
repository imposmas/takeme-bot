"""Рендер карточки вакансии для Telegram — общий между search_once.py
(отправка) и takemebot.py (перерисовка при апруве письма), чтобы карточка не
расходилась в двух местах.
"""
from __future__ import annotations

from html import escape

from db import Vacancy

_CURRENCY = {
    "RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€",
    "KZT": "₸", "UAH": "₴", "BYR": "Br", "BYN": "Br",
}

_WORK_FORMAT_LABELS = {
    "ON_SITE": "офис",
    "REMOTE": "удалённо",
    "HYBRID": "гибрид",
    "FIELD_WORK": "разъездной",
}

# Коды HH (vacancy.workExperience / raw["workExperience"]).
_EXPERIENCE_LABELS = {
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "6+ лет",
}


def format_salary(sfrom: int | None, sto: int | None, currency: str | None) -> str:
    sign = _CURRENCY.get(currency or "", currency or "₽")

    def money(n: int) -> str:
        return f"{n:,}".replace(",", " ") + f" {sign}"

    if sfrom and sto:
        return f"{money(sfrom)} – {money(sto)}"
    if sfrom:
        return f"от {money(sfrom)}"
    if sto:
        return f"до {money(sto)}"
    return "з/п не указана"


def format_work_format(raw: str | None) -> str:
    if not raw:
        return "—"
    labels = [_WORK_FORMAT_LABELS.get(code, code) for code in raw.split(",") if code]
    return "/".join(labels) if labels else "—"


def format_experience(raw: str | None) -> str:
    if not raw:
        return "—"
    return _EXPERIENCE_LABELS.get(raw, raw)


def render_card(vacancy: Vacancy) -> str:
    """Базовое представление карточки — без статусных приписок (их добавляет
    вызывающая сторона: «⏳ Генерирую письмо…», черновик, финальный результат)."""
    lines = [
        f"🎯 <b>{vacancy.match_score:.0f}/100</b>" if vacancy.match_score is not None else "🎯 —",
        f"<b>{escape(vacancy.title or '—')}</b>",
        f"🏢 {escape(vacancy.company or '—')}",
        f"💰 {format_salary(vacancy.salary_from, vacancy.salary_to, vacancy.salary_currency)}",
        f"🏠 {format_work_format(vacancy.work_format)} · 👔 {format_experience(vacancy.experience)}",
    ]
    if vacancy.url:
        lines.append(f"🔗 {vacancy.url}")
    if vacancy.match_reason:
        lines.append("")
        lines.append(f"<i>{escape(vacancy.match_reason)}</i>")
    return "\n".join(lines)
