"""Общий флоу отклика — вызывается из бота по кнопке ✅/✏️.

Не знает о Telegram: принимает id вакансии из нашей БД и текст письма,
диспетчит на нужного JobAgent по имени площадки, пишет результат в БД
(vacancies.status + applications). Площадки без реализации apply() просто
возвращают понятное сообщение вместо падения с NotImplementedError.
"""
from __future__ import annotations

from db import Application, Platform, Session, Vacancy
from job_agents.hh_agent import HHAgent

AGENTS = {
    "hh": HHAgent,
}


async def apply_to_vacancy(vacancy_id: int, cover_letter: str | None) -> tuple[bool, str]:
    """Возвращает (успех, сообщение для пользователя)."""
    async with Session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if vacancy is None:
            return False, "Вакансия не найдена в БД"

        platform = await session.get(Platform, vacancy.platform_id)
        agent_cls = AGENTS.get(platform.name if platform else "")
        if agent_cls is None:
            return False, f"Отклик для площадки «{platform.name}» ещё не реализован"

        agent = agent_cls()
        try:
            ok = await agent.apply(vacancy.external_id, cover_letter)
        except Exception as exc:
            ok = False
            result_message = f"Ошибка при отклике: {exc}"
        else:
            result_message = "Отклик отправлен" if ok else (
                "Не удалось подтвердить отправку — проверь вручную: " + (vacancy.url or "")
            )

        session.add(
            Application(
                vacancy_id=vacancy.id,
                cover_letter=cover_letter,
                status="applied" if ok else "failed",
            )
        )
        if ok:
            vacancy.status = "applied"
        await session.commit()

    return ok, result_message
