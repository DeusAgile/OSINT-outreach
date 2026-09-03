"""
collect_company.py — оркестратор: объединяет то, что раньше было 5-7
отдельных вызовов скриптов (модуль 02 целиком + этап 1 модуля 03) в один
вызов с одним JSON-ответом.

Не новая логика поверх старой — вызывает те же функции, что и
hh_client.py/site_crawler.py/habr_client.py/media_search.py напрямую (импорт,
не subprocess), с тем же кэшем и той же сессией на всех них. Экономия — не
в объёме данных (он тот же), а в количестве round-trip'ов и в том, что
каждый отдельный конверт (`source/method/fetched_at/url/warnings`) больше
не дублируется на каждый вызов.

Дополнительный эффект, не только экономия: разбор найденных
ростер-страниц (setka.ru/networks/.../members, setka.ru/users/<uuid>,
career.habr.com/companies/.../employees) теперь встроен как код, а не как
пункт инструкции, который модель обязана не забыть выполнить. Это прямая
реакция на реальный кейс: на WMX этот шаг был пропущен в исполнении
(инструкция существовала, но её не довели до конца) — здесь его
пропустить нельзя, потому что это не отдельный шаг, а часть одного
вызова.

Использование:
    python collect_company.py --company "Вебпрактик" --site https://webpractik.ru --hh-url https://hh.ru/employer/726289
    python collect_company.py --company "WMT" --hh-url https://hh.ru/employer/4334427 --site https://wmtgroup.ru --max-roster-pages 2

Выход — envelope из common.py с data:
{
  "company_profile": {...},           # из hh_client (блок 1)
  "vacancies": [...],
  "site_pages": [...],                # из site_crawler.crawl (блок 1 + люди)
  "habr_profile": {...} | null,
  "leadership_search_results": [...], # сырые результаты company_leadership для аудита
  "roster_pages_followed": ["url", ...],
  "candidates": [                     # готово для entity_resolve.py --candidates
    {"name", "position", "source", "source_url", "context"}
  ],
  "contacts": [...]                   # email/tg, собранные из всех источников
}
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hh_client  # noqa: E402
import site_crawler  # noqa: E402
import habr_client  # noqa: E402
import media_search  # noqa: E402
from common import Cache, Session, SourceUnavailable, emit, envelope, load_config  # noqa: E402

ROSTER_URL_MARKERS = (
    "setka.ru/networks/",
    "setka.ru/users/",
    "career.habr.com/companies/",  # проверяется дополнительно на "employees" в пути ниже
)


def is_roster_url(url: str) -> bool:
    if "setka.ru/networks/" in url or "setka.ru/users/" in url:
        return True
    if "career.habr.com/companies/" in url and "employ" in url:
        return True
    return False


def collect(
    company_name: str,
    site: str | None,
    hh_url: str | None,
    session: Session,
    cache: Cache,
    cfg: dict[str, Any],
    max_roster_pages: int,
    fetch_vacancy_contacts: bool,
    site_alternates: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    result: dict[str, Any] = {
        "company_profile": None,
        "vacancies": [],
        "site_pages": [],
        "habr_profile": None,
        "leadership_search_results": [],
        "roster_pages_followed": [],
        "candidates": [],
        "contacts": [],
    }

    # --- hh.ru ---
    if hh_url:
        employer_id = None
        import re

        m = re.search(r"/employer/(\d+)", hh_url)
        if m:
            employer_id = m.group(1)
        if employer_id:
            token = cfg["hh"].get("api_token", "")
            data = None
            if cfg["hh"].get("prefer_api", True) and token:
                try:
                    data = hh_client.fetch_employer_api(session, cache, employer_id, token)
                except SourceUnavailable as e:
                    warnings.append(f"hh.ru API недоступен ({e}), переход на HTML")
            if data is None:
                try:
                    data, html_warnings, _ = hh_client.fetch_employer_html(session, cache, employer_id)
                    warnings.extend(f"hh.ru: {w}" for w in html_warnings)
                except SourceUnavailable as e:
                    warnings.append(f"hh.ru недоступен: {e}")
                    data = None
            if data:
                try:
                    vacancies, vac_method = hh_client.fetch_vacancies(
                        session, cache, employer_id, 50, fetch_contacts=fetch_vacancy_contacts
                    )
                    data["vacancies"] = vacancies
                    if vac_method == "html":
                        warnings.append("hh.ru vacancies: через HTML-fallback (без id/дат публикации)")
                except SourceUnavailable as e:
                    warnings.append(f"hh.ru vacancies недоступны: {e}")
                result["company_profile"] = data
                result["vacancies"] = data.get("vacancies", [])
                if data.get("site"):
                    site = site or data["site"]
                for v in result["vacancies"]:
                    if v.get("contact_person"):
                        result["candidates"].append(
                            {
                                "name": v["contact_person"],
                                "position": f"контактное лицо по вакансии «{v.get('title', '')}»",
                                "source": "hh_client",
                                "source_url": v.get("url") or hh_url,
                                "context": v.get("title", ""),
                            }
                        )
    else:
        warnings.append("hh_url не передан — блок hh.ru пропущен")

    # --- сайт компании (основной + альтернативные домены того же субъекта,
    # см. modules/01-company-resolve.md п.4c — оба обходятся, не только
    # основной, иначе теряются факты вроде продуктовой линейки/ЛПР,
    # которые есть только на втором домене) ---
    for site_url in filter(None, [site, *(site_alternates or [])]):
        try:
            pages, people, contacts, site_warnings = site_crawler.crawl(
                session, cache, site_url, ["about", "team", "contacts", "press"], 15, cfg["http"]["user_agent"], 1500
            )
            result["site_pages"].extend(pages)
            result["contacts"].extend(contacts)
            warnings.extend(f"сайт {site_url}: {w}" for w in site_warnings)
            for p in people:
                result["candidates"].append(
                    {
                        "name": p["name"],
                        "position": p.get("position", ""),
                        "source": "site_crawler",
                        "source_url": p.get("source_url", site_url),
                        "context": p.get("position", ""),
                    }
                )
        except SourceUnavailable as e:
            warnings.append(f"сайт {site_url} недоступен: {e}")
    if not site and not site_alternates:
        warnings.append("site не передан — обход сайта пропущен")

    # --- habr career ---
    try:
        habr_url = None
        if site:
            pass  # у нас нет прямого маппинга site -> habr slug, ищем по имени
        habr_url = habr_client.search_company(session, company_name)
        if habr_url:
            habr_data, habr_warnings = habr_client.fetch_company(session, cache, habr_url)
            result["habr_profile"] = habr_data
            warnings.extend(f"habr: {w}" for w in habr_warnings)
            contacts = (habr_data.get("profile") or {}).get("contacts") or {}
            email = contacts.get("email")
            if email and not any(email.startswith(p) for p in ("info@", "hr@", "sales@", "pr@", "sale@")):
                # похоже на личный email — потенциальный кандидат по локальной части
                local = email.split("@")[0]
                if "." in local:
                    guessed_name = " ".join(part.capitalize() for part in local.split("."))
                    result["candidates"].append(
                        {
                            "name": guessed_name,
                            "position": "контакт из профиля career.habr.com (email похож на личный)",
                            "source": "habr_client",
                            "source_url": habr_url,
                            "context": f"email {email}",
                        }
                    )
            for k, v in contacts.items():
                result["contacts"].append(f"{k}: {v}")
        else:
            warnings.append("habr: компания не найдена по названию")
    except SourceUnavailable as e:
        warnings.append(f"habr недоступен: {e}")

    # --- широкий поиск руководства + авто-разбор ростер-страниц ---
    query_sets = media_search.load_query_sets()
    templates = query_sets.get("company_leadership", [])
    user_agent = cfg["http"]["user_agent"]
    roster_urls_found: list[str] = []
    for template in templates:
        query = media_search.render_query(template, {"company": company_name, "person": "", "year": ""})
        if not query:
            continue
        try:
            results = media_search.ddg_search(query, 8, user_agent)
        except Exception as e:  # noqa: BLE001 — поиск не должен ронять весь сбор
            warnings.append(f"company_leadership запрос «{query}» не выполнен: {e}")
            continue
        for r in results:
            r["domain"] = urlparse(r["url"]).netloc.replace("www.", "")
            r["matched_template"] = template
            result["leadership_search_results"].append(r)
            if is_roster_url(r["url"]) and r["url"] not in roster_urls_found:
                roster_urls_found.append(r["url"])

    if not templates:
        warnings.append("company_leadership: набор запросов не найден в config/query-templates.md")

    # автоматический разбор найденных ростер-страниц — НЕ пропускается,
    # это и есть исправление кейса WMX/Рыжов (см. docstring файла)
    for url in roster_urls_found[:max_roster_pages]:
        try:
            data, single_warnings = site_crawler.fetch_single_page(session, cache, url, company_name, 1500)
            result["roster_pages_followed"].append(url)
            result["contacts"].extend(data.get("contacts", []))
            for p in data.get("people", []):
                result["candidates"].append(
                    {
                        "name": p["name"],
                        "position": p.get("position", ""),
                        "source": "site_crawler",
                        "source_url": url,
                        "context": p.get("position", ""),
                    }
                )
            warnings.extend(f"ростер {url}: {w}" for w in single_warnings)
        except SourceUnavailable as e:
            warnings.append(f"ростер-страница {url} недоступна: {e}")
    if len(roster_urls_found) > max_roster_pages:
        warnings.append(
            f"найдено {len(roster_urls_found)} ростер-страниц, разобрано {max_roster_pages} "
            f"(--max-roster-pages) — остальные: {roster_urls_found[max_roster_pages:]}"
        )

    result["contacts"] = sorted(set(result["contacts"]))
    return result, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="orchestrated company_profile + broad LPR discovery collection")
    parser.add_argument("--company", required=True)
    parser.add_argument("--site", default=None)
    parser.add_argument("--site-alternates", default=None, help="через запятую — доп. домены того же субъекта")
    parser.add_argument("--hh-url", default=None)
    parser.add_argument("--max-roster-pages", type=int, default=3)
    parser.add_argument("--fetch-vacancy-contacts", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    session = Session(cfg)
    cache = Cache()

    data, warnings = collect(
        args.company,
        args.site,
        args.hh_url,
        session,
        cache,
        cfg,
        args.max_roster_pages,
        args.fetch_vacancy_contacts,
        site_alternates=[s.strip() for s in args.site_alternates.split(",")] if args.site_alternates else None,
    )

    ok = data["company_profile"] is not None or bool(data["site_pages"]) or bool(data["candidates"])
    emit(
        envelope(
            ok=ok,
            source="collect_company",
            method="orchestrated",
            url=args.hh_url or args.site or "",
            data=data,
            warnings=warnings,
            error=None if ok else {"type": "source_unavailable", "message": "все источники недоступны или пусты"},
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        emit(envelope(ok=False, source="collect_company", method="orchestrated", error={"type": "unexpected_error", "message": str(e)}))
