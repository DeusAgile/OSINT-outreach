# Шаблоны поисковых запросов

Читается `scripts/media_search.py` по имени набора. Каждый набор — блок кода
со строками-шаблонами; `{company}`, `{person}`, `{year}` подставляются из
аргументов скрипта. Шаблон, которому не хватает параметра (например
`{person}` в наборе `person_mentions` без `--person`), молча пропускается —
это не ошибка.

Расширяется правкой этого файла, без изменения `media_search.py`.

**Набор `lpr_search`:**
```
"HRD" {company}
"директор по персоналу" {company}
"руководитель отдела подбора" {company}
"Head of Recruitment" {company}
"HR business partner" {company}
"HR-тимлид" {company}
"HR Team Lead" {company}
"HR-директор" {company}
"руководитель HR" {company}
{company} HR интервью
site:career.habr.com {company}
site:tenchat.ru {company} HR
```

**Набор `person_mentions`:**
```
"{person}" {company}
"{person}" HR
"{person}" интервью
"{person}" site:t.me
"{person}" site:vc.ru OR site:rb.ru OR site:cnews.ru
"{person}" конференция доклад
```

**Набор `company_insights`:**
```
{company} исследование HR {year}
{company} найм автоматизация
{company} новости {year}
```
