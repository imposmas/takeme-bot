"""Сверка «зависших» откликов — на случай, если apply() упал с ошибкой уже
ПОСЛЕ реального отклика (см. apply_flow.py: такие случаи теперь сами себя
чинят при следующей попытке, но это разовый прогон для уже накопившихся
до фикса — и подстраховка на будущее, если случится снова).

Берёт все вакансии, где есть хотя бы одна failed-заявка, но сама вакансия
не помечена applied, и живьём проверяет через already_applied(), не ушёл
ли отклик на самом деле. Если да — чинит vacancies.status,
applications.status и пересылает обновлённую карточку в Telegram.

Запуск: python reconcile_applications.py
"""
import asyncio

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select, update

from apply_flow import AGENTS
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from db import Application, Platform, Session, Vacancy, init_db
from tg_cards import render_card


async def main() -> None:
    await init_db()

    async with Session() as session:
        candidates = (
            await session.execute(
                select(Vacancy)
                .join(Application, Application.vacancy_id == Vacancy.id)
                .where(Application.status == "failed", Vacancy.status != "applied")
                .distinct()
            )
        ).scalars().all()

    print(f"Кандидатов на сверку: {len(candidates)}")
    if not candidates:
        return

    bot = Bot(TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    fixed = 0
    try:
        for vacancy in candidates:
            async with Session() as session:
                platform = await session.get(Platform, vacancy.platform_id)
                agent_cls = AGENTS.get(platform.name if platform else "")
                if agent_cls is None:
                    continue
                agent = agent_cls()

                try:
                    really_applied = await agent.already_applied(vacancy.external_id)
                except Exception as exc:
                    print(f"  {vacancy.external_id}: ошибка проверки ({exc}) — пропуск")
                    continue

                if not really_applied:
                    print(f"  {vacancy.external_id}: подтверждено НЕ откликались — ок")
                    continue

                print(f"  {vacancy.external_id}: реально откликались, чиню статус")
                v = await session.get(Vacancy, vacancy.id)
                v.status = "applied"
                await session.execute(
                    update(Application)
                    .where(Application.vacancy_id == v.id, Application.status == "failed")
                    .values(status="applied")
                )
                await session.commit()

                await bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"{render_card(v)}\n\n✅ Отклик подтверждён "
                    f"(была ложная ошибка — на деле отклик прошёл)",
                )
                fixed += 1
            await asyncio.sleep(2)  # не долбим HH подряд
    finally:
        await bot.session.close()

    print(f"Готово. Исправлено: {fixed}")


if __name__ == "__main__":
    asyncio.run(main())
