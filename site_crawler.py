"""
site_crawler.py — обход сайта компании: разделы «О компании», «Команда»,
«Контакты», «Пресс-центр», «Блог».

Строит карту ссылок с главной страницы (и, если найдено, из sitemap.xml),
классифицирует найденные страницы по разделам эвристикой по URL/тексту
ссылки, для каждой скачанной страницы отдаёт текст, обнаруженные ФИО с
должностями (по паттернам "Имя Фамилия — Должность" и близким) и контакты
(email, ссылки на Telegram).

Соблюдает robots.txt: страницы, запрещённые для User-agent: *, не
запрашиваются.

Использование:
    python site_crawler.py --url https://wmtgroup.ru/
    python site_crawler.py --url https://wmtgroup.ru/ --sections about,team,contacts,press --max-pages 15
"""

from __future__ import annotations

import argparse
import re
import urllib.robotparser as robotparser
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from common import Cache, Session, SourceUnavailable, emit, envelope, load_config

SECTION_KEYWORDS = {
    "about": ["о компании", "о нас", "about", "company", "миссия"],
    "team": ["команда", "team", "сотрудники", "руководство", "менеджмент"],
    "contacts": ["контакты", "contacts", "связаться", "reach us"],
    "press": ["пресс", "press", "media", "сми", "новости", "news"],
    "blog": ["блог", "blog", "статьи", "articles"],
}

# ФИО (Имя Фамилия / Фамилия Имя Отчество) рядом с должностью — русские
# буквы с заглавной, 2-3 слова, за которыми через тире/запятую/на новой
# строке идёт текст, похожий на должность.
NAME_POSITION_RE = re.compile(
    r"(?P<name>[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})\s*[—\-–,:]\s*"
    r"(?P<position>[А-Яа-яA-Za-z][^\n]{3,80})"
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
TG_RE = re.compile(r"https?://t\.me/[A-Za-z0-9_]+")


def _robots_allows(base_url: str, path_url: str, user_agent: str) -> bool:
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(user_agent, path_url)
    except Exception:
        # robots.txt недоступен/не парсится — не блокируем обход по умолчанию
        return True


def classify_link(url: str, anchor_text: str) -> Optional[str]:
    haystack = f"{url} {anchor_text}".lower()
    for section, keywords in SECTION_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return section
    return None


def extract_people(text: str) -> list[dict[str, str]]:
    people = []
    seen = set()
    for m in NAME_POSITION_RE.finditer(text):
        name = m.group("name").strip()
        position = m.group("position").strip()
        key = (name, position)
        if key in seen or len(position.split()) > 12:
            continue
        seen.add(key)
        people.append({"name": name, "position": position})
    return people


def extract_contacts(text: str, html: str) -> list[str]:
    contacts = set(EMAIL_RE.findall(text))
    contacts.update(TG_RE.findall(html))
    return sorted(contacts)


def crawl(session: Session, cache: Cache, base_url: str, wanted_sections: list[str], max_pages: int, user_agent: str):
    warnings: list[str] = []
    pages: list[dict] = []
    all_people: list[dict] = []
    all_contacts: set[str] = set()

    if not _robots_allows(base_url, base_url, user_agent):
        return [], [], [], ["robots.txt запрещает обход главной страницы"]

    try:
        resp = session.get(base_url)
    except SourceUnavailable as e:
        return [], [], [], [f"главная страница недоступна: {e}"]

    cache.put_raw(base_url, resp.text)
    soup = BeautifulSoup(resp.text, "lxml")

    base_path = urlparse(base_url)._replace(fragment="", query="").geturl().rstrip("/")

    candidates: list[tuple[str, str]] = []  # (url, section)
    for a in soup.find_all("a", href=True):
        link = urljoin(base_url, a["href"])
        parsed_link = urlparse(link)
        if parsed_link.netloc != urlparse(base_url).netloc:
            continue
        # анкоры вида #about на той же странице (SPA/одностраничник) — не
        # отдельная страница, пропускаем, чтобы не дублировать контент
        link_path = parsed_link._replace(fragment="", query="").geturl().rstrip("/")
        if link_path == base_path:
            continue
        section = classify_link(link, a.get_text(" ", strip=True))
        if section and (not wanted_sections or section in wanted_sections):
            candidates.append((link, section))

    # главная страница сама может нести описание/контакты
    main_text = soup.get_text(" ", strip=True)
    pages.append({"url": base_url, "section": "about", "title": (soup.title.string if soup.title else ""), "text": main_text[:4000]})
    all_people.extend(extract_people(main_text))
    all_contacts.update(extract_contacts(main_text, resp.text))

    visited = {base_url}
    seen_sections = set()
    for link, section in candidates:
        if len(pages) >= max_pages:
            warnings.append(f"достигнут лимит --max-pages ({max_pages}), часть разделов не обойдена")
            break
        if link in visited or section in seen_sections:
            continue
        visited.add(link)
        if not _robots_allows(base_url, link, user_agent):
            warnings.append(f"{link}: запрещено robots.txt, пропущено")
            continue
        try:
            page_resp = session.get(link)
        except SourceUnavailable as e:
            warnings.append(f"{link}: недоступна ({e})")
            continue
        cache.put_raw(link, page_resp.text)
        page_soup = BeautifulSoup(page_resp.text, "lxml")
        text = page_soup.get_text(" ", strip=True)
        pages.append(
            {
                "url": link,
                "section": section,
                "title": page_soup.title.string if page_soup.title else "",
                "text": text[:4000],
            }
        )
        all_people.extend(extract_people(text))
        all_contacts.update(extract_contacts(text, page_resp.text))
        seen_sections.add(section)

    for section in wanted_sections or []:
        if not any(p["section"] == section for p in pages):
            warnings.append(f"раздел «{section}» не найден на сайте")

    # dedupe people by (name, position)
    dedup_people = []
    seen_keys = set()
    for p in all_people:
        key = (p["name"], p["position"])
        if key not in seen_keys:
            seen_keys.add(key)
            p["source_url"] = base_url
            dedup_people.append(p)

    return pages, dedup_people, sorted(all_contacts), warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="crawl company website for key sections")
    parser.add_argument("--url", required=True)
    parser.add_argument("--sections", default="about,team,contacts,press", help="comma-separated list")
    parser.add_argument("--max-pages", type=int, default=15)
    args = parser.parse_args()

    cfg = load_config()
    session = Session(cfg)
    cache = Cache()
    wanted = [s.strip() for s in args.sections.split(",") if s.strip()]

    pages, people, contacts, warnings = crawl(
        session, cache, args.url, wanted, args.max_pages, cfg["http"]["user_agent"]
    )

    ok = len(pages) > 0
    data = {"pages": pages, "people": people, "contacts": contacts}
    if not ok:
        emit(
            envelope(
                ok=False,
                source="site_crawler",
                method="html",
                url=args.url,
                warnings=warnings,
                error={"type": "source_unavailable", "message": "не удалось получить ни одной страницы"},
            )
        )
        return

    emit(envelope(ok=True, source="site_crawler", method="html", url=args.url, data=data, warnings=warnings))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        emit(envelope(ok=False, source="site_crawler", method="html", error={"type": "unexpected_error", "message": str(e)}))
