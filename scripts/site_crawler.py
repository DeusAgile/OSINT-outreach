"""
site_crawler.py — обход сайта компании: разделы «О компании», «Команда»,
«Контакты», «Пресс-центр», «Блог». Также умеет разобрать одну произвольную
страницу-«ростер» вне домена компании (setka.ru, career.habr.com/companies/
.../employees и подобные агрегаторы, где сразу перечислены сотрудники с
должностями) — режим `--single-page`.

Строит карту ссылок с главной страницы, классифицирует найденные страницы
по разделам эвристикой по URL/тексту ссылки, для каждой скачанной страницы
отдаёт текст, обнаруженные ФИО с должностями (два паттерна — "Имя Фамилия —
Должность" и безразделительный "Имя Должность в Компании", см.
ROSTER_NAME_POSITION_RE) и контакты (email, ссылки на Telegram).

Соблюдает robots.txt: страницы, запрещённые для User-agent: *, не
запрашиваются.

Использование:
    python site_crawler.py --url https://wmtgroup.ru/
    python site_crawler.py --url https://wmtgroup.ru/ --sections about,team,contacts,press --max-pages 15
    python site_crawler.py --single-page https://setka.ru/networks/.../members --company "WMT"
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

# Формат «ростеров»/«сеток» (например setka.ru) — без разделителя, имя
# сразу перед должностью, должность заканчивается на "в <Компания>". Имена
# могут быть кириллицей ИЛИ латиницей (Andrew Meinhardt, Aleksandr Zhukov —
# сетки от hh.ru не транслитерируют иностранные написания). Не жадный до
# " в " — без этого якоря должность легко "съедает" всё до конца строки на
# страницах со сплошным текстом без переносов.
ROSTER_NAME_POSITION_RE = re.compile(
    r"(?P<name>[A-ZА-ЯЁ][\wёЁ\-]+\s+[A-ZА-ЯЁ][\wёЁ\-]+)\s+"
    r"(?P<position>[^\n]{3,80}?)\s+в\s+(?P<company>[A-ZА-ЯЁ][^\n]{2,50}?)(?=\s[A-ZА-ЯЁ][\wёЁ\-]+\s+[A-ZА-ЯЁ]|$)"
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
TG_RE = re.compile(r"https?://t\.me/[A-Za-z0-9_]+")


def _fetch_robots_rules(base_url: str, user_agent: str) -> tuple[Optional[robotparser.RobotFileParser], Optional[str]]:
    """Получает и разбирает robots.txt ОДИН раз на домен, возвращает
    готовый парсер (или None, если разрешено без ограничений) + warning.

    НАЙДЕННЫЙ БАГ (реальный кейс WMX/wmx.pro): `RobotFileParser.read()` на
    HTTP 403 к самому robots.txt молча выставляет внутренний
    disallow_all=True — БЕЗ исключения, поэтому старый `except Exception:
    return True` никогда не срабатывал. wmx.pro отдаёт 403 на /robots.txt
    (похоже на общую анти-бот защиту уровня Cloudflare, блокирующую вообще
    все пути одинаково, а не намеренный запрет через robots.txt), из-за
    чего обход сайта был полностью заблокирован — не потому что сайт
    реально запрещает обход, а потому что мы не смогли ДОСТОВЕРНО узнать,
    запрещает он или нет, и ошиблись в сторону «запрещает».

    403/401 на сам robots.txt — это отказ в доступе к файлу, а не
    содержательное «Disallow: /» внутри него; правильнее читать как
    «не удалось проверить», не как «запрещено». Поэтому: делаем
    HTTP-запрос сами, разбираем ответ вручную вместо доверия к внутренней
    логике `RobotFileParser.read()` на кодах ошибок.

    Раньше это делалось заново на КАЖДУЮ проверяемую ссылку — лишний
    HTTP-запрос на каждый линк и то же предупреждение дублировалось
    столько раз, сколько ссылок проверено. Теперь — один запрос на весь
    обход одного сайта, результат передаётся в `_path_allowed()`."""
    import requests

    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        resp = requests.get(robots_url, headers={"User-Agent": user_agent}, timeout=10)
    except requests.exceptions.RequestException:
        return None, "robots.txt недоступен (сетевая ошибка) — обход разрешён по умолчанию, не проверено"

    if resp.status_code in (401, 403):
        return None, f"robots.txt вернул {resp.status_code} (не удалось проверить, не «запрещено») — обход разрешён по умолчанию"
    if resp.status_code >= 400:
        return None, None  # 404 и подобные — файла нет, обход не ограничен

    rp = robotparser.RobotFileParser()
    try:
        rp.parse(resp.text.splitlines())
        return rp, None
    except Exception:
        return None, "robots.txt получен, но не распарсился — обход разрешён по умолчанию"


def _path_allowed(rp: Optional[robotparser.RobotFileParser], user_agent: str, path_url: str) -> bool:
    if rp is None:
        return True  # правила не получены — не блокируем (см. _fetch_robots_rules)
    return rp.can_fetch(user_agent, path_url)


def classify_link(url: str, anchor_text: str) -> Optional[str]:
    haystack = f"{url} {anchor_text}".lower()
    for section, keywords in SECTION_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return section
    return None


def extract_people(text: str, roster_company_filter: Optional[str] = None) -> list[dict[str, str]]:
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

    for m in ROSTER_NAME_POSITION_RE.finditer(text):
        name = m.group("name").strip()
        position = m.group("position").strip()
        company = m.group("company").strip()
        # На страницах-«сетках» (setka.ru и подобные) рядом перечислены не
        # только сотрудники целевой компании, но и контакты пользователя из
        # ДРУГИХ компаний — без фильтра по компании в результат попадут
        # люди, вообще не имеющие отношения к запросу (см. пример на
        # WMT: "Алла Седова Recruitment Team Lead в IT компания вендор" —
        # другая компания, не WMT). Если фильтр не задан, компания не
        # проверяется — тогда фильтрация обязана произойти на следующем
        # шаге (entity_resolve.py по домену/контексту), не молчаливо
        # пропускаться здесь.
        if roster_company_filter and roster_company_filter.lower() not in company.lower():
            continue
        key = (name, position)
        if key in seen or len(position.split()) > 12:
            continue
        seen.add(key)
        people.append({"name": name, "position": f"{position} в {company}"})
    return people


def extract_contacts(text: str, html: str) -> list[str]:
    contacts = set(EMAIL_RE.findall(text))
    contacts.update(TG_RE.findall(html))
    return sorted(contacts)


NOISE_TAGS = ["nav", "header", "footer", "script", "style", "svg", "noscript"]


def clean_page_text(soup: BeautifulSoup) -> str:
    """Текст страницы без nav/header/footer/script/style — эти блоки
    повторяются идентично на каждой странице сайта (меню, подвал с
    соцсетями и т.п.) и на многостраничных сайтах составляют львиную долю
    из 4000-символьного среза, вытесняя содержательный текст. Работает на
    копии дерева, чтобы не портить soup, который ещё используется для
    поиска ссылок на этой же странице."""
    import copy

    clean = copy.copy(soup)
    for tag_name in NOISE_TAGS:
        for tag in clean.find_all(tag_name):
            tag.decompose()
    return clean.get_text(" ", strip=True)


def crawl(
    session: Session,
    cache: Cache,
    base_url: str,
    wanted_sections: list[str],
    max_pages: int,
    user_agent: str,
    max_chars_per_page: int = 1500,
):
    warnings: list[str] = []
    pages: list[dict] = []
    all_people: list[dict] = []
    all_contacts: set[str] = set()

    robots_rp, robots_warning = _fetch_robots_rules(base_url, user_agent)
    if robots_warning:
        warnings.append(robots_warning)
    if not _path_allowed(robots_rp, user_agent, base_url):
        return [], [], [], warnings + ["robots.txt запрещает обход главной страницы"]

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

    # главная страница сама может нести описание/контакты. Экстракция
    # (extract_people/extract_contacts) работает по ПОЛНОМУ очищенному
    # тексту, а не по обрезанному — обрезка ниже влияет только на то,
    # сколько сырого текста уходит в ответ, не на качество извлечения.
    full_main_text = clean_page_text(soup)
    pages.append({"url": base_url, "section": "about", "title": (soup.title.string if soup.title else ""), "text": full_main_text[:max_chars_per_page]})
    all_people.extend(extract_people(full_main_text))
    all_contacts.update(extract_contacts(full_main_text, resp.text))

    visited = {base_url}
    seen_sections = set()
    for link, section in candidates:
        if len(pages) >= max_pages:
            warnings.append(f"достигнут лимит --max-pages ({max_pages}), часть разделов не обойдена")
            break
        if link in visited or section in seen_sections:
            continue
        visited.add(link)
        if not _path_allowed(robots_rp, user_agent, link):
            warnings.append(f"{link}: запрещено robots.txt, пропущено")
            continue
        try:
            page_resp = session.get(link)
        except SourceUnavailable as e:
            warnings.append(f"{link}: недоступна ({e})")
            continue
        cache.put_raw(link, page_resp.text)
        page_soup = BeautifulSoup(page_resp.text, "lxml")
        text = clean_page_text(page_soup)
        pages.append(
            {
                "url": link,
                "section": section,
                "title": page_soup.title.string if page_soup.title else "",
                "text": text[:max_chars_per_page],
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


def fetch_single_page(
    session: Session, cache: Cache, url: str, company_filter: Optional[str], max_chars_per_page: int
) -> tuple[dict, list[str]]:
    """Разбирает ОДНУ произвольную страницу вне домена компании —
    ростер-страницы вроде setka.ru/networks/.../members или
    career.habr.com/companies/.../employees, где сразу перечислены
    сотрудники нескольких компаний вперемешку (не только целевой — см.
    docstring `extract_people`). Без обхода ссылок, без ограничения по
    домену — это не `crawl()`, а точечный разбор одной уже найденной
    (через media_search/web_search) страницы."""
    warnings: list[str] = []
    resp = session.get(url)
    cache.put_raw(url, resp.text)
    soup = BeautifulSoup(resp.text, "lxml")
    text = clean_page_text(soup)
    people = extract_people(text, roster_company_filter=company_filter)
    contacts = extract_contacts(text, resp.text)
    if company_filter and not people:
        warnings.append(
            f"на странице не нашлось записей с «в {company_filter}» — либо целевой компании "
            "здесь нет, либо формат страницы не совпал с известными паттернами (roster/dash)"
        )
    page = {
        "url": url,
        "section": "roster",
        "title": soup.title.string if soup.title else "",
        "text": text[:max_chars_per_page],
    }
    return {"pages": [page], "people": people, "contacts": contacts}, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="crawl company website for key sections")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="обойти сайт компании (несколько страниц, в пределах домена)")
    group.add_argument(
        "--single-page",
        help="разобрать одну произвольную страницу вне домена компании (ростер/сетка/список сотрудников)",
    )
    parser.add_argument("--sections", default="about,team,contacts,press", help="comma-separated list")
    parser.add_argument("--max-pages", type=int, default=15)
    parser.add_argument(
        "--company",
        default=None,
        help="только для --single-page: фильтр по компании для ROSTER_NAME_POSITION_RE (см. docstring extract_people)",
    )
    parser.add_argument(
        "--max-chars-per-page",
        type=int,
        default=1500,
        help=(
            "срез текста страницы в ответе (экономия токенов у вызывающей модели — "
            "не влияет на полноту извлечения ФИО/контактов, та работает по полному "
            "очищенному тексту до среза; полный HTML всегда остаётся в cache/)"
        ),
    )
    args = parser.parse_args()

    cfg = load_config()
    session = Session(cfg)
    cache = Cache()

    if args.single_page:
        try:
            data, warnings = fetch_single_page(session, cache, args.single_page, args.company, args.max_chars_per_page)
        except SourceUnavailable as e:
            emit(
                envelope(
                    ok=False,
                    source="site_crawler",
                    method="single_page",
                    url=args.single_page,
                    error={"type": e.error_type, "message": str(e)},
                )
            )
            return
        emit(envelope(ok=True, source="site_crawler", method="single_page", url=args.single_page, data=data, warnings=warnings))
        return

    wanted = [s.strip() for s in args.sections.split(",") if s.strip()]

    pages, people, contacts, warnings = crawl(
        session, cache, args.url, wanted, args.max_pages, cfg["http"]["user_agent"], args.max_chars_per_page
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
