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

import llm_match
from config import (
    ANTHROPIC_API_KEY,
    HH_EXCLUDED_WORDS,
    HH_SEARCH_PROFILES,
    HH_SEARCH_TEXT,
    HH_WORK_SCHEDULE,
    MATCH_THRESHOLD,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from db import Application, Platform, Session, Vacancy, init_db, record_agent_session
from job_agents.hh_agent import HHAgent
from tg_cards import render_card
from tg_keyboards import vacancy_keyboard

LIMIT_PER_PROFILE = 60  # сколько брать из каждого профиля поиска
TOTAL_LIMIT = 120         # добиваем бэклог: уже известные — бесплатно (кеш)
SEND_LIMIT = TOTAL_LIMIT  # сколько всего карточек отправить в Telegram за прогон


async def _already_applied(session, vacancy_id: int) -> bool:
    """Отклик уже есть в нашей БД (таблица applications)."""
    found = await session.execute(
        select(Application.id).where(Application.vacancy_id == vacancy_id).limit(1)
    )
    return found.scalar_one_or_none() is not None


async def main() -> None:
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID не задан в .env")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY не задан в .env — нужен для LLM-матчинга")

    await init_db()

    agent = HHAgent()
    if not agent.has_saved_session():
        print("Сессии HH нет — открываю окно для входа…")
        await agent.login()
    await record_agent_session("hh", agent.storage_state_path)

    async with Session() as session:
        hh_id = (
            await session.execute(select(Platform.id).where(Platform.name == "hh"))
        ).scalar_one()
        known_external_ids = set(
            (
                await session.execute(
                    select(Vacancy.external_id).where(Vacancy.platform_id == hh_id)
                )
            ).scalars()
        )
    print(f"Уже в БД (текст переиспользуем, повторно не открываем): {len(known_external_ids)}")

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
        known_external_ids=known_external_ids,
    )
    print(f"Уникальных вакансий после склейки профилей: {len(vacancies)}")
    vacancies = vacancies[:TOTAL_LIMIT]
    print(f"Краш-тест: обрабатываю первые {len(vacancies)}")

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
                        salary_currency=data.get("salary_currency"),
                        work_format=data.get("work_format"),
                        experience=data.get("experience"),
                        raw_description=data["raw_text"],
                        status="new",
                    )
                    session.add(row)
                    await session.flush()
                else:
                    # Бэкфилл для строк, заведённых до появления этих колонок —
                    # эти поля есть уже на странице выдачи, дополнительный
                    # поход на HH для них не нужен.
                    if row.salary_currency is None and data.get("salary_currency"):
                        row.salary_currency = data["salary_currency"]
                    if row.work_format is None and data.get("work_format"):
                        row.work_format = data["work_format"]
                    if row.experience is None and data.get("experience"):
                        row.experience = data["experience"]

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

                if row.match_score is None:
                    try:
                        score, reason = await llm_match.match(
                            row.title, row.company, row.raw_description
                        )
                        row.match_score = score
                        row.match_reason = reason
                        await session.flush()
                        print(f"  {data['key']}: match_score={score} — {reason}")
                    except Exception as exc:
                        print(f"  {data['key']}: ошибка LLM-матчинга ({exc}) — шлю без фильтра")

                if row.match_score is not None and row.match_score < MATCH_THRESHOLD:
                    print(
                        f"  {data['key']}: match_score={row.match_score} < "
                        f"{MATCH_THRESHOLD} — не шлю"
                    )
                    continue

                await bot.send_message(
                    TELEGRAM_CHAT_ID, render_card(row),
                    reply_markup=vacancy_keyboard(row.id),
                )
                row.status = "sent_to_tg"
                sent += 1

            await session.commit()
    finally:
        await bot.session.close()

    print(f"Готово. В Telegram отправлено: {sent}")


if __name__ == "__main__":
    asyncio.run(main())
