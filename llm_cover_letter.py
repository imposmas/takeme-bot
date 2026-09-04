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
    "Ты помогаешь кандидату писать сопроводительные письма на вакансии. Это "
    "короткое сообщение рекрутеру, а не мини-эссе и не отчёт обо всём опыте "
    "разом — жёстко ограничься 3-4 короткими предложениями, максимум ~500 "
    "знаков. Возьми только 1-2 самых сильных пересечения резюме с вакансией "
    "(не пытайся упомянуть каждое совпадение и уж тем более — чего в резюме "
    "нет) и разговорным тоном, как будто пишешь человеку, а не заполняешь "
    "анкету. Без канцелярита, без перечислений через запятую на полстроки, "
    "без шаблонных открывашек вроде «Здравствуйте, меня заинтересовала ваша "
    "вакансия» и без «готова обсудить детали» в конце — это и так подразумевается. "
    "Время глаголов бери по датам из резюме: если место работы там уже не "
    "текущее, пиши прошедшим временем («работала», а не «работаю») — не "
    "утверждай, что кандидат сейчас там работает, если это не так. Верни "
    "только текст письма, без пояснений от себя."
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
