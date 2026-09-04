"""Агент HH.ru — через Playwright (публичный API для соискателей закрыт).

Логин полуручной: HH пускает по номеру телефона и коду, плюс ловит ботов
капчей. Поэтому login() открывает окно браузера (WSLg показывает его на
рабочем столе Windows), пользователь входит руками, а скрипт ждёт признак
авторизации и сохраняет сессию в sessions/hh.json.

search() работает headless по сохранённой сессии: парсит встроенный в страницу
JSON-стейт HH (надёжнее, чем цепляться за вёрстку), с фолбэком на селекторы.
search_profiles() гоняет несколько профилей фильтров за один запуск браузера и
склеивает результат по id вакансии. Между открытиями карточек — пауза, чтобы не
долбить площадку.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from html import unescape
from urllib.parse import urlencode

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from job_agents.base import JobAgent

LOGIN_URL = "https://hh.ru/account/login"
SEARCH_URL = "https://hh.ru/search/vacancy"
# Сколько ждём, пока пользователь дожмёт вход руками.
MANUAL_LOGIN_TIMEOUT = 5 * 60

_VACANCY_URL_RE = re.compile(r"/vacancy/(\d+)")


def vacancy_id_from_url(url: str | None) -> str | None:
    """id вакансии HH из адресной строки: .../vacancy/137000631 → '137000631'."""
    match = _VACANCY_URL_RE.search(url or "")
    return match.group(1) if match else None


class HHAgent(JobAgent):
    platform_name = "hh"

    def __init__(self, headless: bool = True) -> None:
        # search/apply ходят headless по сохранённой сессии; login() всегда с окном.
        self.headless = headless
        # Заполняется apply() доп. подробностью, которая не влезает в bool
        # (например: письмо не приложилось при мгновенном отклике).
        self.last_apply_note: str | None = None

    # --- логин --------------------------------------------------------

    async def _is_authorized(self, page: Page) -> bool:
        """Залогиненного HH уводит со страницы входа. Проверяем в отдельной вкладке,
        не трогая ту, где пользователь вводит код."""
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            pass
        return "/account/login" not in page.url

    async def login(self) -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False)  # всегда с окном
            context = await browser.new_context()
            page = await context.new_page()

            check = await context.new_page()
            already = await self._is_authorized(check)
            await check.close()
            if already:
                await context.storage_state(path=self.storage_state_path)
                await browser.close()
                return

            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            print(
                "HH: войди в открытом окне (номер телефона, код, капча). "
                f"Жду до {MANUAL_LOGIN_TIMEOUT // 60} мин…"
            )
            deadline = asyncio.get_event_loop().time() + MANUAL_LOGIN_TIMEOUT
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(3)
                # после успешного входа HH сам уводит вкладку с /account/login
                if "/account/login" in page.url:
                    continue
                check = await context.new_page()
                ok = await self._is_authorized(check)
                await check.close()
                if ok:
                    await context.storage_state(path=self.storage_state_path)
                    print(f"HH: сессия сохранена → {self.storage_state_path}")
                    await browser.close()
                    return

            await browser.close()
            raise TimeoutError("HH: вход не завершён за отведённое время")

    # --- поиск -------------------------------------------------------

    async def search(self, filters: dict) -> list[dict]:
        """Один профиль фильтров (интерфейс JobAgent).

        filters: text, area, work_format[], work_schedule_by_days[],
                 excluded_text[], order_by, limit, known_external_ids
        """
        return await self._run(
            [dict(filters, profile_name=None)],
            int(filters.get("limit", 3)),
            filters.get("known_external_ids"),
        )

    async def search_profiles(
        self,
        profiles: list[dict],
        *,
        limit_per_profile: int = 5,
        text: str = "",
        excluded_text: list[str] | None = None,
        work_schedule_by_days: list[str] | None = None,
        order_by: str = "relevance",
        known_external_ids: set[str] | None = None,
    ) -> list[dict]:
        """Несколько профилей за один запуск браузера. Дедуп по id вакансии ДО
        похода за полным текстом. В каждом результате profiles[] — какие профили
        его поймали.

        known_external_ids — id вакансий, которые уже есть в нашей БД (текст
        туда уже сохранён раньше): для них НЕ ходим на страницу вакансии
        повторно за текстом, просто отдаём raw_text="" — вызывающая сторона
        для уже известных вакансий это поле не читает. Иначе при увеличении
        лимита выдача снова отдаёт старые вакансии и код лезет на их страницы
        заново без всякой пользы — лишняя нагрузка на HH.
        """
        filter_list = [
            {
                "text": text or prof.get("text", ""),
                "area": prof["area"],
                "work_format": prof.get("work_format", []),
                "work_schedule_by_days": work_schedule_by_days or [],
                "excluded_text": excluded_text or [],
                "order_by": order_by,
                "profile_name": prof.get("name", str(prof["area"])),
            }
            for prof in profiles
        ]
        return await self._run(filter_list, limit_per_profile, known_external_ids)

    async def _run(
        self,
        filter_list: list[dict],
        limit_each: int,
        known_external_ids: set[str] | None = None,
    ) -> list[dict]:
        if not self.has_saved_session():
            raise RuntimeError("Нет сессии HH — сначала выполни login()")

        known_external_ids = known_external_ids or set()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context = await browser.new_context(storage_state=self.storage_state_path)
            page = await context.new_page()

            merged: dict[str, dict] = {}
            for filt in filter_list:
                await page.goto(self._search_url(filt, limit_each),
                                wait_until="domcontentloaded")
                for item in await self._serp_items(page, limit_each):
                    cur = merged.setdefault(item["external_id"],
                                            {**item, "profiles": []})
                    name = filt.get("profile_name")
                    if name and name not in cur["profiles"]:
                        cur["profiles"].append(name)
                await self._human_pause(page)

            results = list(merged.values())
            for item in results:
                if item["external_id"] in known_external_ids:
                    continue  # текст уже есть в БД — не ходим на HH второй раз
                item.update(await self._fetch_details(context, item["url"]))
                await self._human_pause(page)

            await browser.close()
        return results

    @staticmethod
    async def _human_pause(page: Page, low_ms: int = 2500, high_ms: int = 6000) -> None:
        """Пауза со случайным разбросом вместо фиксированной — не долбим площадку
        механическим ритмом."""
        await page.wait_for_timeout(random.uniform(low_ms, high_ms))

    @staticmethod
    def _search_url(filt: dict, limit: int) -> str:
        params = [
            ("text", filt.get("text", "")),
            ("area", str(filt.get("area", "113"))),
            ("items_on_page", max(limit, 20)),
            # relevance, а не publication_time: иначе в выдачу лезут свежие
            # вакансии, лишь косвенно попавшие под запрос.
            ("order_by", filt.get("order_by", "relevance")),
        ]
        for work_format in filt.get("work_format") or []:
            params.append(("work_format", work_format))
        for schedule in filt.get("work_schedule_by_days") or []:
            params.append(("work_schedule_by_days", schedule))
        excluded = filt.get("excluded_text") or []
        if excluded:
            params.append(("excluded_text", ", ".join(excluded)))
        return f"{SEARCH_URL}?{urlencode(params)}"

    async def _serp_items(self, page: Page, limit: int) -> list[dict]:
        state = await self._initial_state(page)
        if state is not None:
            return self._items_from_state(state, limit)
        return await self._items_from_dom(page, limit)

    # --- разбор встроенного JSON-стейта HH --------------------------

    @staticmethod
    async def _initial_state(page: Page) -> dict | None:
        try:
            # <template> всегда hidden → ждём именно attached, не visible.
            await page.wait_for_selector(
                "#HH-Lux-InitialState", state="attached", timeout=10000
            )
        except PlaywrightTimeoutError:
            return None
        raw = await page.evaluate(
            """() => {
                const read = (el) => el
                    ? ((el.content && el.content.textContent) || el.textContent || null)
                    : null;
                const byId = read(document.getElementById('HH-Lux-InitialState'));
                if (byId) return byId;
                for (const t of document.querySelectorAll('template')) {
                    const txt = read(t);
                    if (txt && txt.includes('vacancySearchResult')) return txt;
                }
                return null;
            }"""
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    @classmethod
    def _items_from_state(cls, state: dict, limit: int) -> list[dict]:
        result = state.get("vacancySearchResult") or {}
        raw_items = result.get("vacancies")
        if not raw_items:
            raw_items = cls._deep_find_vacancy_list(state) or []
        return [cls._norm_item(it) for it in raw_items[:limit]]

    @staticmethod
    def _deep_find_vacancy_list(obj) -> list | None:
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and (
                "vacancyId" in obj[0] or "@id" in obj[0]
            ):
                return obj
            for x in obj:
                found = HHAgent._deep_find_vacancy_list(x)
                if found:
                    return found
        elif isinstance(obj, dict):
            for value in obj.values():
                found = HHAgent._deep_find_vacancy_list(value)
                if found:
                    return found
        return None

    @staticmethod
    def _norm_item(raw: dict) -> dict:
        vid = raw.get("vacancyId") or raw.get("@id") or raw.get("id")
        company = raw.get("company") or {}
        comp = raw.get("compensation") or raw.get("salary") or {}
        links = raw.get("links") or {}
        url = (
            links.get("desktop")
            or raw.get("alternate_url")
            or f"https://hh.ru/vacancy/{vid}"
        )
        # id берём из адресной строки вакансии, id из JSON — только запасной.
        ext_id = vacancy_id_from_url(url) or str(vid)
        return {
            "external_id": ext_id,
            "key": f"hh-{ext_id}",
            "url": url,
            "title": raw.get("name") or raw.get("title"),
            "company": company.get("visibleName")
            or company.get("name")
            or raw.get("companyVisibleName"),
            "salary_from": comp.get("from"),
            "salary_to": comp.get("to"),
            "salary_currency": comp.get("currencyCode"),  # RUR / USD / EUR / …
            "work_format": ",".join(HHAgent._extract_work_formats(raw)),
            "experience": raw.get("workExperience"),
            "raw_text": "",
            "applied_on_hh": None,  # заполнит _fetch_details
        }

    @staticmethod
    def _extract_work_formats(raw: dict) -> list[str]:
        """workFormats на странице выдачи и на самой вакансии — разной формы:
        [{"workFormatsElement": [...]}] в выдаче, ["REMOTE"] на странице
        вакансии. Приводим к плоскому списку кодов HH (REMOTE/HYBRID/ON_SITE)."""
        formats = raw.get("workFormats") or []
        result: list[str] = []
        for item in formats:
            if isinstance(item, dict):
                result.extend(item.get("workFormatsElement") or [])
            elif isinstance(item, str):
                result.append(item)
        return result

    # --- фолбэк на селекторы, если стейт не достали ----------------

    async def _items_from_dom(self, page: Page, limit: int) -> list[dict]:
        titles = page.locator('a[data-qa="serp-item__title"]')
        if not await titles.count():
            titles = page.locator('a[data-qa="vacancy-serp__vacancy-title"]')

        count = min(await titles.count(), limit)
        items: list[dict] = []
        for i in range(count):
            link = titles.nth(i)
            href = (await link.get_attribute("href")) or ""
            ext_id = vacancy_id_from_url(href) or href
            card = link.locator(
                'xpath=ancestor::div[contains(@data-qa,"vacancy-serp__vacancy")][1]'
            )
            items.append(
                {
                    "external_id": ext_id,
                    "key": f"hh-{ext_id}",
                    "url": href.split("?")[0],
                    "title": (await link.inner_text()).strip(),
                    "company": await self._safe_text(
                        card, '[data-qa="vacancy-serp__vacancy-employer"]'
                    ),
                    "salary_from": None,
                    "salary_to": None,
                    "salary_currency": None,
                    "work_format": "",
                    "experience": None,
                    "raw_text": "",
                    "applied_on_hh": None,
                }
            )
        return items

    @staticmethod
    async def _safe_text(scope, selector: str) -> str | None:
        loc = scope.locator(selector)
        if await loc.count():
            return (await loc.first.inner_text()).strip()
        return None

    # --- страница вакансии: полный текст + «я уже откликался?» ------

    async def _fetch_details(self, context: BrowserContext, url: str) -> dict:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            state = await self._initial_state(page)
            raw_text = ""
            applied = None
            if state:
                html = (state.get("vacancyView") or {}).get("description")
                if html:
                    raw_text = self._strip_html(html)
                applied = self._applied_from_state(state, vacancy_id_from_url(url))
            if not raw_text:
                loc = page.locator('[data-qa="vacancy-description"]')
                if await loc.count():
                    raw_text = (await loc.first.inner_text()).strip()
            return {"raw_text": raw_text, "applied_on_hh": applied}
        finally:
            await page.close()

    @staticmethod
    def _applied_from_state(state: dict, vid: str | None) -> bool | None:
        """HH хранит статусы откликов в applicantVacancyResponseStatuses.
        Есть переговоры (negotiations) → на вакансию уже откликались."""
        if not vid:
            return None
        entry = (state.get("applicantVacancyResponseStatuses") or {}).get(str(vid))
        if not entry:
            return None
        negotiations = entry.get("negotiations") or {}
        return bool(negotiations.get("total")) or bool(negotiations.get("topicList"))

    @staticmethod
    def _strip_html(html: str) -> str:
        text = re.sub(r"<(br|/p|/li|/div)>", "\n", html, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
        return re.sub(r"[ \t]+", " ", text).strip()

    # --- отклик --------------------------------------------------------

    async def apply(self, vacancy_id: str, cover_letter: str | None) -> bool:
        """Откликается на вакансию HH.

        У HH два разных флоу после клика по кнопке отклика — какой попадётся,
        заранее не знаем:

        A) Форма отклика (подсмотрено вживую, БЕЗ финального сабмита, чтобы не
           отправить настоящий отклик во время разработки): переход на
           /applicant/vacancy_response?vacancyId=… (резюме подставляется само,
           если оно одно) → раскрыть [data-qa=vacancy-response-letter-toggle] →
           заполнить [data-qa=vacancy-response-popup-form-letter-input] → клик
           [data-qa=vacancy-response-submit-popup].
        B) Мгновенный отклик — сам клик по кнопке уже отправляет отклик БЕЗ
           формы. Письмо в этом случае прикладывается отдельно, ПОСЛЕ отклика,
           через чат с работодателем (см. _attach_letter_after_instant_apply,
           флоу проверен вживую). Если что-то не найдётся — не паникуем,
           откликом это не считаем неудачей, просто письмо будет не приложено
           (self.last_apply_note).

        Успех отклика (факт того, что он вообще создан) подтверждаем ОТДЕЛЬНЫМ
        заходом на страницу вакансии и проверкой applicantVacancyResponseStatuses
        (та же логика, что и «уже откликались» в search()) — это надёжнее, чем
        гадать про текст тоста/редиректа.
        """
        if not self.has_saved_session():
            raise RuntimeError("Нет сессии HH — сначала выполни login()")

        self.last_apply_note = None

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context = await browser.new_context(storage_state=self.storage_state_path)
            page = await context.new_page()
            try:
                await page.goto(f"https://hh.ru/vacancy/{vacancy_id}",
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)

                link = page.locator('[data-qa="vacancy-response-link-top"]').first
                if await link.count() == 0:
                    state = await self._initial_state(page)
                    if state and self._applied_from_state(state, vacancy_id):
                        return True  # уже откликались раньше
                    raise RuntimeError("Кнопка отклика не найдена на странице вакансии")

                try:
                    async with page.expect_navigation(
                        wait_until="domcontentloaded", timeout=8000
                    ):
                        await link.click()
                except PlaywrightTimeoutError:
                    pass  # возможно, форма открылась модалкой на этой же странице

                await page.wait_for_timeout(1500)

                submit = page.locator('[data-qa="vacancy-response-submit-popup"]').first
                if await submit.count():
                    # Ветка A — обычная форма отклика.
                    if cover_letter:
                        toggle = page.locator(
                            '[data-qa="vacancy-response-letter-toggle"]'
                        ).first
                        if await toggle.count():
                            await toggle.click()
                            await page.wait_for_timeout(500)
                        textarea = page.locator(
                            '[data-qa="vacancy-response-popup-form-letter-input"]'
                        ).first
                        if await textarea.count():
                            await textarea.fill(cover_letter)
                            await page.wait_for_timeout(300)
                        else:
                            self.last_apply_note = (
                                "поле письма не нашлось на форме — отправлено без письма"
                            )

                    await submit.click()
                    await page.wait_for_timeout(2500)
                else:
                    # Ветка B — похоже на мгновенный отклик: формы нет, значит клик
                    # по верхней кнопке уже отправил отклик как есть.
                    if cover_letter:
                        attached = await self._attach_letter_after_instant_apply(
                            page, vacancy_id, cover_letter
                        )
                        self.last_apply_note = (
                            "мгновенный отклик — письмо приложено через чат"
                            if attached
                            else "мгновенный отклик — письмо НЕ приложено, добавь вручную"
                        )
            finally:
                await browser.close()

        return await self._confirm_applied(vacancy_id)

    async def _attach_letter_after_instant_apply(
        self, page: Page, vacancy_id: str, cover_letter: str
    ) -> bool:
        """Прикладывает письмо к уже созданному (мгновенному) отклику.

        Флоу проверен ВЖИВУЮ и реально отправляет письмо, поэтому вызывать
        только когда отклик уже точно создан:
        /applicant/negotiations → карточка отклика по vacancy_id →
        [data-qa=open_chat] открывает встроенный чат-виджет (iframe
        chatik.hh.ru) → в чате есть системная ссылка
        [data-qa=chatik-chat-message-applicant-action] («Добавить
        сопроводительное») → клик переводит поле ввода в режим письма →
        текст в [data-qa=text-input] внутри этого iframe → Enter отправляет
        (письмо приезжает в тот же «Отклик на вакансию», а не отдельным
        сообщением).

        Любая неудача — тихий False, а не исключение: отклик уже случился,
        оставить его без письма не страшно, просто предупредим пользователя
        через self.last_apply_note.

        Живой прогон показал: элементы на месте, но HH не всегда успевает
        проиндексировать свежий отклик в /applicant/negotiations сразу же —
        поэтому каждый шаг ждёт своего элемента с повторными попытками, а не
        падает с первого захода.
        """
        try:
            item = await self._retry_find(
                lambda: self._negotiation_item(page, vacancy_id),
                reload_url="https://hh.ru/applicant/negotiations",
                page=page,
            )
            if item is None:
                return False

            chat_btn = item.locator('[data-qa="open_chat"]').first
            if await chat_btn.count() == 0:
                return False
            await chat_btn.click()

            chat_frame = await self._retry_find(
                lambda: self._chat_frame(page), page=page, attempts=6, pause_ms=1000
            )
            if chat_frame is None:
                return False

            action = await self._retry_find(
                lambda: self._first_or_none(
                    chat_frame.locator('[data-qa="chatik-chat-message-applicant-action"]')
                ),
                page=page,
                attempts=5,
                pause_ms=800,
            )
            if action is not None:
                await action.click()
                await page.wait_for_timeout(1000)

            text_input = chat_frame.locator('[data-qa="text-input"]').first
            if await text_input.count() == 0:
                return False
            await text_input.click()
            await text_input.fill(cover_letter)
            await page.wait_for_timeout(300)
            await text_input.press("Enter")
            await page.wait_for_timeout(1500)
            return True
        except Exception:
            return False

    @staticmethod
    def _negotiation_item(page: Page, vacancy_id: str):
        loc = page.locator(
            f'[data-qa="negotiations-item"]:has(a[href*="{vacancy_id}"])'
        ).first
        return loc

    @staticmethod
    def _chat_frame(page: Page):
        return next(
            (fr for fr in page.frames if "chatik.hh.ru/chat" in (fr.url or "")), None
        )

    @staticmethod
    async def _first_or_none(locator):
        return locator.first if await locator.count() else None

    @staticmethod
    async def _retry_find(
        finder,
        *,
        page: Page,
        reload_url: str | None = None,
        attempts: int = 4,
        pause_ms: int = 1500,
    ):
        """Повторяет finder() до первого непустого результата.

        finder может вернуть локатор (проверяем .count()) или уже готовый
        объект/None (например, найденный фрейм). reload_url, если задан,
        перезагружает страницу перед каждой попыткой — нужно для списка
        откликов, который может ещё не знать о только что созданном отклике.
        """
        for attempt in range(attempts):
            if reload_url:
                await page.goto(reload_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(pause_ms)
            result = finder()
            if asyncio.iscoroutine(result):
                result = await result
            if result is not None:
                if hasattr(result, "count"):
                    if await result.count():
                        return result
                else:
                    return result
            if not reload_url and attempt < attempts - 1:
                await page.wait_for_timeout(pause_ms)
        return None

    async def already_applied(self, vacancy_id: str) -> bool:
        """Публичная обёртка над _confirm_applied — часть интерфейса JobAgent.
        apply_flow.py зовёт её ПЕРЕД apply(), чтобы не откликнуться повторно,
        если предыдущая попытка на самом деле прошла, а упала уже на
        последующем шаге (например, при попытке приложить письмо)."""
        return await self._confirm_applied(vacancy_id)

    async def _confirm_applied(self, vacancy_id: str) -> bool:
        """Отдельным заходом проверяет, появилась ли вакансия в откликах."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context = await browser.new_context(storage_state=self.storage_state_path)
            page = await context.new_page()
            try:
                await page.goto(f"https://hh.ru/vacancy/{vacancy_id}",
                                wait_until="domcontentloaded")
                state = await self._initial_state(page)
                return bool(state and self._applied_from_state(state, vacancy_id))
            finally:
                await browser.close()


if __name__ == "__main__":
    asyncio.run(HHAgent().login())
