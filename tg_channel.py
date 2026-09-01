"""
tg_channel.py — публичные Telegram-каналы через веб-превью t.me/s/<channel>.

Не требует авторизации, но и не даёт полноценного доступа: доступна только
публичная лента (обычно последние ~20 постов на страницу), поиск по тексту
постов и приватные каналы недоступны. Это ограничение фиксируется в
warnings при каждом вызове — не только когда что-то пошло не так, а всегда,
чтобы модуль, вызывающий скрипт, не принял частичные данные за полные
(MTProto-доступ с полной историей — вне объёма скилла, см. SKILL.md §14).

Использование:
    python tg_channel.py --channel jul_obrazova
    python tg_channel.py --channel jul_obrazova --limit 60
    python tg_channel.py --channel jul_obrazova --before 1234
"""

from __future__ import annotations

import argparse
import re
from typing import Any

from bs4 import BeautifulSoup

from common import Cache, Session, SourceUnavailable, emit, envelope, load_config

TG_BASE = "https://t.me/s"

PERMANENT_LIMITATION = (
    "доступна только публичная веб-превью лента t.me/s/ — поиск по тексту "
    "постов и приватные каналы недоступны, полная история требует MTProto"
)


def parse_messages(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    messages = []
    for msg in soup.select(".tgme_widget_message"):
        msg_id = msg.get("data-post", "").split("/")[-1]
        text_el = msg.select_one(".tgme_widget_message_text")
        date_el = msg.select_one("time")
        link_el = msg.select_one("a.tgme_widget_message_date")
        has_media = bool(msg.select_one(".tgme_widget_message_photo, .tgme_widget_message_video"))
        messages.append(
            {
                "id": msg_id,
                "date": date_el.get("datetime") if date_el else None,
                "text": text_el.get_text(" ", strip=True) if text_el else "",
                "url": link_el["href"] if link_el else None,
                "has_media": has_media,
            }
        )
    return messages


def parse_channel_meta(html: str) -> tuple[str | None, str | None]:
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one(".tgme_channel_info_header_title") or soup.select_one(".tgme_page_title")
    subs_el = soup.select_one(".tgme_channel_info_counter .counter_value")
    return (
        title_el.get_text(" ", strip=True) if title_el else None,
        subs_el.get_text(" ", strip=True) if subs_el else None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="public Telegram channel feed via t.me/s/")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--before", type=int, default=None, help="message id to paginate backwards from")
    args = parser.parse_args()

    channel = args.channel.lstrip("@")
    url = f"{TG_BASE}/{channel}"
    params = {"before": args.before} if args.before else None

    cfg = load_config()
    session = Session(cfg)
    cache = Cache()

    try:
        resp = session.get(url, params=params)
    except SourceUnavailable as e:
        emit(envelope(ok=False, source="t.me", method="s_preview", url=url, error={"type": e.error_type, "message": str(e)}))
        return

    cache.put_raw(url, resp.text, params=params)

    if "tgme_widget_message" not in resp.text and "tgme_channel_info" not in resp.text:
        emit(
            envelope(
                ok=False,
                source="t.me",
                method="s_preview",
                url=url,
                error={"type": "not_found", "message": f"канал «{channel}» не найден или не публичный"},
            )
        )
        return

    title, subscribers = parse_channel_meta(resp.text)
    messages = parse_messages(resp.text)[: args.limit]

    warnings = [PERMANENT_LIMITATION]
    if not messages:
        warnings.append("posts: не найдены на этой странице (лента пуста или превышена пагинация)")

    data = {
        "channel": channel,
        "title": title,
        "subscribers": subscribers,
        "posts": messages,
    }

    emit(envelope(ok=True, source="t.me", method="s_preview", url=url, data=data, warnings=warnings))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        emit(envelope(ok=False, source="t.me", method="s_preview", error={"type": "unexpected_error", "message": str(e)}))
