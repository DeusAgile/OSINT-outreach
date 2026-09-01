"""
common.py — общая обвязка для всех скриптов talentmind-outreach.

Даёт: Session с ретраями и вежливыми паузами, файловый кэш, единый JSON-
конверт ответа, чтение config.json, вывод результата/ошибки, проверку
доступности headless-браузера.

Ничего в этом файле не обращается к сети напрямую при импорте — безопасно
импортировать из любого скрипта источника.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Пути и конфиг
# ---------------------------------------------------------------------------

SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = SKILL_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.json"
EXAMPLE_CONFIG_PATH = CONFIG_DIR / "config.example.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "hh": {"api_token": "", "prefer_api": True},
    "http": {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "timeout_sec": 25,
        "retries": 3,
        "per_domain_delay_sec": 2,
    },
    "search": {"provider": "builtin", "region_id": 225, "results": 20},
    "report": {"insights_period_months": 12, "max_persons": 5},
    "paths": {"cache": "cache", "output": "reports"},
}


def load_config() -> dict[str, Any]:
    """Читает config/config.json, накладывая поверх DEFAULT_CONFIG.

    Отсутствие config.json — не ошибка: скрипт работает на дефолтах и без
    токенов (просто идёт по fallback-путям). Битый JSON — ошибка конфигурации
    (код возврата 2), потому что тут пользователь явно пытался что-то
    настроить и промахнулся.
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if DEFAULT_CONFIG_PATH.exists():
        try:
            with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
        except json.JSONDecodeError as e:
            fail(f"config/config.json содержит невалидный JSON: {e}", code=2)
        for section, values in user_cfg.items():
            if isinstance(values, dict) and isinstance(cfg.get(section), dict):
                cfg[section].update(values)
            else:
                cfg[section] = values
    return cfg


