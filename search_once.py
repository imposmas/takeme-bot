"""Разовый прогон: логин на HH (если нет сессии) → поиск по двум профилям →
в БД → карточки в Telegram.

Тестовый сценарий, не боевой цикл. Лимиты намеренно маленькие, чтобы не долбить
площадку и не спамить себе в чат.

Запуск:  python search_once.py
"""
import asyncio

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select

from config import (
    HH_EXCLUDED_WORDS,
    HH_SEARCH_PROFILES,
    HH_SEARCH_TEXT,
    HH_WORK_SCHEDULE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from db import Application, Platform, Session, Vacancy, init_db
from job_agents.hh_agent import HHAgent

LIMIT_PER_PROFILE = 3  # сколько брать из каждого профиля поиска
SEND_LIMIT = 3         # сколько всего карточек отправить в Telegram за прогон

_CURRENCY = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€",
             "KZT": "₸", "UAH": "₴", "BYR": "Br", "BYN": "Br"}


def _fmt_salary(sfrom: int | None, sto: int | None, currency: str | None) -> str:
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


def _card(row: Vacancy, data: dict) -> str:
    profiles = ", ".join(data.get("profiles") or []) or "—"
    return (
        f"<b>{row.title or '—'}</b>\n"
        f"{row.company or '—'}\n"
        f"{_fmt_salary(row.salary_from, row.salary_to, data.get('salary_currency'))}\n"
        f"{row.url or ''}\n"
        f"<i>{profiles}</i> · <code>#{data['key']}</code>"
    )


async def _already_applied(session, vacancy_id: int) -> bool:
    """Отклик уже есть в нашей БД (таблица applications)."""
    found = await session.execute(
        select(Application.id).where(Application.vacancy_id == vacancy_id).limit(1)
    )
    return found.scalar_one_or_none() is not None


async def main() -> None:
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID не задан в .env")

    await init_db()

    agent = HHAgent()
    if not agent.has_saved_session():
        print("Сессии HH нет — открываю окно для входа…")
        await agent.login()

    print(
        f"Поиск: text={HH_SEARCH_TEXT!r}, профилей={len(HH_SEARCH_PROFILES)}, "
        f"по {LIMIT_PER_PROFILE} из каждого"
    )
    vacancies = await agent.search_profiles(
        HH_SEARCH_PROFILES,
        limit_per_profile=LIMIT_PER_PROFILE,
        text=HH_SEARCH_TEXT,
        excluded_text=HH_EXCLUDED_WORDS,
        work_schedule_by_days=HH_WORK_SCHEDULE,
    )
    print(f"Уникальных вакансий после склейки профилей: {len(vacancies)}")

    bot = Bot(TELEGRAM_BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    sent = 0
    try:
        async with Session() as session:
            hh = (
                await session.execute(select(Platform).where(Platform.name == "hh"))
            ).scalar_one()

            for data in vacancies:
                if sent >= SEND_LIMIT:
                    break

                row = (
                    await session.execute(
                        select(Vacancy).where(
                            Vacancy.platform_id == hh.id,
                            Vacancy.external_id == data["external_id"],
                        )
                    )
                ).scalar_one_or_none()

                if row is None:
                    row = Vacancy(
                        platform_id=hh.id,
                        external_id=data["external_id"],
                        url=data["url"],
                        title=data["title"],
                        company=data["company"],
                        salary_from=data["salary_from"],
                        salary_to=data["salary_to"],
                        raw_description=data["raw_text"],
                        status="new",
                    )
                    session.add(row)
                    await session.flush()

                # «Я уже откликалась на это?» — HH + наша БД.
                applied = bool(data.get("applied_on_hh")) or await _already_applied(
                    session, row.id
                )
                if applied:
                    if row.status != "applied":
                        row.status = "applied"
                    print(f"  {data['key']}: уже откликались — пропуск")
                    continue

                if row.status in ("sent_to_tg", "skipped", "applied"):
                    print(f"  {data['key']}: status={row.status} — повторно не шлю")
                    continue

                await bot.send_message(TELEGRAM_CHAT_ID, _card(row, data))
                row.status = "sent_to_tg"
                sent += 1

            await session.commit()
    finally:
        await bot.session.close()

    print(f"Готово. В Telegram отправлено: {sent}")


if __name__ == "__main__":
    asyncio.run(main())
