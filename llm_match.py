"""LLM-матчинг вакансии с резюме — score 0-100 + короткое обоснование.

Модель — MATCH_MODEL (Haiku по умолчанию, см. config.py): задача оценочная,
а не творческая, дорогая модель тут не нужна. Результат кешируется в БД
(vacancies.match_score/match_reason) вызывающей стороной, повторно не считается.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import anthropic

from config import ANTHROPIC_API_KEY, MATCH_MODEL, RESUME_PATH

# Не тащим в LLM вакансию целиком, если там простыня — не нужно и дороже.
_MAX_DESCRIPTION_CHARS = 6000

_TOOL = {
    "name": "submit_match",
    "description": "Вернуть оценку соответствия вакансии резюме.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "0 — совсем не подходит, 100 — идеальное совпадение",
            },
            "reason": {
                "type": "string",
                "description": "1-2 предложения по-русски: почему такая оценка",
            },
        },
        "required": ["score", "reason"],
        "additionalProperties": False,
    },
}

_SYSTEM = (
    "Ты — ассистент по поиску работы. Тебе дают резюме кандидата и текст вакансии. "
    "Оцени, насколько вакансия подходит кандидату, числом от 0 до 100: учитывай стек "
    "технологий, требуемый уровень (junior/middle/senior) и опыт, формат работы, если "
    "он упомянут в тексте. Не занижай оценку за то, что не критично (например, "
    "отсутствие конкретной БД при наличии похожей в резюме). Отвечай только вызовом "
    "инструмента submit_match."
)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY не задан в .env — нужен для LLM-матчинга"
            )
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


@lru_cache(maxsize=1)
def _load_resume() -> str:
    path = Path(RESUME_PATH)
    if not path.exists():
        raise RuntimeError(f"Резюме не найдено: {path} (см. config.RESUME_PATH)")
    return path.read_text(encoding="utf-8")


async def match(
    title: str | None, company: str | None, raw_text: str | None
) -> tuple[int, str]:
    """Сравнивает вакансию с резюме через LLM. Возвращает (score 0-100, reason)."""
    resume = _load_resume()
    description = (raw_text or "")[:_MAX_DESCRIPTION_CHARS]

    vacancy_block = (
        f"Должность: {title or '—'}\n"
        f"Компания: {company or '—'}\n\n"
        f"Текст вакансии:\n{description or '(текст не получен)'}"
    )

    client = _get_client()
    response = await client.messages.create(
        model=MATCH_MODEL,
        max_tokens=500,
        system=_SYSTEM,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "submit_match"},
        messages=[
            {
                "role": "user",
                "content": f"РЕЗЮМЕ:\n{resume}\n\n---\n\nВАКАНСИЯ:\n{vacancy_block}",
            }
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_match":
            score = int(block.input["score"])
            reason = str(block.input["reason"]).strip()
            return max(0, min(100, score)), reason

    raise RuntimeError(f"LLM не вернула submit_match: stop_reason={response.stop_reason}")
