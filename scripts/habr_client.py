"""
habr_client.py — Хабр Карьера (career.habr.com) и Хабр (habr.com).

Режим компании: профиль на Хабр Карьере, вакансии, упомянутые сотрудники.
Режим персоны: профиль, публикации, комментарии, ответы на Q&A.

Использование:
    python habr_client.py --company "WMT"
    python habr_client.py --company-url https://career.habr.com/companies/wmt
    python habr_client.py --person jul_obrazova
"""

from __future__ import annotations

import argparse
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from common import Cache, Session, SourceUnavailable, emit, envelope, load_config

CAREER_BASE = "https://career.habr.com"
HABR_BASE = "https://habr.com"


def search_company(session: Session, name: str) -> str | None:
    url = f"{CAREER_BASE}/companies"
    resp = session.get(url, params={"q": name})
    soup = BeautifulSoup(resp.text, "lxml")
    link = soup.select_one('a[href^="/companies/"]')
    if link:
        return CAREER_BASE + link["href"].split("?")[0]
    return None


def fetch_company(session: Session, cache: Cache, company_url: str) -> tuple[dict[str, Any], list[str]]:
    resp = session.get(company_url)
    cache.put_raw(company_url, resp.text)
    soup = BeautifulSoup(resp.text, "lxml")
    warnings: list[str] = []

    # Чистое название компании — ссылка на саму страницу компании в
    # хедере, БЕЗ класса (у неё нет своего data-qa/class, но это первая
    # такая ссылка на странице). h1 — это заголовок блока "О компании X",
    # а не имя компании (даёт "О компании «РЕД СОФТ»" вместо "РЕД СОФТ" —
    # проверено на реальной странице, найдено при разборе кейса RED SOFT).
    name_el = None
    company_path = urlparse(company_url).path
    for a in soup.find_all("a", href=company_path):
        text = a.get_text(" ", strip=True)
        if text:
            name_el = a
            break
    if name_el is None:
        name_el = soup.select_one("h1")

    description_el = soup.select_one('[class*="company_description"]') or soup.select_one("meta[name='description']")
    description = None
    if description_el is not None:
        description = description_el.get("content") if description_el.name == "meta" else description_el.get_text(" ", strip=True)
    if not description:
        warnings.append("description: не найдено")

    # Контакты — телефон/email/VK в сайдбаре ([data-qa] на этой странице
    # нет вообще, вёрстка на классах .contacts/.contact/.type/.value).
    # НАЙДЕНО НА РЕАЛЬНОМ КЕЙСЕ (RED SOFT): раньше этого блока не было
    # вообще — телефон и email HR-руководителя лежали открытым текстом в
    # HTML, который скрипт и так получал, просто их никто не искал.
    contacts: dict[str, str] = {}
    for c in soup.select(".contacts .contact"):
        type_el = c.select_one(".type")
        value_el = c.select_one(".value")
        if type_el and value_el:
            key = type_el.get_text(" ", strip=True).rstrip(":").strip().lower()
            contacts[key] = value_el.get_text(" ", strip=True)
    if not contacts:
        warnings.append("contacts: раздел «Контакты» не найден — телефон/email компании, если публикуются, здесь")

    # сайт компании — первая внешняя ссылка в сайдбаре (не ведущая на сам habr.com)
    site = None
    sidebar = soup.select_one("aside")
    if sidebar:
        ext_link = sidebar.select_one('a[href^="http"]:not([href*="habr.com"])')
        if ext_link:
            site = ext_link.get_text(" ", strip=True)

    vacancies = []
    for v in soup.select('a[href*="/vacancies/"]')[:30]:
        title = v.get_text(" ", strip=True)
        if title:
            vacancies.append({"title": title, "url": CAREER_BASE + v["href"] if v["href"].startswith("/") else v["href"]})
    if not vacancies:
        warnings.append("vacancies: не найдены (возможно, нет открытых — не всегда признак сбоя, см. страницу вручную)")

    employees = []
    for e in soup.select('a[href^="/"][href*="/"]'):
        # эвристика: ссылки на персональные профили в блоке "сотрудники"
        parent_classes = " ".join(e.parent.get("class", [])) if e.parent else ""
        if "employee" in parent_classes or "team" in parent_classes:
            person_name = e.get_text(" ", strip=True)
            if person_name:
                employees.append({"name": person_name, "profile_url": CAREER_BASE + e["href"] if e["href"].startswith("/") else e["href"]})

    profile = {
        "name": name_el.get_text(" ", strip=True) if name_el else None,
        "description": description,
        "url": company_url,
        "site": site,
        "contacts": contacts,
    }
    if profile["name"] is None:
        warnings.append("company name: не найдено")

    data = {"profile": profile, "vacancies": vacancies, "employees": employees}
    return data, warnings


