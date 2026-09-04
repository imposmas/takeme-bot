"""Абстрактный интерфейс агента площадки.

Оркестратор работает только через этот интерфейс и не знает специфики
конкретного сайта. Реализации: hh_agent.py, habr_agent.py, getmatch_agent.py.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

from config import SESSIONS_DIR


class JobAgent(ABC):
    #: совпадает с platforms.name в БД: 'hh' / 'habr_career' / 'getmatch' / 'company_site'
    platform_name: str

    @abstractmethod
    async def login(self) -> None:
        """Авторизуется на площадке и сохраняет сессию (storage_state)."""

    @abstractmethod
    async def search(self, filters: dict) -> list[dict]:
        """Ищет вакансии по фильтрам.

        Возвращает список словарей с общим набором полей:
            external_id  — id вакансии на площадке (str)
            url          — ссылка на вакансию
            title        — должность
            company      — компания
            salary_from  — нижняя граница вилки или None
            salary_to    — верхняя граница вилки или None
            raw_text     — полный текст вакансии для LLM-матчинга
        """

    @abstractmethod
    async def apply(self, vacancy_id: str, cover_letter: str | None) -> bool:
        """Откликается на вакансию. Возвращает True при успехе."""

    async def already_applied(self, vacancy_id: str) -> bool:
        """Проверка «отклик уже реально существует на площадке» — до вызова
        apply(), чтобы не откликнуться повторно (например, после ошибки,
        случившейся уже ПОСЛЕ того, как отклик фактически ушёл — apply()
        кинул исключение на последующем шаге, но сам отклик состоялся).

        Площадка без такой проверки — просто False (тогда apply_flow идёт
        в обычный apply(), ничего не меняется)."""
        return False

    # --- общее для всех площадок ---------------------------------------

    @property
    def storage_state_path(self) -> str:
        """Путь к json с куками Playwright для этой площадки."""
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        return str(Path(SESSIONS_DIR) / f"{self.platform_name}.json")

    def has_saved_session(self) -> bool:
        return Path(self.storage_state_path).exists()