# ---------------------------------------------------------------------------
# Вывод результата
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(
    *,
    ok: bool,
    source: str,
    method: str,
    url: str = "",
    data: Any = None,
    warnings: Optional[list[str]] = None,
    cache_path: Optional[str] = None,
    error: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    env: dict[str, Any] = {
        "ok": ok,
        "source": source,
        "method": method,
        "fetched_at": now_iso(),
        "url": url,
        "data": data if data is not None else {},
        "warnings": warnings or [],
    }
    if cache_path:
        env["cache_path"] = cache_path
    if not ok:
        env["error"] = error or {"type": "unknown", "message": "unspecified error"}
    return env


def emit(obj: dict[str, Any]) -> None:
    """Печатает результат в stdout строго как JSON — никаких других строк
    в stdout не должно быть ни до, ни после (человекочитаемые пояснения,
    если нужны, идут в stderr)."""
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def fail(message: str, code: int = 1, *, error_type: str = "error") -> None:
    """Печатает сообщение в stderr и завершает процесс ненулевым кодом.

    Коды: 1 — источник недоступен/заблокирован; 2 — ошибка конфигурации;
    3 — неверные аргументы. Используется для фатальных сбоев ДО того, как
    у нас появилась возможность сформировать содержательный envelope
    (например, невалидные аргументы командной строки). Когда источник просто
    недоступен во время обычной работы скрипта, предпочтительно вернуть
    envelope с ok:false через emit(), а не fail() — вызывающий модуль ждёт
    JSON на stdout даже при неудаче источника (см. п.5 SKILL.md).
    """
    print(f"[{error_type}] {message}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Кэш
# ---------------------------------------------------------------------------


class Cache:
    """Файловый кэш в пределах одного прогона. Ключ — md5 от нормализованного
    URL + параметров. Между сессиями не персистентен (файловая система
    сбрасывается) — это ограничение зафиксировано в SKILL.md, не баг кэша."""

    def __init__(self, cache_dir: Optional[str] = None):
        cfg = load_config()
        raw = cache_dir or cfg["paths"]["cache"]
        p = Path(raw)
        # относительный путь в конфиге — относительно корня скилла, а не
        # текущей рабочей директории вызывающего процесса
        self.dir = p if p.is_absolute() else (SKILL_ROOT / p)
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(url: str, params: Optional[dict] = None) -> str:
        norm = url.strip().rstrip("/")
        if params:
            norm += "?" + json.dumps(params, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(norm.encode("utf-8")).hexdigest()

    def raw_path(self, url: str, params: Optional[dict] = None) -> Path:
        return self.dir / f"{self._key(url, params)}.raw"

    def json_path(self, url: str, params: Optional[dict] = None) -> Path:
        return self.dir / f"{self._key(url, params)}.json"

    def get_raw(self, url: str, params: Optional[dict] = None) -> Optional[str]:
        p = self.raw_path(url, params)
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None

    def put_raw(self, url: str, content: str, params: Optional[dict] = None) -> str:
        p = self.raw_path(url, params)
        p.write_text(content, encoding="utf-8")
        return str(p)

    def get_json(self, url: str, params: Optional[dict] = None) -> Optional[Any]:
        p = self.json_path(url, params)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def put_json(self, url: str, obj: Any, params: Optional[dict] = None) -> str:
        p = self.json_path(url, params)
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(p)


# ---------------------------------------------------------------------------
# Session — обёртка над requests с ретраями, backoff, вежливыми паузами
# ---------------------------------------------------------------------------


class SourceUnavailable(Exception):
    """Источник не отвечает после ретраев, заблокирован (403/429 после
    попыток) или иная ошибка сети. Ловится вызывающим кодом скрипта, который
    оборачивает её в envelope(ok=False, ...) — сеть НИКОГДА не должна убивать
    процесс необработанным traceback'ом."""

    def __init__(self, message: str, error_type: str = "source_unavailable"):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


class Session:
    """requests.Session с браузерным User-Agent, таймаутом, тремя попытками
    и экспоненциальным backoff на 5xx/сетевые сбои, плюс реестр вежливых
    задержек по доменам (не чаще одного запроса в N секунд к одному хосту
    в рамках прогона)."""

    _last_request_at: dict[str, float] = {}  # per-process registry, keyed by host

    def __init__(self, cfg: Optional[dict[str, Any]] = None):
        self.cfg = cfg or load_config()
        http_cfg = self.cfg["http"]
        self.timeout = http_cfg.get("timeout_sec", 25)
        self.retries = http_cfg.get("retries", 3)
        self.per_domain_delay = http_cfg.get("per_domain_delay_sec", 2)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": http_cfg.get("user_agent", DEFAULT_CONFIG["http"]["user_agent"]),
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            }
        )

    def _respect_domain_delay(self, url: str) -> None:
        from urllib.parse import urlparse

        host = urlparse(url).netloc
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            wait = self.per_domain_delay - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    def get(self, url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None) -> requests.Response:
        """GET с ретраями/backoff. Поднимает SourceUnavailable при исчерпании
        попыток — не requests.exceptions напрямую, чтобы вызывающий код имел
        один тип исключения для всех сетевых сбоев."""
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries):
            self._respect_domain_delay(url)
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
            except requests.exceptions.RequestException as e:
                last_exc = e
                self._backoff_sleep(attempt)
                continue

            if resp.status_code in (403, 429):
                # адаptivная пауза при 403/429: увеличенная задержка, повтор,
                # затем явный отказ с причиной
                if attempt < self.retries - 1:
                    self._backoff_sleep(attempt, extra=True)
                    continue
                raise SourceUnavailable(
                    f"{url} → HTTP {resp.status_code} после {self.retries} попыток",
                    error_type="blocked",
                )
            if resp.status_code >= 500:
                last_exc = requests.exceptions.HTTPError(f"HTTP {resp.status_code}")
                self._backoff_sleep(attempt)
                continue

            return resp

        raise SourceUnavailable(
            f"{url} → недоступен после {self.retries} попыток ({last_exc})",
            error_type="network_error",
        )

    @staticmethod
    def _backoff_sleep(attempt: int, extra: bool = False) -> None:
        base = 2 * (2**attempt)  # 2, 4, 8 ...
        if extra:
            base *= 1.5
        time.sleep(base)


def browser_available() -> bool:
    """Проверяет, может ли Chromium реально запуститься (не просто что
    playwright установлен как пакет — бинарники ms-playwright могут
    отсутствовать, тогда нужен HTTP-fallback). Дешёвая проверка: пробуем
    запустить и сразу закрыть браузер с коротким таймаутом."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(timeout=8000)
            browser.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Утилиты общего назначения
# ---------------------------------------------------------------------------


def read_text_or_none(path: str | Path) -> Optional[str]:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else None
