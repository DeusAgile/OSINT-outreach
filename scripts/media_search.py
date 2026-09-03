"""
media_search.py — не самостоятельный источник, а слой генерации запросов
поверх поисковой системы.

Берёт шаблоны из config/query-templates.md по имени набора (--query-set),
подставляет {company}/{person}/{year}, выполняет поиск по каждому шаблону,
дедуплицирует результаты по URL, скорит по домену-источнику (официальный
сайт компании и hh.ru — выше, агрегаторы и малоизвестные домены — ниже) и
возвращает консолидированный список.

Поисковый провайдер по умолчанию ("builtin") — DuckDuckGo HTML-версия
(html.duckduckgo.com), не требует API-ключа. config.search.provider оставлен
расширяемым: строка в реестре меняет метод вызова без переписывания модулей
(тот же принцип, что и config/sources.md для остальных источников).

Использование:
    python media_search.py --query-set lpr_search --company "WMT" --limit 30
    python media_search.py --query-set person_mentions --company "WMT" --person "Юлия Образова"
    python media_search.py --query-set company_insights --company "WMT" --limit 20
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

from common import SKILL_ROOT, emit, envelope, load_config

QUERY_TEMPLATES_PATH = SKILL_ROOT / "config" / "query-templates.md"

DDG_HTML = "https://html.duckduckgo.com/html/"

# Проверено фактическим запуском (см. SKILL.md §4): DuckDuckGo HTML время от
# времени зависает/таймаутит с IP дата-центра (похоже на антибот-защиту, та
# же причина, что и 403 на api.hh.ru), но обычно отдаёт реальные результаты.
# Частичный сбой — не повод считать, что у компании нет цифрового следа:
# сохраняем как permanent-warning, чтобы вызывающий модуль не спутал пустой
# ответ по части шаблонов с отсутствием данных.
SEARCH_RELIABILITY_WARNING = (
    "поисковый провайдер (DuckDuckGo HTML) иногда не отвечает или отдаёт "
    "неполные результаты при запуске с IP дата-центра — если результатов "
    "мало или нет, это не обязательно означает отсутствие цифрового следа"
)

# Скоринг по домену: официальный сайт компании и hh.ru доверенные, крупные
# отраслевые СМИ — средне, агрегаторы и незнакомые домены — ниже.
HIGH_TRUST_DOMAINS = {"hh.ru", "career.habr.com", "habr.com"}
MEDIUM_TRUST_DOMAINS = {
    "vc.ru", "rb.ru", "cnews.ru", "forbes.ru", "tenchat.ru", "t.me",
    "comnews.ru", "amlive.ru", "setka.ru", "dreamjob.ru",
}
# dreamjob.ru — не в HIGH_TRUST специально: сама структура страницы
# работодателя надёжна (как источник ДЛЯ поиска нужной компании), но
# содержание — субъективные отзывы, не факты; высокий скоринг здесь не
# должен читаться как «содержимому можно доверять как hh.ru» — см.
# dreamjob_client.py.


def load_query_sets() -> dict[str, list[str]]:
    """Парсит config/query-templates.md: заголовки '**Набор `name`:**' и
    следующий за ними ```блок``` со строками-шаблонами."""
    if not QUERY_TEMPLATES_PATH.exists():
        return {}
    text = QUERY_TEMPLATES_PATH.read_text(encoding="utf-8")
    sets: dict[str, list[str]] = {}
    # Матчим только заголовки вида "Набор `name`:**", а не любое упоминание
    # имени набора в тексте (в пояснениях имя набора тоже встречается в
    # обратных кавычках) — иначе пояснительный текст перед первым набором
    # ложно захватывает код первого блока.
    pattern = re.compile(r"Набор `([a-z_]+)`:\*\*\s*```\n(.*?)```", re.DOTALL)
    for match in pattern.finditer(text):
        name = match.group(1)
        lines = [l.strip() for l in match.group(2).strip().split("\n") if l.strip()]
        sets[name] = lines
    return sets


def render_query(template: str, params: dict[str, str]) -> Optional[str]:
    try:
        return template.format(**params)
    except KeyError:
        return None  # шаблон требует параметр, которого нет (например {person} без --person)


def score_result(url: str, company_domain: Optional[str]) -> float:
    domain = urlparse(url).netloc.replace("www.", "")
    if company_domain and domain == company_domain.replace("www.", ""):
        return 1.0
    if domain in HIGH_TRUST_DOMAINS:
        return 0.9
    if domain in MEDIUM_TRUST_DOMAINS:
        return 0.6
    return 0.3


def unwrap_ddg_link(href: str) -> str:
    """DuckDuckGo HTML отдаёт ссылки-редиректы вида
    '//duckduckgo.com/l/?uddg=<urlencoded-target>&rut=...' — разворачиваем
    в реальный целевой URL, иначе скоринг по домену и вся дедупликация
    бессмысленны (все ссылки оказались бы на duckduckgo.com)."""
    if "uddg=" not in href:
        return href
    from urllib.parse import parse_qs, unquote, urlsplit

    query = urlsplit(href if href.startswith("http") else "https:" + href).query
    parsed = parse_qs(query)
    target = parsed.get("uddg")
    return unquote(target[0]) if target else href


def ddg_search(query: str, limit: int, user_agent: str) -> list[dict[str, str]]:
    """Прямой запрос к DuckDuckGo HTML с коротким таймаутом и одной попыткой.

    Поисковые движки не читают Session.get() из common.py: там backoff/ретраи
    рассчитаны на источники данных (API, сайты компаний), а не на поисковики
    с антибот-защитой — там повторные попытки при 403/зависании только
    удлиняют ожидание без пользы. Сбой здесь — это ok:false с понятной
    причиной, а не долгое зависание всего пайплайна.
    """
    resp = requests.get(
        DDG_HTML,
        params={"q": query},
        headers={"User-Agent": user_agent, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
        timeout=8,
    )
    soup = BeautifulSoup(resp.text, "lxml")
    results = []
    for r in soup.select(".result")[:limit]:
        link_el = r.select_one("a.result__a")
        snippet_el = r.select_one(".result__snippet")
        if not link_el:
            continue
        href = unwrap_ddg_link(link_el.get("href", ""))
        results.append(
            {
                "url": href,
                "title": link_el.get_text(" ", strip=True),
                "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="template-driven search across query sets")
    parser.add_argument("--query-set", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--person", default=None)
    parser.add_argument("--year", default=str(datetime.now().year))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--company-domain", default=None)
    args = parser.parse_args()

    sets = load_query_sets()
    if args.query_set not in sets:
        emit(
            envelope(
                ok=False,
                source="media_search",
                method="query_templates",
                error={
                    "type": "config_error",
                    "message": f"набор запросов «{args.query_set}» не найден в config/query-templates.md. Доступны: {', '.join(sets.keys()) or 'нет'}",
                },
            )
        )
        return

    params = {"company": args.company, "person": args.person or "", "year": args.year}
    templates = sets[args.query_set]
    queries_used = []
    all_results: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    cfg = load_config()
    user_agent = cfg["http"]["user_agent"]
    per_query_limit = max(3, args.limit // max(len(templates), 1))

    for template in templates:
        query = render_query(template, params)
        if query is None:
            continue  # шаблон person_mentions без --person, например
        queries_used.append(query)
        try:
            results = ddg_search(query, per_query_limit, user_agent)
        except requests.exceptions.RequestException as e:
            warnings.append(f"запрос «{query}» не выполнен: {e}")
            continue
        for r in results:
            if r["url"] not in all_results:
                r["domain"] = urlparse(r["url"]).netloc.replace("www.", "")
                r["score"] = score_result(r["url"], args.company_domain)
                r["matched_template"] = template
                all_results[r["url"]] = r

    ranked = sorted(all_results.values(), key=lambda r: r["score"], reverse=True)[: args.limit]

    if not queries_used:
        warnings.append("ни один шаблон не удалось подставить (проверьте --person, если набор его требует)")
    if not ranked:
        warnings.append("результаты не найдены ни по одному запросу")
    warnings.append(SEARCH_RELIABILITY_WARNING)

    ok = bool(queries_used)  # если хоть один запрос выполнился, это не отказ — просто может быть пусто
    emit(
        envelope(
            ok=ok,
            source="media_search",
            method="ddg_html" if cfg["search"].get("provider", "builtin") == "builtin" else cfg["search"]["provider"],
            data={"queries_used": queries_used, "results": ranked},
            warnings=warnings,
            error=None if ok else {"type": "config_error", "message": "нет подставленных запросов"},
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        emit(envelope(ok=False, source="media_search", method="ddg_html", error={"type": "unexpected_error", "message": str(e)}))
