# Реестр источников

Читается модулями по полю `category` при каждом запуске (не хранится
внутри модулей). Добавление источника = новая строка здесь, без правки
логики модулей — см. «Архитектурный принцип» в SKILL.md.

`status: pending` и `status: unverified` — источники не вызываются
автоматически модулями (кроме явного ручного запроса). `status: partial` —
вызываются, но помечаются в отчёте как источник с ограниченным охватом.

| Источник | category | Скрипт | Метод | status |
|---|---|---|---|---|
| **Оркестратор** (hh.ru + сайт + habr + широкий ЛПР-поиск) | company_profile, lpr_search | `collect_company.py` | импорт функций остальных скриптов | works — заменяет 5-7 отдельных вызовов на один, см. `modules/02-company-data-collect.md` |
| hh.ru — компания | company_profile | `hh_client.py` | API при токене, иначе `data-qa` | works |
| hh.ru — вакансии | company_profile, lpr_search | `hh_client.py --with-vacancies` | то же | works |
| Сайт компании | company_profile | `site_crawler.py` | HTML | works |
| Сайт — «Команда»/«Контакты» | lpr_search | `site_crawler.py --sections team,contacts` | HTML | works |
| Ростер-страницы вне сайта компании (setka.ru, career.habr.com/employees и подобные) | lpr_search | `site_crawler.py --single-page --company` | HTML | works — проверено на реальном кейсе (WMT, setka.ru), нашло HR-контакт, который прямой HR-поиск пропустил, см. `modules/03-lpr-search.md` |
| Habr Career — компания | lpr_search | `habr_client.py --company` | HTML | works — с 2026-09-02 извлекает и раздел «Контакты» (телефон/email компании, если опубликованы), раньше эти поля были в HTML, но не парсились (найдено на реальном кейсе RED SOFT — телефон и email HR-руководителя лежали открытым текстом, скрипт их не искал) |
| Habr / Habr Q&A — персона | person_mentions | `habr_client.py --person` | HTML | partial — профиль career.habr.com сам по себе SPA без SSR (обычный GET получает пустую оболочку, поля осознанно `null`, см. предупреждение скрипта); статьи на habr.com/users всё ещё тянутся |
| Отраслевые СМИ (VC.ru, RB.ru, CNews, Forbes) | person_mentions | `media_search.py` | поиск + fetch | partial |
| Dream Job (dreamjob.ru) — отзывы и процесс отбора | hr_process | `dreamjob_client.py --company-url` | HTML | works — проверено живьём: рейтинг/категории/тексты отзывов и структурированные описания процесса отбора (`/interviews`: число этапов, формат, вопросы) извлекаются корректно. Контент субъективный (анонимные отзывы), не факт о компании — см. предупреждение самого скрипта. Поиск employer_id по названию — через `media_search.py --query-set hr_process` (`site:dreamjob.ru`), у dreamjob.ru нет собственного публичного поиска по названию |
| ComNews, AM Live, TenChat, Setka — упоминания | hr_process, person_mentions | `media_search.py --query-set hr_process` | поиск | works как поисковые цели (проверена сетевая доступность всех четырёх доменов); сам факт всё ещё зависит от общей ненадёжности `media_search.py`, см. ниже |
| VK — публичная страница компании | hr_process | `vk_client.py` | HTML | **blocked** — vk.com заблокирован на уровне сети песочницы Claude (`x-block-reason: hostname_blocked`), не проверено живьём. Код написан по тому же паттерну, что остальные HTML-источники — может заработать в среде выполнения без этой блокировки, но это не подтверждено. Первый шаг при включении — перепроверить `python vk_client.py --group <slug>`, не доверять этой строке на слово |
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
