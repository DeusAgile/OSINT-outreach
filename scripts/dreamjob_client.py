"""
dreamjob_client.py — Dream Job (dreamjob.ru), сервис отзывов сотрудников и
кандидатов о работодателях (принадлежит/интегрирован с hh.ru).

Даёт то, чего нет ни в одном другом источнике скилла: как реально
происходит отбор в компанию (число этапов, формат, вопросы на
собеседовании — раздел /interviews) и как сотрудники оценивают условия,
руководство, коллектив (раздел отзывов на главной странице компании).

ВАЖНО — эпистемический статус этого источника отличается от остальных.
Отзывы — это субъективные, анонимные мнения отдельных людей, не
верифицированные факты о компании. Дважды подтверждено при разборе
источника (см. обзоры сервиса на otzovik.com/irecommend.ru): у площадки
есть репутация избирательной модерации (жалобы на удаление негативных
отзывов по запросу работодателя). Поэтому каждый факт отсюда должен идти
с пометкой «по отзыву сотрудника/кандидата», не как нейтральное
утверждение о компании — это не техническая деталь, а требование к тому,
как модуль-потребитель цитирует этот источник (см. modules/02, 03).

Использование:
    python dreamjob_client.py --company-url https://dreamjob.ru/employers/25713
    python dreamjob_client.py --company-url https://dreamjob.ru/employers/25713 --reviews-limit 15
    python dreamjob_client.py --company-url https://dreamjob.ru/employers/25713 --no-interviews

Прямого поиска работодателя по названию у dreamjob.ru нет (в отличие от
hh.ru) — employer_id узнаётся через media_search.py/web_search с
`site:dreamjob.ru <название>` (см. config/query-templates.md, набор
`hr_process`), а не через этот скрипт.
"""

from __future__ import annotations

import argparse
import re
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common import Cache, Session, SourceUnavailable, emit, envelope, load_config

BASE = "https://dreamjob.ru"


def parse_overview(soup: BeautifulSoup) -> dict[str, Any]:
    content = soup.select_one(".reviews__content") or soup
    text = content.get_text(" ", strip=True)
    lines_text = content.get_text("\n", strip=True)

    rating = None
    m = re.search(r"(\d[,.]\d)\s*(?:Очень хорошо|Хорошо|Отлично|Плохо|Нормально)?", text)
    if m:
        rating = m.group(1).replace(",", ".")

    review_count = None
    m2 = re.search(r"(\d+)\s*отзыв", text)
    if m2:
        review_count = int(m2.group(1))

    # "Рекомендуют" — подпись, "94%" идёт на СЛЕДУЮЩЕЙ строке, не перед ней
    recommend_pct = None
    m3 = re.search(r"Рекомендуют\n(\d+)%", lines_text)
    if m3:
        recommend_pct = int(m3.group(1))

    categories: dict[str, str] = {}
    dashboard = soup.select_one(".dashboard__ratings")
    if dashboard:
        # текст вида "4,8 Условия труда 4,6 Коллектив 4,5 Руководство ..."
        dtext = dashboard.get_text(" ", strip=True)
        for cm in re.finditer(r"(\d[,.]\d)\s+([А-Яа-яё][^\d]{2,30}?)(?=\s\d[,.]\d|\s*$)", dtext):
            categories[cm.group(2).strip()] = cm.group(1).replace(",", ".")

    return {
        "rating": float(rating) if rating else None,
        "review_count": review_count,
        "recommend_pct": recommend_pct,
        "category_ratings": categories,
    }


def parse_reviews(soup: BeautifulSoup, limit: int, max_chars: int = 400) -> list[dict[str, Any]]:
    reviews = []
    for r in soup.select(".review")[:limit]:
        title_el = r.select_one(".review__title")
        text_el = r.select_one(".review__text")
        loc_el = r.select_one(".review__location")
        if not text_el:
            continue
        reviews.append(
            {
                "question": title_el.get_text(" ", strip=True) if title_el else None,
                "text": text_el.get_text(" ", strip=True)[:max_chars],
                "location_date": loc_el.get_text(" ", strip=True) if loc_el else None,
            }
        )
    return reviews


