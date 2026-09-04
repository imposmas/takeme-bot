"""Генерация сопроводительного письма под вакансию.

В отличие от матчинга (llm_match.py, Haiku — оценочная задача) здесь важно
качество текста, поэтому модель — Sonnet.
"""
from __future__ import annotations

from pathlib import Path

import anthropic

from config import ANTHROPIC_API_KEY, RESUME_PATH

# Творческая задача, дешёвая модель тут ни к чему — в отличие от матчинга.
COVER_LETTER_MODEL = "claude-sonnet-5"

_MAX_DESCRIPTION_CHARS = 6000

_SYSTEM = (
    "Ты помогаешь кандидату писать сопроводительные письма на вакансии. Пиши "
    "по-русски, по делу, без канцелярита и воды, 4-7 предложений. Отмечай "
    "конкретные пересечения опыта из резюме с требованиями вакансии — не "
    "выдумывай того, чего в резюме нет. Не используй шаблонные открывашки вроде "
    "«Здравствуйте, меня заинтересовала ваша вакансия». Верни только текст "
    "письма, без пояснений от себя."
)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY не задан в .env — нужен для генерации писем"
            )
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _load_resume() -> str:
    path = Path(RESUME_PATH)
    if not path.exists():
        raise RuntimeError(f"Резюме не найдено: {path} (см. config.RESUME_PATH)")
    return path.read_text(encoding="utf-8")


async def generate(title: str | None, company: str | None, raw_text: str | None) -> str:
    """Генерирует текст сопроводительного письма под конкретную вакансию."""
    resume = _load_resume()
    description = (raw_text or "")[:_MAX_DESCRIPTION_CHARS]

    client = _get_client()
    response = await client.messages.create(
        model=COVER_LETTER_MODEL,
        max_tokens=800,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"РЕЗЮМЕ:\n{resume}\n\n---\n\nВАКАНСИЯ\n"
                    f"Должность: {title or '—'}\nКомпания: {company or '—'}\n\n"
                    f"{description or '(текст вакансии не получен)'}"
                ),
            }
        ],
    )

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise RuntimeError(f"LLM не вернула текст письма: stop_reason={response.stop_reason}")
    return text
