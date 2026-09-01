"""
tenchat_client.py — TenChat (tenchat.ru).

Базовый режим — HTTP + парсинг server-side-rendered разметки, работает для
профилей людей и компаний. Раздел «/connect» (поиск людей) рендерится
клиентским JS (SPA) — доступен только через --headless (Chromium). Перед
запуском браузера скрипт проверяет browser_available(); если браузер
недоступен (бинарники ms-playwright не установлены — известное ограничение
среды, см. SKILL.md §4), возвращает ok:false с error.type:
"browser_unavailable" и НЕ роняет прогон остальных источников.

Использование:
    python tenchat_client.py --profile ivan_petrov
    python tenchat_client.py --company "WMT"
    python tenchat_client.py --company "WMT" --headless
"""

from __future__ import annotations

import argparse
from typing import Any

from bs4 import BeautifulSoup

from common import Cache, Session, SourceUnavailable, browser_available, emit, envelope, load_config

BASE = "https://tenchat.ru"


def fetch_ssr(session: Session, cache: Cache, url: str) -> tuple[dict[str, Any], list[str]]:
    resp = session.get(url)
    cache.put_raw(url, resp.text)
    soup = BeautifulSoup(resp.text, "lxml")
    warnings: list[str] = []

    name_el = soup.select_one("h1") or soup.select_one("meta[property='og:title']")
    name = name_el.get("content") if name_el and name_el.name == "meta" else (name_el.get_text(" ", strip=True) if name_el else None)
    desc_el = soup.select_one("meta[property='og:description']")
    description = desc_el.get("content") if desc_el else None

    if not name:
        warnings.append("name: не найдено — раздел, возможно, требует JS-рендеринга (см. --headless)")
    if not description:
        warnings.append("description: не найдено")

    return {"name": name, "description": description, "url": url}, warnings


def fetch_headless(url: str, wait_selector: str | None = None) -> tuple[str, list[str]]:
    from playwright.sync_api import sync_playwright

    warnings: list[str] = []
    html = ""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, timeout=25000, wait_until="networkidle")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8000)
                except Exception:
                    warnings.append(f"селектор {wait_selector} не появился за 8с — контент мог не догрузиться")
            html = page.content()
        finally:
            browser.close()
    return html, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="TenChat profile/company/people-search")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile")
    group.add_argument("--company")
    parser.add_argument("--headless", action="store_true", help="use Chromium for JS-rendered sections (e.g. /connect people search)")
    args = parser.parse_args()

    cfg = load_config()
    session = Session(cfg)
    cache = Cache()

    if args.profile:
        url = f"{BASE}/{args.profile}"
    else:
        url = f"{BASE}/search?q={args.company}"

    if args.headless:
        if not browser_available():
            emit(
                envelope(
                    ok=False,
                    source="tenchat.ru",
                    method="headless",
                    url=url,
                    error={
                        "type": "browser_unavailable",
                        "message": "Chromium недоступен в этой среде (бинарники ms-playwright не установлены) — раздел, требующий JS, пропущен",
                    },
                )
            )
            return
        try:
            html, warnings = fetch_headless(url)
        except Exception as e:  # noqa: BLE001
            emit(envelope(ok=False, source="tenchat.ru", method="headless", url=url, error={"type": "browser_error", "message": str(e)}))
            return
        cache.put_raw(url, html)
        soup = BeautifulSoup(html, "lxml")
        results = []
        for card in soup.select('[class*="user-card"], [class*="person-card"]'):
            name_el = card.select_one("a")
            if name_el:
                results.append({"name": name_el.get_text(" ", strip=True), "url": BASE + name_el.get("href", "")})
        if not results:
            warnings.append("results: карточки людей не найдены — вёрстка могла измениться, требует проверки селектора")
        emit(envelope(ok=True, source="tenchat.ru", method="headless", url=url, data={"results": results}, warnings=warnings))
        return

    try:
        data, warnings = fetch_ssr(session, cache, url)
    except SourceUnavailable as e:
        emit(envelope(ok=False, source="tenchat.ru", method="ssr", url=url, error={"type": e.error_type, "message": str(e)}))
        return

    emit(envelope(ok=True, source="tenchat.ru", method="ssr", url=url, data=data, warnings=warnings))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        emit(envelope(ok=False, source="tenchat.ru", method="unknown", error={"type": "unexpected_error", "message": str(e)}))