def parse_interviews(soup: BeautifulSoup, limit: int) -> list[dict[str, Any]]:
    """Разбирает /interviews — структурированные описания процесса отбора:
    число этапов, длительность, формат, вопросы, что входило в отбор.

    Вёрстка кладёт подпись поля и его значение на РАЗНЫЕ строки (не
    "Количество этапов: 1 этап" в одной строке, а "Количество этапов"\\n"1
    этап"), и подпись последнего поля сама переносится на две строки
    ("Что включал в себя"\\n"процесс отбора в компанию") — построчный
    парсинг по labels, а не regex на слитный текст."""
    lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    FIELD_LABELS = {
        "Количество этапов": "stages",
        "Длительность": "duration",
        "Где проходило": "format",
        "Вопросы на собеседовании": "questions",
    }
    LAST_LABEL_PART1 = "Что включал в себя"
    LAST_LABEL_PART2 = "процесс отбора в компанию"

    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line in FIELD_LABELS and i + 1 < len(lines):
            current[FIELD_LABELS[line]] = lines[i + 1]
            i += 2
            continue
        if line == LAST_LABEL_PART1 and i + 2 < len(lines) and lines[i + 1] == LAST_LABEL_PART2:
            current["process_included"] = lines[i + 2]
            i += 3
            if current:
                entries.append(current)
                if len(entries) >= limit:
                    break
                current = {}
            continue
        i += 1
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description="Dream Job employer reviews and interview-process data")
    parser.add_argument("--company-url", required=True, help="https://dreamjob.ru/employers/<id>")
    parser.add_argument("--reviews-limit", type=int, default=15)
    parser.add_argument("--max-chars-per-review", type=int, default=400, help="срез текста отзыва (экономия токенов)")
    parser.add_argument("--interviews-limit", type=int, default=15)
    parser.add_argument("--no-interviews", action="store_true", help="пропустить раздел /interviews (экономия запроса)")
    args = parser.parse_args()

    cfg = load_config()
    session = Session(cfg)
    cache = Cache()

    base_url = args.company_url.rstrip("/")
    warnings: list[str] = []

    try:
        resp = session.get(base_url)
    except SourceUnavailable as e:
        emit(envelope(ok=False, source="dreamjob.ru", method="html", url=base_url, error={"type": e.error_type, "message": str(e)}))
        return

    cache.put_raw(base_url, resp.text)
    soup = BeautifulSoup(resp.text, "lxml")

    if "employers" not in resp.url and "employers" not in base_url:
        emit(
            envelope(
                ok=False,
                source="dreamjob.ru",
                method="html",
                url=base_url,
                error={"type": "invalid_arguments", "message": "URL не похож на страницу работодателя (ожидается /employers/<id>)"},
            )
        )
        return

    overview = parse_overview(soup)
    reviews = parse_reviews(soup, args.reviews_limit, args.max_chars_per_review)
    if not reviews:
        warnings.append("reviews: не найдены — либо у компании их нет на Dream Job, либо изменилась вёрстка")

    interviews: list[dict[str, Any]] = []
    if not args.no_interviews:
        interviews_url = urljoin(base_url + "/", "interviews")
        try:
            int_resp = session.get(interviews_url)
            cache.put_raw(interviews_url, int_resp.text)
            int_soup = BeautifulSoup(int_resp.text, "lxml")
            interviews = parse_interviews(int_soup, args.interviews_limit)
        except SourceUnavailable as e:
            warnings.append(f"interviews: раздел недоступен ({e})")
        if not interviews:
            warnings.append("interviews: описания процесса отбора не найдены — либо кандидаты их не оставляли, либо вёрстка изменилась")

    warnings.append(
        "все данные с этой страницы — субъективные анонимные отзывы сотрудников/кандидатов, "
        "не верифицированные факты о компании; у площадки есть репутация избирательной модерации "
        "(жалобы на удаление негативных отзывов по запросу работодателя) — цитировать как «по отзыву "
        "сотрудника», не как нейтральный факт"
    )

    data = {"overview": overview, "reviews": reviews, "interviews": interviews}
    emit(envelope(ok=True, source="dreamjob.ru", method="html", url=base_url, data=data, warnings=warnings))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        emit(envelope(ok=False, source="dreamjob.ru", method="html", error={"type": "unexpected_error", "message": str(e)}))
