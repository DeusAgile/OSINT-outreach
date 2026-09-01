"""
hh_client.py — данные о работодателе с hh.ru.

Приоритет: api.hh.ru (если есть hh.api_token в конфиге и/или --prefer-api) →
при 403/отсутствии токена молча переходит на парсинг HTML по [data-qa]
атрибутам. api.hh.ru известен блокировкой по IP дата-центра (ddos-guard,
bad_user_agent: blacklisted) даже с браузерным User-Agent — это ожидаемо,
не повод падать, а повод переключиться на fallback.

Использование:
    python hh_client.py --employer-id 4334427
    python hh_client.py --url https://hh.ru/employer/4334427
    python hh_client.py --search "WMT"
    python hh_client.py --employer-id 4334427 --with-vacancies --vacancies-limit 50
    python hh_client.py --employer-id 4334427 --prefer-api

Результат — envelope из common.py в stdout. Каждое поле, которое не удалось
извлечь, попадает в warnings, а не превращается в отказ всего запроса —
кроме случая, когда компания вообще не найдена.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common import Cache, Session, SourceUnavailable, emit, envelope, fail, load_config

API_BASE = "https://api.hh.ru"
SITE_BASE = "https://hh.ru"

# Селекторы для HTML-режима, проверены на реальной странице (см. SKILL.md §6.1)
SELECTORS = {
    "name": ['[data-qa="company-header-title-name"]', "h1"],
    "site": ['[data-qa="sidebar-company-site"]'],
    "industries": ['[data-qa="company-info-industries"]'],
    "region": ['[data-qa="company-info-address"]'],
    "accredited_it": ['[data-qa="advantage-accredited-it-employer"]'],
    "description": ['[data-qa="company-description"]'],
}


def _first_match_text(soup: BeautifulSoup, selectors: list[str]) -> Optional[str]:
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if text:
                return text
    return None


def _keyword_fallback(soup: BeautifulSoup, keywords: list[str]) -> Optional[str]:
    """Требование п.3.2 ТЗ: у каждого поля обязан быть текстовый fallback —
    поиск по ключевому слову рядом с искомым значением, на случай если
    data-qa атрибут переименован/удалён.

    Строка с ключевым словом часто оказывается просто заголовком блока
    ("О компании") — в этом случае берём следующую непустую строку как
    вероятное значение, а не сам заголовок."""
    lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n")]
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(kw in low for kw in keywords):
            if len(line) <= 20 and idx + 1 < len(lines) and lines[idx + 1]:
                return lines[idx + 1]
            return line
    return None


def parse_employer_html(html: str, url: str) -> tuple[dict[str, Any], list[str]]:
    soup = BeautifulSoup(html, "lxml")
    warnings: list[str] = []
    data: dict[str, Any] = {}

    field_keywords = {
        "name": ["название"],
        "site": ["сайт компании", "веб-сайт"],
        "industries": ["отрасл"],
        "region": ["адрес", "регион"],
        "accredited_it": ["аккредитац"],
        "description": ["о компании", "описание"],
    }

    for field, selectors in SELECTORS.items():
        value = _first_match_text(soup, selectors)
        if value is None:
            value = _keyword_fallback(soup, field_keywords.get(field, []))
        if value is None:
            warnings.append(f"{field}: не найдено ни по data-qa, ни по ключевому слову")
        data[field] = value

    # industries/description иногда содержат список — разбиваем по запятым/переносам
    if data.get("industries"):
        data["industries"] = [s.strip() for s in re.split(r"[,\n]", data["industries"]) if s.strip()]
    else:
        data["industries"] = []

    # site может быть относительной ссылкой-редиректом hh.ru — нормализуем
    site_el = soup.select_one('[data-qa="sidebar-company-site"]')
    if site_el and site_el.get("href"):
        data["site"] = urljoin(url, site_el["href"])

    open_vacancies_el = soup.select_one('[data-qa="employer-page-tabs-desktop-go-VACANCIES"]') or soup.select_one(
        '[data-qa*="vacancies-count"]'
    )
    if open_vacancies_el:
        m = re.search(r"\d+", open_vacancies_el.get_text())
        data["open_vacancies"] = int(m.group()) if m else None
    else:
        data["open_vacancies"] = None
        warnings.append("open_vacancies: селектор не найден")

    data["accredited_it"] = bool(data.get("accredited_it"))
    data["vacancies"] = []
    return data, warnings


def fetch_employer_html(session: Session, cache: Cache, employer_id: str) -> tuple[dict[str, Any], list[str], str]:
    url = f"{SITE_BASE}/employer/{employer_id}"
    cached = cache.get_raw(url)
    if cached is not None:
        html = cached
    else:
        resp = session.get(url)
        html = resp.text
        cache.put_raw(url, html)
    data, warnings = parse_employer_html(html, url)
    return data, warnings, url


def fetch_employer_api(session: Session, cache: Cache, employer_id: str, token: str) -> dict[str, Any]:
    url = f"{API_BASE}/employers/{employer_id}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = session.get(url, headers=headers)
    if resp.status_code != 200:
        raise SourceUnavailable(f"api.hh.ru → HTTP {resp.status_code}", error_type="blocked")
    payload = resp.json()
    cache.put_json(url, payload)
    return {
        "name": payload.get("name"),
        "site": payload.get("site_url"),
        "industries": [i.get("name") for i in payload.get("industries", [])],
        "region": (payload.get("area") or {}).get("name"),
        "description": payload.get("description"),
        "accredited_it": payload.get("accredited_it_employer", False),
        "open_vacancies": payload.get("open_vacancies"),
        "vacancies": [],
    }


def fetch_vacancies_api(session: Session, employer_id: str, limit: int) -> list[dict[str, Any]]:
    url = f"{API_BASE}/vacancies"
    params = {"employer_id": employer_id, "per_page": min(limit, 100)}
    resp = session.get(url, params=params)
    if resp.status_code != 200:
        raise SourceUnavailable(f"api.hh.ru/vacancies → HTTP {resp.status_code}", error_type="blocked")
    payload = resp.json()
    out = []
    for item in payload.get("items", [])[:limit]:
        out.append(
            {
                "id": item.get("id"),
                "title": item.get("name"),
                "url": item.get("alternate_url"),
                "published_at": item.get("published_at"),
                "contact_person": (item.get("contacts") or {}).get("name"),
            }
        )
    return out


def fetch_vacancy_contact_html(session: Session, vacancy_url: str) -> Optional[str]:
    """Контактное лицо на странице вакансии — если работодатель его указал
    (по опыту это делают не все, поэтому None здесь частый и легитимный
    результат, а не сбой парсинга)."""
    resp = session.get(vacancy_url)
    soup = BeautifulSoup(resp.text, "lxml")
    el = soup.select_one('[data-qa="vacancy-contacts__fio"]') or soup.select_one('[data-qa*="contact"][data-qa*="fio"]')
    if el:
        return el.get_text(" ", strip=True)
    text_lines = soup.get_text("\n", strip=True).split("\n")
    for i, line in enumerate(text_lines):
        if "контактное лицо" in line.lower() and i + 1 < len(text_lines):
            return text_lines[i + 1]
    return None


def fetch_vacancies_html(session: Session, employer_id: str, limit: int, fetch_contacts: bool) -> list[dict[str, Any]]:
    """HTML-fallback без API: список вакансий работодателя через
    hh.ru/search/vacancy?employer_id=... (публичная страница, токен не
    нужен), опционально дозапрашивает контактное лицо с каждой карточки
    вакансии отдельным GET-ом (см. fetch_vacancy_contact_html) — это N+1
    запросов, поэтому `fetch_contacts` включается явно, а не всегда."""
    url = f"{SITE_BASE}/search/vacancy"
    resp = session.get(url, params={"employer_id": employer_id})
    soup = BeautifulSoup(resp.text, "lxml")
    out = []
    for el in soup.select('[data-qa="serp-item__title"]')[:limit]:
        vac_url = el.get("href", "").split("?")[0]
        out.append({"id": None, "title": el.get_text(" ", strip=True), "url": vac_url, "published_at": None, "contact_person": None})
    if fetch_contacts:
        for v in out:
            if not v["url"]:
                continue
            try:
                v["contact_person"] = fetch_vacancy_contact_html(session, v["url"])
            except SourceUnavailable:
                continue
    return out


def fetch_vacancies(
    session: Session, cache: Cache, employer_id: str, limit: int, fetch_contacts: bool = False
) -> tuple[list[dict[str, Any]], str]:
    try:
        return fetch_vacancies_api(session, employer_id, limit), "api"
    except SourceUnavailable:
        pass
    try:
        return fetch_vacancies_html(session, employer_id, limit, fetch_contacts), "html"
    except SourceUnavailable:
        return [], "html"


def search_employer_api(session: Session, query: str, token: str) -> list[dict[str, Any]]:
    url = f"{API_BASE}/employers"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = session.get(url, params={"text": query, "per_page": 10}, headers=headers)
    if resp.status_code != 200:
        raise SourceUnavailable(f"api.hh.ru/employers search → HTTP {resp.status_code}", error_type="blocked")
    payload = resp.json()
    return [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "url": item.get("alternate_url"),
            "match_type": _name_matches_query(query, item.get("name", "")) or "api_relevance",
        }
        for item in payload.get("items", [])
    ]


def _name_matches_query(query: str, candidate_name: str) -> Optional[str]:
    """Возвращает "exact" (точное совпадение без учёта регистра),
    "partial" (запрос — подстрока названия или наоборот, например
    «РЕОН» ⊂ «Реон-Техно») или None (никакой связи — кандидат из шума
    полнотекстового поиска по вакансиям, не по названию компании)."""
    q = query.strip().lower()
    n = candidate_name.strip().lower()
    if not q or not n:
        return None
    if q == n:
        return "exact"
    if q in n or n in q:
        return "partial"
    return None


def search_employer_html(session: Session, query: str) -> list[dict[str, Any]]:
    """Fallback без токена/при недействительном токене: api.hh.ru/employers
    требует авторизации даже для поиска (без токена или при 403/401 —
    отказ), а прямой HTML-поиск работодателей по тексту у hh.ru отсутствует
    как отдельная страница. Обходной путь: поиск по вакансиям
    (hh.ru/search/vacancy?text=<запрос>, публичная страница, не требует
    токена) и извлечение работодателей из карточек результатов
    ([data-qa="vacancy-serp__vacancy-employer"] → ссылка вида
    /employer/{id}).

    ВАЖНО (баг, найденный на реальном запросе «РЕОН»): это полнотекстовый
    поиск по ТЕКСТУ ВАКАНСИЙ, а не по названиям работодателей — hh.ru может
    вернуть работодателя, чьё название вообще не пересекается с запросом
    (например, «Правительство Москвы» на запрос «РЕОН», потому что слово
    встретилось где-то в тексте вакансии). Раньше функция отдавала все
    такие карточки без разбора, и «единственный кандидат, похожий на
    название» (Реон-Техно) ошибочно принимался модулем 01 за однозначный
    результат — хотя на рынке есть отдельная, не связанная компания «РЕОН»
    (регион: трубопроводная арматура, reon-armatura.ru), которая просто не
    участвует в этой конкретной выборке вакансий. Поэтому: (1) кандидаты,
    чьё название вообще не пересекается с запросом, отбрасываются здесь же;
    (2) оставшимся проставляется `match_type` — "exact"/"partial" — и
    partial-совпадение (как «Реон-Техно» для запроса «РЕОН») НЕ считается
    модулем 01 автоматически разрешённой неоднозначностью, см.
    modules/01-company-resolve.md.

    Работает только для компаний с открытыми вакансиями на момент запроса
    — это осознанное ограничение fallback-пути, а не баг: у компании без
    активных вакансий надёжного HTML-способа найти employer_id по названию
    нет, и в этом случае функция вернёт пустой список (не исключение) —
    вызывающий код должен добывать сайт/hh.ru через media_search и/или
    родной инструмент веб-поиска (см. modules/01-company-resolve.md)."""
    url = f"{SITE_BASE}/search/vacancy"
    resp = session.get(url, params={"text": query})
    soup = BeautifulSoup(resp.text, "lxml")
    seen: dict[str, dict[str, Any]] = {}
    for el in soup.select('[data-qa="vacancy-serp__vacancy-employer"]'):
        href = el.get("href", "")
        m = re.search(r"/employer/(\d+)", href)
        if not m:
            continue
        name = el.get_text(" ", strip=True)
        match_type = _name_matches_query(query, name)
        if match_type is None:
            continue  # шум полнотекстового поиска — название не связано с запросом
        emp_id = m.group(1)
        if emp_id not in seen:
            seen[emp_id] = {
                "id": emp_id,
                "name": name,
                "url": f"{SITE_BASE}/employer/{emp_id}",
                "match_type": match_type,
            }
    return list(seen.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="hh.ru employer data")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--employer-id")
    group.add_argument("--url")
    group.add_argument("--search")
    parser.add_argument("--with-vacancies", action="store_true")
    parser.add_argument("--vacancies-limit", type=int, default=50)
    parser.add_argument(
        "--fetch-contacts",
        action="store_true",
        help="в HTML-fallback режиме дозапросить контактное лицо с каждой страницы вакансии (N+1 запросов, медленнее)",
    )
    parser.add_argument("--prefer-api", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    session = Session(cfg)
    cache = Cache()

    if args.search:
        search_warnings: list[str] = []
        results: list[dict[str, str]] = []
        method = "api_search"
        token = cfg["hh"].get("api_token", "")
        if token:
            try:
                results = search_employer_api(session, args.search, token)
            except SourceUnavailable as e:
                search_warnings.append(
                    f"api.hh.ru/employers недоступен ({e}) — токен отсутствует, недействителен или "
                    "заблокирован IP дата-центра; переход на HTML-fallback через поиск вакансий"
                )
        else:
            search_warnings.append("hh.api_token не задан в конфиге — сразу переход на HTML-fallback")

        if not results:
            method = "html_vacancy_search"
            try:
                results = search_employer_html(session, args.search)
            except SourceUnavailable as e:
                emit(
                    envelope(
                        ok=False,
                        source="hh.ru",
                        method=method,
                        warnings=search_warnings,
                        error={"type": e.error_type, "message": str(e)},
                    )
                )
                return
            if not results:
                search_warnings.append(
                    "HTML-fallback не нашёл работодателя: он ищет только среди компаний с "
                    "открытыми вакансиями на hh.ru прямо сейчас — если у компании сейчас нет "
                    "активных вакансий, employer_id этим путём не найти, используйте "
                    "scripts/media_search.py как альтернативный сигнал (см. modules/01-company-resolve.md)"
                )

        emit(
            envelope(
                ok=len(results) > 0,
                source="hh.ru",
                method=method,
                data={"results": results},
                warnings=search_warnings,
                error=None if results else {"type": "not_found", "message": f"«{args.search}» не найден ни через API, ни через HTML-fallback"},
            )
        )
        return

    employer_id = args.employer_id
    if args.url:
        m = re.search(r"/employer/(\d+)", args.url)
        if not m:
            fail(f"Не удалось извлечь employer_id из URL: {args.url}", code=3)
        employer_id = m.group(1)

    prefer_api = args.prefer_api or cfg["hh"].get("prefer_api", True)
    token = cfg["hh"].get("api_token", "")
    method = "html_data_qa"
    warnings: list[str] = []
    used_url = f"{SITE_BASE}/employer/{employer_id}"

    data: Optional[dict[str, Any]] = None
    if prefer_api and token:
        try:
            data = fetch_employer_api(session, cache, employer_id, token)
            method = "api"
            used_url = f"{API_BASE}/employers/{employer_id}"
        except SourceUnavailable as e:
            warnings.append(f"api.hh.ru недоступен ({e}), переход на HTML-парсинг")

    if data is None:
        try:
            data, html_warnings, used_url = fetch_employer_html(session, cache, employer_id)
            warnings.extend(html_warnings)
        except SourceUnavailable as e:
            emit(
                envelope(
                    ok=False,
                    source="hh.ru",
                    method=method,
                    url=used_url,
                    warnings=warnings,
                    error={"type": e.error_type, "message": str(e)},
                )
            )
            return

    if args.with_vacancies:
        data["vacancies"], vac_method = fetch_vacancies(
            session, cache, employer_id, args.vacancies_limit, fetch_contacts=args.fetch_contacts
        )
        if not data["vacancies"]:
            warnings.append("vacancies: не найдены (нет открытых вакансий или источник недоступен)")
        elif vac_method == "html":
            warnings.append(
                "vacancies: получены через HTML-fallback (api.hh.ru недоступен) — "
                "у поля id значение null, дат публикации нет"
            )
        if args.with_vacancies and not args.fetch_contacts:
            warnings.append(
                "vacancies: contact_person не запрашивался (нужен --fetch-contacts) — "
                "и даже с флагом контактное лицо на hh.ru указывает не каждый работодатель, "
                "пустое значение может означать «не заполнено», а не сбой парсинга"
            )

    emit(
        envelope(
            ok=True,
            source="hh.ru",
            method=method,
            url=used_url,
            data=data,
            warnings=warnings,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except SourceUnavailable as e:
        emit(envelope(ok=False, source="hh.ru", method="unknown", error={"type": e.error_type, "message": str(e)}))
    except Exception as e:  # noqa: BLE001 — источник никогда не должен падать молча
        emit(envelope(ok=False, source="hh.ru", method="unknown", error={"type": "unexpected_error", "message": str(e)}))
