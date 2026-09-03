# TalentMind Outreach Intelligence

Claude Skill: research pipeline for B2B sales/recruiting outreach — given
a company name (или batch .xlsx со списком компаний), находит сферу
деятельности компании, HR decision-maker'ов, их публичный цифровой след,
и генерирует персонализированные черновики (Telegram/email) с указанием
источника на каждый факт.

**Точка входа — [`SKILL.md`](./SKILL.md).** Там архитектурный принцип,
как запускать пайплайн, единый формат ответа скриптов, anti-patterns,
проверенное окружение (что реально работает, что нет — по фактическим
тестам, не предположениям), правовые требования (152-ФЗ) и что осознанно
остаётся за пределами скилла.

## Структура

```
├── SKILL.md            # точка входа
├── anti-patterns.md     # чего не делать (обязательно к прочтению)
├── config/               # реестр источников, шаблоны запросов, структура отчёта
├── modules/              # 01–07: инструкции по стадиям пайплайна
├── scripts/               # источники данных, entity_resolve, xlsx-утилиты
└── templates/              # markdown-шаблоны отчёта и черновиков
```

## Быстрый старт

1. Скопировать `config/config.example.json` → `config/config.json`
   (см. `config/README.md`).
2. Прочитать `SKILL.md`.
3. Прогнать по компании: `modules/01-company-resolve.md` →
   `07-batch-runner.md` по порядку (для одной компании — до `06`).

## Упаковка в `.skill`

```
python3 -m scripts.package_skill <путь-к-этой-папке> <output-dir>
```
(скрипт `package_skill.py` — часть публичного `skill-creator`, не входит
в этот репозиторий).
