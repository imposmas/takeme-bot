"""Экспортирует текущую БД в dashboard_template.html → dashboard_export.html
(снимок для артефакта «Мониторинг откликов»).

Сам по себе НЕ публикует — только собирает файл. Публикация артефакта
делается из Claude-сессии (Artifact-тул), потому что артефакты — часть
Claude-платформы, а не то, что можно запушить скриптом. Личные данные
(вакансии, письма) — поэтому dashboard_export.html в .gitignore.

Запуск: python export_dashboard.py
"""
import json
import sqlite3

from config import DB_PATH

TEMPLATE_PATH = "dashboard_template.html"
OUTPUT_PATH = "dashboard_export.html"


def export_data() -> dict:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        vacancies = [
            dict(r)
            for r in con.execute(
                """
                SELECT id, title, company, salary_from, salary_to, salary_currency,
                       work_format, experience, match_score, match_reason, status,
                       url, found_at, employer_response, employer_response_at
                FROM vacancies ORDER BY id
                """
            )
        ]
        applications: dict[int, list[dict]] = {}
        for r in con.execute(
            "SELECT vacancy_id, cover_letter, applied_at, status FROM applications"
        ):
            applications.setdefault(r[0], []).append(
                {"cover_letter": r[1], "applied_at": r[2], "status": r[3]}
            )
    finally:
        con.close()
    return {"vacancies": vacancies, "applications": applications}


def main() -> None:
    data = export_data()
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()

    out = template.replace("__DATA_JSON__", data_json)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(out)

    print(f"Готово: {OUTPUT_PATH} ({len(out)} байт, {len(data['vacancies'])} вакансий)")
    print("Дальше — опубликовать через Artifact в Claude-сессии, той же ссылкой (url=).")


if __name__ == "__main__":
    main()
