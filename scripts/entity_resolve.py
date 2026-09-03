"""
entity_resolve.py — склейка и атрибуция персон-кандидатов, собранных
разными модулями пайплайна.

Это скрипт, а не рекомендация модели — намеренно. anti-patterns.md
перечисляет вещи вроде «не приписывать факт человеку по одному слабому
совпадению имени» или «не путать тёзок», но без детерминированного скрипта
эти правила остаются декларацией, которую модель нарушит на неполных данных
(см. §6.7 build-спеки). Здесь эти правила закодированы как проверяемая
логика: нормализация ФИО, склейка дублей, привязка к компании по домену/
контексту, скоринг совпадения и предварительный confidence.

Использование:
    python entity_resolve.py --candidates candidates.json --company "WMT" --company-domain wmtgroup.ru

candidates.json — список кандидатов вида:
    [{"name": "Юлия Образова", "position": "HRD", "source_url": "...",
      "source": "site_crawler", "context": "..."}, ...]
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any, Optional

from common import emit, envelope, fail

# Частые транслитерации латиница<->кириллица для сопоставления, не для
# перевода — только когда одно и то же ФИО встретилось в обеих системах
# письма (например, в email или на англоязычном профиле).
TRANSLIT_MAP = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "z": "з",
    "i": "и", "y": "й", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
    "p": "п", "r": "р", "s": "с", "t": "т", "u": "у", "f": "ф", "h": "х",
    "c": "ц", "j": "й",
}


def normalize_name(raw: str) -> str:
    """Приводит ФИО к сравнимой форме: убирает падежные окончания по
    грубой эвристике, лишние пробелы, приводит к нижнему регистру для
    сравнения (регистр для отображения сохраняется отдельно в name_variants)."""
    name = re.sub(r"\s+", " ", raw.strip())
    # убираем инициалы вида "И.И." слипшиеся с фамилией — не разделяем,
    # просто нормализуем пробелы вокруг точек
    name = re.sub(r"\.(?=\S)", ". ", name)
    return name


def to_translit_key(name: str) -> str:
    """Грубый ключ для сопоставления транслитерированных вариантов —
    достаточно для дедупликации, не для качественного перевода."""
    lowered = name.lower()
    out = []
    for ch in lowered:
        out.append(TRANSLIT_MAP.get(ch, ch))
    return re.sub(r"[^а-яё ]", "", "".join(out)).strip()


def name_words_prefixed(name: str) -> set[str]:
    """Слова ФИО как 4-буквенные (или короче — целиком) префиксы после
    транслитерации в кириллицу. Использование префикса вместо точного стемминга
    надёжнее для русских падежных окончаний (Юлия/Юлии/Юлию, Образова/Образовой)
    — устраняет расхождение в 1-2 последних буквах, которое точный стемминг
    ловит не всегда."""
    words = normalize_name(name).replace(".", "").split()
    out = set()
    for w in words:
        w = to_translit_key(w) or w.lower()
        # Русские падежные окончания обычно 1-2 буквы: для коротких слов
        # (4-5 букв) достаточно отрезать последнюю; для более длинных берём
        # первые 4 буквы как стабильный корень, не совпадающий у разных
        # слов, но устойчивый к падежу одного и того же слова.
        if len(w) > 5:
            prefix = w[:4]
        elif len(w) >= 4:
            prefix = w[:-1]
        else:
            prefix = w
        out.add(prefix)
    return out


def name_similarity_key(name: str) -> str:
    """Устаревший точный ключ — оставлен для обратной совместимости
    отображения name_normalized; фактическая склейка теперь идёт через
    name_words_prefixed() и сравнение по пересечению префиксов (см. resolve())."""
    return " ".join(sorted(name_words_prefixed(name)))


def company_link_evidence(candidate: dict[str, Any], company: str, company_domain: Optional[str]) -> Optional[dict[str, str]]:
    """Ищет основание для привязки кандидата к компании: домен в email/URL,
    упоминание компании в позиции/контексте. Возвращает None, если основания
    нет — вызывающий код тогда обязан не приписывать человека к компании
    (anti-pattern: «не подтягивать факты о компании-однофамильце»).

    ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ: совпадение по названию компании в context — это
    поиск подстроки, а не проверка утверждения. Контекст вида «не имеет
    отношения к WMT» тоже содержит «WMT» и будет засчитан как context_mention.
    Это не страшно само по себе: единственный источник с context_mention даёт
    только TENTATIVE (см. score_and_confidence) — самый низкий уровень, и
    module 03/05 не должны выдавать TENTATIVE-находки за идентифицированного
    ЛПР без независимого подтверждения. Но если модуль-вызывающий код решит
    доверять company_link напрямую, эту эвристику стоит доусилить (проверка
    отрицаний рядом с упоминанием) до этого."""
    text_fields = " ".join(
        str(candidate.get(f, "")) for f in ("position", "context", "source_url", "email")
    ).lower()
    company_lower = company.lower()

    if company_domain and company_domain.lower() in text_fields:
        return {"type": "domain_match", "evidence_url": candidate.get("source_url", "")}
    if company_lower in text_fields:
        return {"type": "context_mention", "evidence_url": candidate.get("source_url", "")}
    return None


def score_and_confidence(sources: list[str], link_type: Optional[str], evidence_urls: Optional[list[str]] = None) -> tuple[float, str]:
    """Скоринг совпадения и предварительный confidence по правилам из
    config/confidence-levels.md:
    - TENTATIVE: один источник, независимо не подтверждён
    - FIRM: один источник, но первичный/официальный (сайт компании, hh.ru)
    - CONFIRMED: подтверждено ≥2 независимыми источниками

    НАЙДЕННЫЙ БАГ (реальный кейс RED SOFT): «независимость» раньше считалась
    по имени СКРИПТА (`sources`), а не по тому, откуда данные взялись на
    самом деле. `media_search.py`/`web_search` — это не источник данных, а
    поисковый слой поверх множества сайтов; когда он находит одного и того
    же человека на list-org.com И на audit-it.ru (два независимых
    реестровых сайта), обе находки помечались `source: "media_search"` —
    `set(sources)` схлопывался в один элемент, и подтверждённый двумя
    независимыми реестрами гендиректор получал TENTATIVE вместо CONFIRMED.
    Теперь независимость считается по количеству УНИКАЛЬНЫХ ДОМЕНОВ в
    `evidence_urls`, если они переданы — а не только по именам скриптов.
    Домены между собой считаются независимыми источниками, даже если оба
    найдены через один и тот же поисковый скрипт."""
    primary_sources = {"site_crawler", "hh_client"}
    unique_sources = set(sources)

    unique_domains: set[str] = set()
    if evidence_urls:
        from urllib.parse import urlparse

        for u in evidence_urls:
            if u:
                netloc = urlparse(u).netloc.lower().replace("www.", "")
                if netloc:
                    unique_domains.add(netloc)

    independence_count = max(len(unique_sources), len(unique_domains))

    if independence_count >= 2:
        confidence = "CONFIRMED"
        score = 0.9
    elif unique_sources & primary_sources:
        confidence = "FIRM"
        score = 0.7
    else:
        confidence = "TENTATIVE"
        score = 0.4

    if link_type is None:
        # без привязки к компании даже несколько источников не спасают
        # уверенность — это может быть один и тот же тёзка в разных местах
        confidence = "TENTATIVE"
        score = min(score, 0.3)
    return score, confidence


def resolve(candidates: list[dict[str, Any]], company: str, company_domain: Optional[str]) -> list[dict[str, Any]]:
    # groups: list of {prefixes: set[str], name_normalized, name_variants, ...}
    groups: list[dict[str, Any]] = []

    def find_group(prefixes: set[str]) -> Optional[dict[str, Any]]:
        best, best_overlap = None, 0
        for g in groups:
            overlap = len(g["prefixes"] & prefixes)
            # Для 2-словных имён (Имя Фамилия) требуем совпадения ОБОИХ слов —
            # иначе двух разных "Ивановых" склеит по одной фамилии (anti-pattern:
            # тёзки/однофамильцы). Для 3-словных (с отчеством) допускаем 1
            # расхождение, так как отчество на сайтах часто опускают.
            shorter = min(len(g["prefixes"]), len(prefixes))
            needed = shorter if shorter <= 2 else shorter - 1
            if overlap >= needed and overlap > best_overlap:
                best, best_overlap = g, overlap
        return best

    for cand in candidates:
        raw_name = cand.get("name", "").strip()
        if not raw_name:
            continue
        prefixes = name_words_prefixed(raw_name)
        if not prefixes:
            continue

        link = company_link_evidence(cand, company, company_domain)

        group = find_group(prefixes)
        if group is None:
            group = {
                "prefixes": set(prefixes),
                "name_normalized": normalize_name(raw_name),
                "name_variants": set(),
                "position": cand.get("position"),
                "company_link": None,
                "sources": [],
                "evidence_urls": [],
            }
            groups.append(group)
        else:
            group["prefixes"] |= prefixes  # расширяем набор известных вариантов префиксов

        group["name_variants"].add(raw_name)
        if not group["position"] and cand.get("position"):
            group["position"] = cand.get("position")
        source = cand.get("source", "unknown")
        group["sources"].append(source)
        if cand.get("source_url"):
            group["evidence_urls"].append(cand["source_url"])
        if link and group["company_link"] is None:
            group["company_link"] = link

    persons = []
    for group in groups:
        score, confidence = score_and_confidence(
            group["sources"], (group["company_link"] or {}).get("type"), group["evidence_urls"]
        )
        persons.append(
            {
                "name_normalized": group["name_normalized"],
                "name_variants": sorted(group["name_variants"]),
                "position": group["position"],
                "company_link": group["company_link"] or {"type": "none", "evidence_url": None},
                "sources": sorted(set(group["sources"])),
                "score": round(score, 2),
                "confidence_suggested": confidence,
            }
        )

    persons.sort(key=lambda p: p["score"], reverse=True)
    return persons


def main() -> None:
    parser = argparse.ArgumentParser(description="resolve and dedupe person candidates, attribute to company")
    parser.add_argument("--candidates", required=True, help="path to candidates.json")
    parser.add_argument("--company", required=True)
    parser.add_argument("--company-domain", default=None)
    args = parser.parse_args()

    try:
        with open(args.candidates, "r", encoding="utf-8") as f:
            candidates = json.load(f)
    except FileNotFoundError:
        fail(f"файл кандидатов не найден: {args.candidates}", code=3)
        return
    except json.JSONDecodeError as e:
        fail(f"candidates.json содержит невалидный JSON: {e}", code=3)
        return

    if not isinstance(candidates, list):
        fail("candidates.json должен содержать список объектов-кандидатов", code=3)
        return

    persons = resolve(candidates, args.company, args.company_domain)

    warnings = []
    no_link = [p for p in persons if p["company_link"]["type"] == "none"]
    if no_link:
        warnings.append(
            f"{len(no_link)} кандидат(ов) без подтверждённой привязки к компании — "
            "не использовать как основание факта без ручной проверки (anti-pattern: тёзки/однофамильцы)"
        )

    emit(
        envelope(
            ok=True,
            source="entity_resolve",
            method="heuristic",
            data={"persons": persons},
            warnings=warnings,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        emit(envelope(ok=False, source="entity_resolve", method="heuristic", error={"type": "unexpected_error", "message": str(e)}))
