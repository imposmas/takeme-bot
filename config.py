"""Единая точка загрузки конфига из .env."""
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} не задан в .env")
    return value


TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Логин на HH — по номеру телефона и коду, руками в окне браузера.
# Логин/пароль в .env не держим.

# --- параметры поиска вакансий на HH ---

HH_SEARCH_TEXT = os.getenv("HH_SEARCH_TEXT", "Java")

# area id на HH: 113 = вся Россия, 66 = Нижний Новгород (город), 1 = Москва, 2 = СПб
HH_AREA_RUSSIA = "113"
HH_AREA_NN = "66"

# Слова-исключения (HH excluded_text): вакансия с ними в тексте не попадёт в выдачу.
HH_EXCLUDED_WORDS = ["Senior", "Старший", "Руководитель", "Ведущий"]

# График 5/2 (HH work_schedule_by_days) — общий для обоих профилей.
HH_WORK_SCHEDULE = ["FIVE_ON_TWO_OFF"]

# Два профиля поиска. Оба прогоняются за один запуск, результат объединяется
# по id вакансии (в карточке видно, какой профиль её поймал).
HH_SEARCH_PROFILES = [
    {
        "name": "РФ · удалёнка",
        "area": HH_AREA_RUSSIA,
        "work_format": ["REMOTE"],
    },
    {
        "name": "Нижний Новгород · удалёнка + гибрид",
        "area": HH_AREA_NN,
        "work_format": ["REMOTE", "HYBRID"],
    },
]

# Путь к файлу БД и папке с сохранёнными сессиями Playwright.
DB_PATH = os.getenv("DB_PATH", "takeme.db")
DB_URL = f"sqlite+aiosqlite:///{DB_PATH}"
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")
