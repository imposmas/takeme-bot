"""Мониторинг статусов откликов на HH — просмотрено/отказ.

Один заход на /applicant/negotiations (с пагинацией) покрывает все отклики
сразу, включая сделанные вручную мимо бота — для них заводим минимальную
запись в vacancies, чтобы дальше тоже отслеживать. В Telegram шлём только
при ПЕРЕХОДЕ в отказ, а не на каждой проверке.

Вызывается из фонового цикла в takemebot.py.
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import Bot
from sqlalchemy import select

from db import Platform, Session, Vacancy
from job_agents.hh_agent import HHAgent

RESPONSE_LABELS = {
    "not-viewed": "не просмотрено",
    "viewed": "просмотрено",
    "discard": "отказ",
}


async def check_and_notify(bot: Bot, chat_id: int) -> int:
    """Возвращает число новых отказов, найденных за этот проход."""
    agent = HHAgent()
    if not agent.has_saved_session():
        return 0

    items = await agent.check_negotiations()
    if not items:
        return 0

    new_rejections = 0
    async with Session() as session:
        hh = (await session.execute(select(Platform).where(Platform.name == "hh"))).scalar_one()

        for item in items:
            row = (
                await session.execute(
                    select(Vacancy).where(
                        Vacancy.platform_id == hh.id,
                        Vacancy.external_id == item["external_id"],
                    )
                )
            ).scalar_one_or_none()

            if row is None:
                # Отклик есть на HH, а у нас про него ничего нет — сделан
                # вручную мимо бота. Заводим минимальную запись, чтобы
                # дальше тоже следить за статусом.
                row = Vacancy(
                    platform_id=hh.id,
                    external_id=item["external_id"],
                    url=item["url"],
                    title=item["title"],
                    company=item["company"],
                    status="applied",
                )
                session.add(row)
                await session.flush()

            was_discard = row.employer_response == "discard"
            row.employer_response = item["response_status"]
            row.employer_response_at = datetime.utcnow()

            if item["response_status"] == "discard" and not was_discard:
                new_rejections += 1
                await bot.send_message(
                    chat_id,
                    f"❌ Отказ по вакансии «{escape(row.title or '')}» — "
                    f"{escape(row.company or '')}\n{row.url}",
                )

        await session.commit()

    return new_rejections
