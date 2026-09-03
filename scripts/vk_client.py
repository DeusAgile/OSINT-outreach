"""
vk_client.py — публичная страница компании VK (посты, описание, контакты).

⚠️ СТАТУС: НЕ ПРОВЕРЕН ЖИВЫМ ЗАПРОСОМ. При попытке во время сборки скилла
vk.com оказался заблокирован на уровне сети песочницы Claude
(`x-block-reason: hostname_blocked`) — это блокировка платформы
выполнения, не анти-бот защита VK и не то, что можно обойти правкой этого
скрипта. Код написан по тому же паттерну, что и остальные HTML-источники
скилла (Session/Cache/envelope из common.py), и должен заработать, если
сеть, из которой реально идёт запуск скилла (не всякая среда выполнения
Claude блокирует vk.com одинаково), не блокирует vk.com — но это
предположение, не проверенный факт. Первое, что стоит сделать при
включении этого источника в реальный прогон — не доверять этому
комментарию, а перепроверить `python vk_client.py --group <slug>` и
посмотреть на реальный код ответа.

Использование:
    python vk_client.py --group webpractik
    python vk_client.py --url https://vk.com/webpractik
"""

from __future__ import annotations

import argparse
import re
from typing import Any

from bs4 import BeautifulSoup

from common import Cache, Session, SourceUnavailable, emit, envelope, load_config

BASE = "https://vk.com"
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def parse_group_page(html: str) -> tuple[dict[str, Any], list[str]]:
    soup = BeautifulSoup(html, "lxml")
    warnings: list[str] = []

    title_el = soup.select_one("meta[property='og:title']") or soup.select_one("h1")
    description_el = soup.select_one("meta[property='og:description']")
    title = title_el.get("content") if title_el and title_el.name == "meta" else (title_el.get_text(" ", strip=True) if title_el else None)
    description = description_el.get("content") if description_el else None

    if not title:
        warnings.append("title: не найдено — возможно, группа приватная или требует авторизации для просмотра")

    text = soup.get_text(" ", strip=True)
    emails = sorted(set(EMAIL_RE.findall(text)))

    # посты в веб-версии без авторизации часто не рендерятся (VK агрессивно
    # просит войти) — если это так, честно фиксируем, а не отдаём пустой
    # список молча
    posts_present = bool(soup.select_one('[data-testid*="post"]') or soup.select_one(".post"))
    if not posts_present:
        warnings.append(
            "posts: не найдены в HTML без авторизации — VK часто требует вход даже для чтения "
            "публичных постов через обычный HTTP-запрос; это ограничение источника, не сбой парсинга"
        )

    return {"title": title, "description": description, "emails": emails, "posts": []}, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="VK public group/company page")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--group", help="slug группы/страницы, например webpractik")
    group.add_argument("--url", help="полный URL вида https://vk.com/webpractik")
    args = parser.parse_args()

    url = args.url or f"{BASE}/{args.group}"

    cfg = load_config()
    session = Session(cfg)
    cache = Cache()

    try:
        resp = session.get(url)
    except SourceUnavailable as e:
        emit(
            envelope(
                ok=False,
                source="vk.com",
                method="html",
                url=url,
                error={
                    "type": e.error_type,
                    "message": f"{e} — если error.type указывает на блокировку сети (не 403/429 от самого VK), "
                    "это, вероятно, платформенная блокировка hostname, а не анти-бот; см. docstring скрипта",
                },
            )
        )
        return

    cache.put_raw(url, resp.text)
    data, warnings = parse_group_page(resp.text)
    emit(envelope(ok=True, source="vk.com", method="html", url=url, data=data, warnings=warnings))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        emit(envelope(ok=False, source="vk.com", method="html", error={"type": "unexpected_error", "message": str(e)}))
