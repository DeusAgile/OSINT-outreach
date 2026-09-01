# Реестр источников

Читается модулями по полю `category` при каждом запуске (не хранится
внутри модулей). Добавление источника = новая строка здесь, без правки
логики модулей — см. «Архитектурный принцип» в SKILL.md.

`status: pending` и `status: unverified` — источники не вызываются
автоматически модулями (кроме явного ручного запроса). `status: partial` —
вызываются, но помечаются в отчёте как источник с ограниченным охватом.

| Источник | category | Скрипт | Метод | status |
|---|---|---|---|---|
| hh.ru — компания | company_profile | `hh_client.py` | API при токене, иначе `data-qa` | works |
| hh.ru — вакансии | company_profile, lpr_search | `hh_client.py --with-vacancies` | то же | works |
| Сайт компании | company_profile | `site_crawler.py` | HTML | works |
| Сайт — «Команда»/«Контакты» | lpr_search | `site_crawler.py --sections team,contacts` | HTML | works |
| Habr Career — компания | lpr_search | `habr_client.py --company` | HTML | works |
| Habr / Habr Q&A — персона | person_mentions | `habr_client.py --person` | HTML | partial — профиль career.habr.com сам по себе SPA без SSR (обычный GET получает пустую оболочку, поля осознанно `null`, см. предупреждение скрипта); статьи на habr.com/users всё ещё тянутся |
| Отраслевые СМИ (VC.ru, RB.ru, CNews, Forbes) | person_mentions | `media_search.py` | поиск + fetch | partial |
| TenChat — профиль | person_mentions | `tenchat_client.py --profile` | SSR | works |
| TenChat — поиск людей | lpr_search | `tenchat_client.py --headless` | браузер | unverified |
| Telegram — публичные каналы | person_mentions | `tg_channel.py` | `t.me/s/` | partial |
| YouTube | person_mentions | `media_search.py` | поиск | partial |
| Подкаст-платформы (Яндекс Музыка, Звук, Apple Podcasts) | person_mentions | `media_search.py` | поиск | partial |
| Реестры юрлиц (Checko / Datanewton) | company_profile | — | API | pending |
| Поисковые системы | вспомогательный | `media_search.py` | поиск | partial |
| ~~LinkedIn~~ | — | — | — | не реализуется |

## Известное ограничение: `media_search.py` понижен до `partial`

Изначально спецификация помечала `media_search.py` как `works` для всех
своих категорий. При проверке запуском в среде исполнения скилла DuckDuckGo
HTML (провайдер по умолчанию) и Bing HTML отдавали анти-бот заглушки/decoy-
контент с IP дата-центра — та же причина, что и 403 на `api.hh.ru` (см.
build-спеку §4). Скрипт не падает и не зависает (короткий таймаут, без
ретраев на уровне поиска — см. docstring `media_search.py`), но пустой
результат от него **не является доказательством** отсутствия цифрового
следа персоны — это ограничение среды, а не факт о персоне.

Модули 03/04, читающие эту таблицу, обязаны трактовать `partial`-статус
`media_search.py` так же, как и для остальных partial-источников: вызывать,
но не считать «не найдено» окончательным без пометки в отчёте.

Требуется отдельная инфраструктура для устранения (ротация IP/прокси — см.
SKILL.md §«Что остаётся за пределами скилла»).