def fetch_person(session: Session, cache: Cache, username: str) -> tuple[dict[str, Any], list[str]]:
    profile_url = f"{CAREER_BASE}/{username}"
    warnings: list[str] = []

    try:
        resp = session.get(profile_url)
    except SourceUnavailable as e:
        raise

    cache.put_raw(profile_url, resp.text)
    soup = BeautifulSoup(resp.text, "lxml")

    # ВАЖНО (найдено на реальном профиле): страница персоны на
    # career.habr.com — клиентский SPA-шаблон без SSR-контента (0 data-qa
    # атрибутов, содержимое рендерится JS после загрузки). Обычный GET
    # получает почти пустую оболочку — <h1> внутри неё это URL-слаг
    # (например "jul_obrazova"), а не отображаемое имя, и любая ссылка на
    # /companies/ на этой оболочке — это элемент навигации ("Рейтинг" и
    # т.п.), а не текущий работодатель профиля. Раньше `company_el` брал
    # первую такую ссылку и уверенно выдавал её текст за компанию человека
    # — это было тихо неверно, а не просто иногда пусто. Теперь: если
    # похоже, что страница пуста (мало текста, нет data-qa), не гадаем —
    # возвращаем None по всем полям с явным предупреждением, что источник
    # требует JS-рендеринга (см. tenchat_client.py --headless как пример
    # того, как это делается, когда действительно нужно).
    has_data_qa = bool(soup.select("[data-qa]"))
    page_text_len = len(soup.get_text(strip=True))

    if not has_data_qa and page_text_len < 2000:
        warnings.append(
            "профиль на career.habr.com отрендерен клиентским JS — обычный GET получает "
            "пустую SPA-оболочку без данных; поля name/position/company не заполнены, "
            "а не «не найдены» в смысле отсутствия у человека профиля"
        )
        profile = {"name": None, "position": None, "company": None, "url": profile_url}
    else:
        name_el = soup.select_one("h1") or soup.select_one('[class*="user_name"]')
        position_el = soup.select_one('[class*="specialization"]') or soup.select_one('[class*="position"]')
        company_el = soup.select_one('[data-qa*="company"] a[href*="/companies/"]') or soup.select_one(
            '[class*="resume_company"] a[href*="/companies/"]'
        )
        profile = {
            "name": name_el.get_text(" ", strip=True) if name_el else None,
            "position": position_el.get_text(" ", strip=True) if position_el else None,
            "company": company_el.get_text(" ", strip=True) if company_el else None,
            "url": profile_url,
        }
        if not profile["name"]:
            warnings.append("person name: не найдено — возможно, неверный логин или изменилась вёрстка")

    posts = []
    try:
        posts_resp = session.get(f"{HABR_BASE}/ru/users/{username}/publications/articles/")
        posts_soup = BeautifulSoup(posts_resp.text, "lxml")
        for article in posts_soup.select("article")[:20]:
            title_el = article.select_one("a[href*='/articles/']")
            if not title_el:
                continue
            posts.append(
                {
                    "title": title_el.get_text(" ", strip=True),
                    "url": HABR_BASE + title_el["href"] if title_el["href"].startswith("/") else title_el["href"],
                    "date": None,
                    "excerpt": (article.get_text(" ", strip=True))[:300],
                }
            )
    except SourceUnavailable:
        warnings.append("publications: раздел статей на habr.com недоступен")

    if not posts:
        warnings.append("posts: не найдены (может не быть публикаций у этого пользователя)")

    data = {"profile": profile, "posts": posts, "comments": [], "vacancies": []}
    return data, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="Habr Career / Habr data")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--company")
    group.add_argument("--company-url")
    group.add_argument("--person")
    args = parser.parse_args()

    cfg = load_config()
    session = Session(cfg)
    cache = Cache()

    try:
        if args.person:
            data, warnings = fetch_person(session, cache, args.person)
            url = f"{CAREER_BASE}/{args.person}"
        else:
            company_url = args.company_url
            if not company_url:
                company_url = search_company(session, args.company)
                if not company_url:
                    emit(
                        envelope(
                            ok=False,
                            source="career.habr.com",
                            method="html",
                            warnings=[],
                            error={"type": "not_found", "message": f"компания «{args.company}» не найдена на Хабр Карьере"},
                        )
                    )
                    return
            data, warnings = fetch_company(session, cache, company_url)
            url = company_url
    except SourceUnavailable as e:
        emit(envelope(ok=False, source="career.habr.com", method="html", error={"type": e.error_type, "message": str(e)}))
        return

    emit(envelope(ok=True, source="career.habr.com", method="html", url=url, data=data, warnings=warnings))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        emit(envelope(ok=False, source="career.habr.com", method="html", error={"type": "unexpected_error", "message": str(e)}))
